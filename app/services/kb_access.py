"""知识库访问权限的唯一判定处

一张权限矩阵（DEVELOPMENT.md 第 6 节也写了同一份）：

| 操作                                | owner | write 授权 | read 授权 | public 库的其他人 |
|-------------------------------------|:-----:|:----------:|:---------:|:-----------------:|
| 看库信息 / 文档列表 / 详情 / 下载     |   ✓   |     ✓      |    ✓      |        ✓          |
| 上传文档 / 删除文档（含别人传的）      |   ✓   |     ✓      |    ✗      |        ✗          |
| 改库名/描述/切块参数、删库、增删授权   |   ✓   |     ✗      |    ✗      |        ✗          |

对外语义（避免用权限探测别人的库是否存在）：
    连 read 都没有  → 当作「不存在」404
    能 read 不能写  → 403，msg 里说明需要什么权限

为什么 write 授权能删别人传的文档：共享库是团队资产，删错靠 /documents/status
对账 + 数据库层软删兜底；但改库设置/删库/再授权只能是 owner。
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud import kb_crud
from app.db.models import KbPermission, KbShare, KnowledgeBase, PERMISSION_LEVEL
from app.core.exceptions import ForbiddenError, NotFoundError


@dataclass(frozen=True)
class KbAccess:
    """一次权限判定的结果，路由拿到后可以直接用 can_read / can_write"""

    kb: KnowledgeBase
    permission: KbPermission | None = None
    is_owner: bool = False
    share: KbShare | None = None

    @property
    def level(self) -> int:
        if self.is_owner:
            return 99
        return PERMISSION_LEVEL.get(self.permission, 0) if self.permission else 0

    @property
    def can_read(self) -> bool:
        return self.permission is not None or self.is_owner

    @property
    def can_write(self) -> bool:
        return self.level >= PERMISSION_LEVEL[KbPermission.WRITE]

    @property
    def can_manage(self) -> bool:
        """改库设置 / 删库 / 增删授权 —— 只有 owner"""
        return self.is_owner

    def require_read(self, msg: str = "没有该知识库的读取权限") -> "KbAccess":
        if not self.can_read:
            raise NotFoundError("知识库不存在")   # 读不到就不暴露它的存在
        return self

    def require_write(self) -> "KbAccess":
        if not self.is_owner and self.permission != KbPermission.WRITE:
            raise ForbiddenError("需要知识库的读写（write）权限")
        return self

    def require_manage(self) -> "KbAccess":
        if not self.is_owner:
            raise ForbiddenError("只有知识库创建者可以执行该操作")
        return self


async def resolve(kb: KnowledgeBase, user_id, db: AsyncSession) -> KbAccess:
    """算出某个用户对某知识库的访问级别"""
    if str(kb.owner_id) == str(user_id):
        return KbAccess(kb=kb, permission=KbPermission.WRITE, is_owner=True)

    share = await kb_crud.get_share(kb.id, user_id, db)
    permission = KbPermission(share.permission) if share else None

    # public 库给所有人只读；但如果已经单独授过 write，就以更高的那条为准
    if kb.visibility == "public":
        if permission is None:
            permission = KbPermission.READ
        # 已经是 write 的不动

    return KbAccess(kb=kb, permission=permission, is_owner=False, share=share)


async def access_of(kb_id, user_id, db: AsyncSession) -> KbAccess | None:
    """按 kb_id 取权限；库不存在/已软删返回 None（路由统一当 404 处理）"""
    kb = await kb_crud.get_kb(kb_id, db)
    if kb is None:
        return None
    return await resolve(kb, user_id, db)


async def document_access(document, user_id, db: AsyncSession) -> KbAccess | None:
    """文档的可见性取决于它所在知识库的权限（共享之后不能再只认 owner_id）"""
    return await access_of(document.kb_id, user_id, db)


async def accessible_kb_ids(user_id, db: AsyncSession) -> list:
    """这个用户「读得到」的所有库 id：自己的 + 被授权的 + public 的

    列表类查询（比如不带 kb_id 的 /documents/list）用它做 IN 过滤，
    否则共享库里的文档永远不会出现在别人眼前。
    """
    owned = [kb.id for kb in (await kb_crud.list_owned_kbs(user_id, db, 0, 500))[0]]
    shared = await kb_crud.list_shared_kb_ids(user_id, db)
    public = await kb_crud.list_public_kb_ids(db)

    merged: list = []
    seen = set()
    for kb_id in [*owned, *shared, *public]:
        key = str(kb_id)
        if key not in seen:
            seen.add(key)
            merged.append(kb_id)
    return merged
