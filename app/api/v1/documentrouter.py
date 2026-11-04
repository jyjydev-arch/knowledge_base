"""文档路由（M1 进行中）

对应表：documents（原始文件） + knowledge_bases（所属知识库） + chunks（切块，M2 写入）

用户身份一律来自 get_current_user（Authorization 头），不接受 user_id 参数；
CRUD 层每条查询都带 owner_id，越权在数据层就挡掉。
"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DB, CurrentUser
from app.core.exceptions import NotImplementedFeatureError
from app.db.crud import document_crud
from app.schemas.common import ApiResponse, PageData, PageParams
from app.schemas.document_schemas import DocumentResponse

router = APIRouter(prefix="/documents", tags=["Documents"])


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


@router.post("/upload", summary="上传文档（未实现）")
async def upload_document():
    """
    上传文档，更新知识库文档的内容
    实现方法：前端上传文档，后端接收文档，解析文档内容，存储到data/(用户id)/documents下，
    成功后再进行将（文件名，文件路径，用户id，创建时间）存储到数据库中，并且更新知识库的版本信息。

    实现提示（M1）：入参 UploadFile + kb_id；sha256 判重用 document_crud.get_document_by_hash；
    写库用 document_crud.create_document（内部已 refresh_kb_stats 更新知识库版本信息）。
    """
    raise NotImplementedFeatureError("上传文档还没实现（见 TODO.md P0-M1）")


@router.get("/detail", summary="文档详情（未实现）")
async def document_detail():
    """
    显示出当前知识库文档的主要内容以及当前知识库的版本信息。

    实现提示（M1）：document_crud.get_document(document_id, current_user.id, db) 取文档，
    正文取该文档 seq 最小的几个 chunk，kb_name / kb_version 来自 knowledge_bases。
    """
    raise NotImplementedFeatureError("文档详情还没实现（见 TODO.md P0-M1）")


@router.delete("/delete/{document_id}", summary="删除文档（未实现）")
async def delete_document():
    """
    删除指定文档id的文档，并且删除后需要更新知识库的版本信息。
    进入data/(用户id)/documents下，删除指定文档id的文档，并且删除后需要更新知识库的版本信息。
    1. 删除数据库中指定文档id的记录
    2. 删除data/(用户id)/documents下的指定文档id的文档
    3. 更新知识库的版本信息
    4. 返回删除成功的消息
    5. 如果删除失败，返回删除失败的消息
    6. 如果文档不存在，返回文档不存在的消息
    7. 如果文档id不合法，返回文档id不合法的消息
    8. 如果文档id为空，返回文档id不是uuid格式的消息

    实现提示（M1）：1/3 已经由 document_crud.delete_document 做好（级联删 chunks + refresh_kb_stats）；
    6-8 交给路径参数 document_id: uuid.UUID（格式非法 FastAPI 直接 422，越权与不存在统一 404）；
    2 删磁盘文件记得 resolve() 后确认仍在 data/{user_id}/ 下，防目录穿越。
    """
    raise NotImplementedFeatureError("删除文档还没实现（见 TODO.md P0-M1）")


@router.get("/status", summary="知识库/文档状态（未实现）")
async def document_status():
    """
    显示当前知识库内容的状态，包括文档解析状态

    实现提示（M1）：document_crud.count_by_status(current_user.id, db, kb_id) 给出各状态数量，
    再带上知识库的 version / last_indexed_at 返回即可。
    """
    raise NotImplementedFeatureError("状态查询还没实现（见 TODO.md P0-M1）")


@router.get("/download/{document_id}", summary="下载文档（未实现）")
async def download_document():
    """
    下载指定文档

    实现提示（M1）：先 document_crud.get_document(...) 校验归属，再 FileResponse(doc.file_path)。
    """
    raise NotImplementedFeatureError("文档下载还没实现（见 TODO.md P0-M1）")
