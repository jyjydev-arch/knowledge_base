"""文档路由（M1 进行中）

对应表：documents（原始文件） + knowledge_bases（所属知识库） + chunks（切块，M2 写入）

用户身份一律来自 get_current_user（Authorization 头），不接受 user_id 参数；
CRUD 层每条查询都带 owner_id，越权在数据层就挡掉。
"""

import uuid
from pathlib import Path as FsPath
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Path, Query, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import DB, CurrentUser
from app.core.document_hash import calculate_document_hash
from app.core.exceptions import (
    BusinessError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ParamError,
)
from app.db.crud import document_crud
from app.schemas.common import ApiResponse, PageData, PageParams
from app.schemas.document_schemas import DocumentDetail, DocumentResponse, DocumentStatusOut
from app.services.process_document import (
    SAFE_EXT,
    delete_document_file,
    document_storage_path,
    kb_storage_dir,
    new_document_file_path,
    remove_kb_directory_if_empty,
    search_document_file,
)

router = APIRouter(prefix="/documents", tags=["Documents"])  # 版本前缀统一在 main.py 加，别在这里再写一遍

# 上传/删除/下载共用的边界常量（扩展名与 uuid 段的字符白名单在 process_document 里定义，只此一份）
USER_DATA_ROOT = FsPath("data")  # 相对进程 CWD（start.sh 在项目根启动）
PREVIEW_MAX_CHARS = 2000  # 详情正文预览的截断长度
PREVIEW_CHUNKS = 3        # 预览最多取前几块

ALLOWED_EXTENSIONS = {
    "pdf": "application/pdf",
    "md": "text/markdown",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB

DOCUMENT_NOT_FOUND_MSG = "文档不存在"


def _safe_basename(raw: str | None) -> str:
    """客户端传来的 filename 完全不可信

    可能是 Windows 全路径（C:\\Users\\x\\a.pdf）、带 ../ 的名字、甚至空；
    只取最后一段用于展示，绝不拿它拼路径（落盘文件名一律用 document_id）。
    """
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    if not name:
        raise ParamError("文件名为空，无法判断类型")
    return name


def _safe_extension(filename: str) -> str:
    """扩展名白名单：先过字符集（挡注入），再过类型（挡不支持的格式）"""
    ext = FsPath(filename).suffix.lstrip(".").lower()
    if not SAFE_EXT.match(ext):
        raise ParamError("文件扩展名不合法")
    if ext not in ALLOWED_EXTENSIONS:
        raise ParamError(f"不支持的文件类型 .{ext}，目前只支持 {'/'.join(sorted(ALLOWED_EXTENSIONS))}")
    return ext


def _cleanup_plan(document) -> tuple[str, str, str] | None:
    """从文档记录算出「可以安全删除」的 (user_id, kb_id, ext)；不可信就返回 None

    校验点（顺序固定，任一不过就只删记录、绝不碰磁盘）：
        空路径 / 绝对路径 / 含 .. / 扩展名不合法 /
        与 document_storage_path(user_id, kb_id, document_id, ext) 拼出来的路径不一致 /
        解析后跳出本人目录。
    最后一条最关键：删除是不可逆操作，宁可留个孤儿文件（/status 会报 orphan_files），
    也不能删错别人的东西。
    """
    if not document.file_path:
        return None

    stored = FsPath(document.file_path)
    if stored.is_absolute() or ".." in stored.parts:
        return None

    ext = stored.suffix.lstrip(".").lower()
    if not SAFE_EXT.match(ext):
        return None

    try:
        expected = FsPath(document_storage_path(document.owner_id, document.kb_id, document.id, ext))
    except ValueError:  # service 层的 uuid 段校验没过
        return None

    if expected != stored:
        return None
    if expected.resolve().parent != FsPath(kb_storage_dir(document.owner_id, document.kb_id)).resolve():
        return None
    return str(document.owner_id), str(document.kb_id), ext


def _readable_path(document) -> FsPath | None:
    """下载用的路径判定：比删除宽松（老记录可能不是当前约定拼的），但仍不能跳出本人目录"""
    if not document.file_path:
        return None

    stored = FsPath(document.file_path)
    if stored.is_absolute() or ".." in stored.parts:
        return None

    # file_path 已经带 data/ 前缀，直接相对 CWD 解析，不能再拼一次 USER_DATA_ROOT
    owner_root = (USER_DATA_ROOT / str(document.owner_id)).resolve()
    resolved = stored.resolve()
    if owner_root not in resolved.parents:
        return None
    return resolved


@router.get("/list", response_model=ApiResponse[PageData[DocumentResponse]], summary="文档列表")
async def list_documents(
    current_user: CurrentUser,
    db: DB,
    page: Annotated[int, Query(ge=1, description="页码，从1开始")] = 1,
    size: Annotated[int, Query(ge=1, le=100, description="每页条数，最大100")] = 20,
    kb_id: Annotated[str | None, Query(description="只看某个知识库的文档，不传则看全部")] = None,
):
    """
    列举出所有文档的基本信息，包括文档标题，并且按照时间顺序进行排序，以及提供下载方式。
    实现方法：查询数据库，获取所有文档的基本信息，按照时间顺序进行排序，返回给前端。
    根据用户id查看用户上传的文档列表

    落地说明（改动点）：
        - 空列表是正常结果（status=200 + items=[]），不再当 404 处理；
        - 返回体不再是 ORM 对象，而是 DocumentResponse（含 download_url）；
        - ApiResponse.ok 的第一个参数是 data，不是 status（原写法 status="success" 会直接 500）。
    """
    params = PageParams(page=page, size=size)
    documents, total = await document_crud.list_documents(
        current_user.id, db, params.offset, params.limit, kb_id=kb_id
    )
    data = PageData[DocumentResponse](
        items=[DocumentResponse.model_validate(doc) for doc in documents],
        total=total,
        page=params.page,
        size=params.size,
    )
    return ApiResponse.ok(data, msg="查询成功")


@router.post("/upload", response_model=ApiResponse[DocumentResponse], summary="上传文档")
async def upload_document(
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    db: DB,
    file: Annotated[UploadFile, File(description="要上传的文档（pdf / md / docx）")],
    kb_id: Annotated[str, Query(description="所属知识库ID")],
    document_name: Annotated[str | None, Query(description="文档标题，不传则用原文件名（去扩展名）")] = None,
):
    """
    上传文档，更新知识库文档的内容
    实现方法：前端上传文档，后端接收文档，解析文档内容，存储到 data/(用户id)/ 下，
    文件名就保存为 documents_id.pdf/.md/.docs，成功后再进行将（文件名，文件路径，用户id，创建时间）
    存储到数据库中，并且更新知识库的版本信息。

    路径约定（已定，唯一来源 services.process_document.document_storage_path）：
        data/{user_id}/{kb_id}/{document_id}.{ext}     ← 存进 documents.file_path 的就是这一串相对路径

    落地顺序（边界全过才动磁盘，落盘和写库互为回滚）：
        1. 类型边界：原始文件名先 sanitize（可能是 Windows 全路径/带 ../），扩展名先过字符集
           再过白名单（pdf/md/docx）→ 不合法 400；
        2. 归属边界：kb_id 是客户端传的，必须校验「存在 + 属于当前用户」，
           否则就能把文件塞进别人的知识库 → 不通过 404（和不存在同一响应，不透露归属）；
        3. 内容边界：流式算 sha256 并在读取过程中卡 20MB 上限（不能信 Content-Length），
           超限 413；算完把指针拨回 0，落盘还要从头拷；
        4. 判重：按 uq(kb_id, file_hash) 的口径在**同一个知识库内**判重，重复直接 409，
           一个字节都不再写；
        5. 先生成 document_id 再落盘（文件名要用它），写文件放线程池，避免阻塞事件循环；
        6. 写库：create_document 内部级联处理知识库统计与 version+1（需求「更新知识库的版本信息」）；
        7. 任一步失败都回滚已落盘的文件，不留垃圾；返回体给前端 DocumentResponse（含 id/status/download_url）。

    background_tasks 目前没用上：M2 解析/切块接进来后，这里会
    background_tasks.add_task(process_document, document.id) 让上传接口先返回再慢慢解析。
    """
    # 1. 类型边界
    original_name = _safe_basename(file.filename)
    ext = _safe_extension(original_name)

    # 2. 知识库归属边界
    kb = await document_crud.get_knowledge_base(kb_id, current_user.id, db)
    if kb is None:
        raise NotFoundError("知识库不存在")

    # 3. 内容指纹 + 体积上限
    file_hash, file_size = await calculate_document_hash(file, max_bytes=MAX_UPLOAD_BYTES)

    # 4. 同库判重（先查重再写盘，重复文件不会留下垃圾）
    if await document_crud.get_document_by_hash(kb.id, file_hash, db) is not None:
        raise ConflictError("该文件已在此知识库中，未重复入库")

    # 5. 先生成 id，再按约定落盘：data/{user_id}/{kb_id}/{document_id}.{ext}
    document_id = uuid.uuid4()
    stored_path = document_storage_path(current_user.id, kb.id, document_id, ext)
    try:
        await run_in_threadpool(
            new_document_file_path, file, str(current_user.id), str(kb.id), str(document_id), ext
        )
    except OSError:
        raise BusinessError("文件保存失败，请稍后重试", status=500)

    # 6/7. 写库，失败就把刚落盘的文件删掉
    try:
        document = await document_crud.create_document(
            kb_id=kb.id,
            owner_id=current_user.id,
            title=(document_name or FsPath(original_name).stem)[:255],
            filename=original_name[:255],
            file_path=stored_path,
            file_size=file_size,
            mime_type=ALLOWED_EXTENSIONS[ext],  # 用白名单派生的类型，客户端 Content-Type 可以随便伪造
            file_hash=file_hash,
            db=db,
            document_id=document_id,
        )
    except Exception as exc:
        await _cleanup_uploaded_file(current_user.id, kb.id, document_id, ext)
        raise BusinessError("文档入库失败，已回滚", status=500) from exc

    if document is None:  # 外键失败 / 并发下撞了唯一索引
        await _cleanup_uploaded_file(current_user.id, kb.id, document_id, ext)
        raise ConflictError("文档入库失败，请重试")

    return ApiResponse.ok(DocumentResponse.model_validate(document), msg="上传成功")


async def _cleanup_uploaded_file(user_id, kb_id, document_id: uuid.UUID, ext: str) -> None:
    """回滚用：删掉刚落盘但没能入库的文件，并把空出来的知识库目录一并收掉

    删不掉也只是留个孤儿文件（/status 会报出来），不影响接口结果，所以这里吞掉 OSError。
    """
    try:
        await run_in_threadpool(delete_document_file, str(user_id), str(kb_id), str(document_id), ext)
        await run_in_threadpool(remove_kb_directory_if_empty, str(user_id), str(kb_id))
    except OSError:
        pass


@router.delete("/delete/{document_id}", response_model=ApiResponse[None], summary="删除文档")
async def delete_document(
    document_id: Annotated[uuid.UUID, Path(description="文档ID，必须是 uuid")],
    current_user: CurrentUser,
    db: DB,
):
    """
    删除指定文档id的文档，并且删除后需要更新知识库的版本信息。
    进入 data/{user_id}/{kb_id}/ 下，删除指定文档id的文档，并且删除后需要更新知识库的版本信息。
    1. 删除数据库中指定文档id的记录
    2. 删除 data/{user_id}/{kb_id}/ 下的指定文档id的文档
        文件名字就保存为documents_id.pdf/.md/.docs
    3. 更新知识库的版本信息
    4. 返回删除成功的消息
    5. 如果删除失败，返回删除失败的消息
    6. 如果文档不存在，返回文档不存在的消息
    7. 如果文档id不合法，返回文档id不合法的消息
    8. 如果文档id为空，返回文档id不是uuid格式的消息

    落地顺序（先判边界，再动数据和磁盘）：
        a. 401：未登录/token 失效由 CurrentUser 依赖挡掉；
        b. 需求 7-9：document_id 声明成路径参数 uuid.UUID，空值/非 uuid/不合法 FastAPI 直接 422；
        c. 需求 6：document_crud.get_document(id, owner_id, db) 一次做完「存在性 + 归属」，
           别人的文档和不存在一律 404，不透露归属；
        d. 路径边界：_cleanup_plan(document) 按 document_storage_path 重算路径，
           绝对路径 / 含 .. / 扩展名不合法 / 与约定路径对不上 / 跳出本人目录 —— 一律返回 None，
           也就是只删记录不碰磁盘（宁可留孤儿文件让 /status 报 orphan_files，也不能删错东西）；
        e. 需求 1/3/5：先删数据库（级联删 chunks + refresh_kb_stats：version+1、重算统计、
           写 last_indexed_at）；删库失败直接 409，此时绝不碰磁盘；
        f. 需求 2/4：库删成功后再调 delete_document_file(user_id, kb_id, document_id, ext) 删文件，
           目录空了顺手 remove_kb_directory_if_empty；同步 IO 放线程池，不阻塞事件循环。

    为什么先删库再删文件：库里查不到就是对外一致的状态；反过来（文件先没了、库没删掉）
    会留下「有记录却读不到文件」的坏数据，比残留一个孤儿文件难恢复得多。
    """
    # c. 存在性 + 归属
    document = await document_crud.get_document(document_id, current_user.id, db)
    if document is None:
        raise NotFoundError(DOCUMENT_NOT_FOUND_MSG)

    # d. 路径边界（只读校验，此刻还不碰磁盘）
    plan = _cleanup_plan(document)
    user_id, kb_id, ext = plan if plan else (None, None, None)

    # e. 先删数据库
    if not await document_crud.delete_document(document, db):
        raise ConflictError("删除失败，请稍后重试")

    if plan is None:
        return ApiResponse.ok(msg="文档记录已删除（存储路径不合法，未清理磁盘文件）")

    # f. 再删磁盘文件
    try:
        removed = await run_in_threadpool(delete_document_file, user_id, kb_id, str(document_id), ext)
        await run_in_threadpool(remove_kb_directory_if_empty, user_id, kb_id)
    except OSError:
        return ApiResponse.ok(msg="文档记录已删除，但磁盘文件清理失败，请稍后重试")

    if not removed:
        return ApiResponse.ok(msg="文档已删除（磁盘文件不存在，可能已清理）")
    return ApiResponse.ok(msg="文档已删除")


@router.get("/detail", response_model=ApiResponse[DocumentDetail], summary="文档详情")
async def document_detail(
    current_user: CurrentUser,
    db: DB,
    document_id: Annotated[uuid.UUID, Query(description="文档ID")],
    preview_chunks: Annotated[int, Query(ge=1, le=20, description="正文预览最多取几块")] = PREVIEW_CHUNKS,
):
    """
    显示出当前知识库文档的主要内容以及当前知识库的版本信息。

    落地说明：
        1. 归属和存在性一次查完（get_document 带 owner_id）→ 别人的文档一律 404；
        2. 正文来自 chunks 表按 seq 升序的前 N 块（M2 还没做时为空，content_preview=null，
           不算错误，前端按 chunk_total=0 提示「尚未解析」）；
        3. kb_name / kb_version 一起返回，正好对上需求里的「当前知识库的版本信息」。
    """
    document = await document_crud.get_document(document_id, current_user.id, db)
    if document is None:
        raise NotFoundError(DOCUMENT_NOT_FOUND_MSG)

    chunks = await document_crud.list_chunks(document_id, current_user.id, db, limit=preview_chunks)
    kb = await document_crud.get_knowledge_base(document.kb_id, current_user.id, db)

    preview = "\n\n".join(chunk.content for chunk in chunks if chunk.content)
    detail = DocumentDetail.model_validate(document).model_copy(
        update={
            "content_preview": (preview[:PREVIEW_MAX_CHARS] or None),
            "kb_name": kb.name if kb else None,
            "kb_version": kb.version if kb else None,
            "chunk_total": document.chunk_count,
        }
    )
    return ApiResponse.ok(detail, msg="获取成功")


@router.get("/download/{document_id}", response_class=FileResponse, summary="下载文档")
async def download_document(
    document_id: Annotated[uuid.UUID, Path(description="文档ID，必须是 uuid")],
    current_user: CurrentUser,
    db: DB,
):
    """
    下载指定文档

    落地说明：
        1. 先 document_crud.get_document(id, owner_id) 校验存在 + 归属（别人的文档 404）；
        2. 再 _readable_path() 做路径边界：绝对路径 / 含 ../ / 跳出 data/<本人>/ 的一律拒绝，
           下载比删除宽松（历史记录的拼法可能不是当前约定），但仍然不能越目录；
        3. 文件不在磁盘上 → 404「文件已丢失」，并在 msg 里提示可以用 /status 核对；
        4. 成功时返回的是文件流（不是 ApiResponse 那层 JSON），
           所以这条接口的错误响应才是统一格式，正常响应走 Content-Disposition 下载；
           Content-Disposition 用 documents.filename（原始名）而不是 document_id.ext。
    """
    document = await document_crud.get_document(document_id, current_user.id, db)
    if document is None:
        raise NotFoundError(DOCUMENT_NOT_FOUND_MSG)

    path = _readable_path(document)
    if path is None:
        raise ForbiddenError("文档存储路径不合法，无法下载")
    if not await run_in_threadpool(path.is_file):
        raise NotFoundError("文件已丢失，请重新上传或用 /documents/status 核对")

    return FileResponse(
        path=str(path),
        filename=document.filename or path.name,
        media_type=document.mime_type or "application/octet-stream",
    )


@router.get("/status", response_model=ApiResponse[DocumentStatusOut], summary="知识库/文档状态")
async def document_status(
    current_user: CurrentUser,
    db: DB,
    kb_id: Annotated[str, Query(description="知识库ID")],
):
    """
    显示当前知识库内容的状态，包括文档解析状态

    落地说明：
        1. kb_id 先过「存在 + 属于当前用户」，否则 404（原来的写法这里会直接 TypeError）；
        2. 解析进度用 document_crud.count_by_status，五种状态都会补齐 0，前端不用猜键；
        3. 一致性核对按你的思路做，但改成可判定的结构化结果而不是比 len：
           库里每条记录的 file_path 取文件名，和 data/{user_id}/{kb_id}/ 目录下的实际文件对比，
           缺的进 missing_files（文档打不开）、多的进 orphan_files（占空间的垃圾）；
        4. 空库不是异常：document_total=0 且 healthy=true，msg 说明「目前没有文件」；
        5. 不一致也返回 200 + healthy=false —— 接口本身执行成功了，
           数据完整性问题要用数据字段表达，别用 HTTP 状态码混着业务语义。
    """
    kb = await document_crud.get_knowledge_base(kb_id, current_user.id, db)
    if kb is None:
        raise NotFoundError("知识库不存在")

    counts = await document_crud.count_by_status(current_user.id, db, kb_id=kb.id)
    stored_paths = await document_crud.list_document_paths(kb.id, current_user.id, db)
    # 列目录是同步 IO，放线程池里跑
    disk_files = await run_in_threadpool(search_document_file, str(current_user.id), str(kb.id))

    disk_names = set(disk_files)
    expected_names = {FsPath(path).name for path in stored_paths}
    missing_files = sorted(name for name in expected_names if name not in disk_names)
    orphan_files = sorted(name for name in disk_names if name not in expected_names)
    healthy = not missing_files and not orphan_files

    if not stored_paths and not disk_files:
        msg = "知识库目前没有文件"
    elif healthy:
        msg = f"知识库文件正常（共 {len(stored_paths)} 个）"
    else:
        msg = f"文件不一致：{len(missing_files)} 个缺失、{len(orphan_files)} 个多余，请检查"

    data = DocumentStatusOut(
        kb_id=kb.id,
        kb_version=kb.version,
        last_indexed_at=kb.last_indexed_at,
        document_total=len(stored_paths),
        status_counts={str(status): count for status, count in counts.items()},
        disk_file_count=len(disk_files),
        missing_files=missing_files,
        orphan_files=orphan_files,
        healthy=healthy,
    )
    return ApiResponse.ok(data, msg=msg)
