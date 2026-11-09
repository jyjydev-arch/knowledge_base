"""知识库共享授权路由（3 条：授权 / 取消授权 / 名单）

约定：
    - kb_id 一律用 query 参数，配合 require_kb_access 依赖做权限判定；
    - 这三条都是 owner 专属（mode="manage"）：write 授权也不能再往下授权，
      否则权限会被人传人，失控；
    - 被授权人「有哪些库」需要 M0 的 GET /kb/list（还没做），
      目前拿到 kb_id 就能直接访问对应文档接口。
"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DB, KBManager
from app.core.exceptions import ConflictError, NotFoundError, ParamError
from app.db.crud import kb_crud, user_crud
from app.schemas.common import ApiResponse
from app.schemas.kb_schemas import KbShareCreate, KbShareOut

router = APIRouter(prefix="/kb", tags=["knowledge_bases"])


def _to_out(share, grantee_username: str, grantee_email: str) -> KbShareOut:
    """手工组装，避免在异步路由里碰 share.grantee 触发懒加载异常"""
    return KbShareOut(
        share_id=share.id,
        kb_id=share.kb_id,
        grantee_id=share.grantee_id,
        grantee_username=grantee_username,
        grantee_email=grantee_email,
        permission=share.permission,
        created_at=share.created_at,
    )


@router.post("/share", response_model=ApiResponse[KbShareOut], summary="授权他人访问知识库")
async def grant_kb_share(
    access: KBManager,
    payload: KbShareCreate,
    db: DB,
):
    """
    显示需求：让用户可以把自己的知识库共享给别人（只读 / 读写）。

    落地：
        1. 权限：只有 owner（require_kb_access("manage")）能授权，write 授权的人不行；
        2. 被授权人按 email 精确查找，找不到 404；给 owner 自己授权 400（无意义）；
        3. 同一 (kb, 用户) 再授权 = 更新权限，接口幂等，重复调用不会多出记录。
    """
    grantee = await user_crud.get_user_by_email(payload.email, db)
    if grantee is None:
        raise NotFoundError("用户不存在")
    if str(grantee.id) == str(access.kb.owner_id):
        raise ParamError("不能给自己授权，你已经是该知识库的创建者")

    share = await kb_crud.grant_share(
        access.kb.id, grantee.id, access.kb.owner_id, db, permission=str(payload.permission)
    )
    if share is None:
        raise ConflictError("授权失败，请稍后重试")

    return ApiResponse.ok(_to_out(share, grantee.username, grantee.email), msg="授权成功")


@router.delete("/share", response_model=ApiResponse[None], summary="取消授权")
async def revoke_kb_share(
    access: KBManager,
    db: DB,
    email: Annotated[str, Query(description="要取消授权的用户邮箱")],
):
    """
    取消某个用户对该知识库的授权。

    落地：
        1. 同样 owner only；
        2. 「没有这条授权」不当错误处理（幂等），msg 里说明即可，
           前端重复调用/并发取消都不会拿到 4xx；
        3. 只删授权记录，不动文档 —— 对方自己的库里上传的东西仍然归他。
    """
    grantee = await user_crud.get_user_by_email(email, db)
    if grantee is None:
        raise NotFoundError("用户不存在")

    removed = await kb_crud.revoke_share(access.kb.id, grantee.id, db)
    if not removed:
        return ApiResponse.ok(msg="该用户本来就没有这个知识库的授权")

    # 授权撤销后不需要动 version（版本只反映文档内容变化）
    return ApiResponse.ok(msg="已取消授权")


@router.get("/share/list", response_model=ApiResponse[list[KbShareOut]], summary="知识库授权名单")
async def list_kb_shares(access: KBManager, db: DB):
    """
    查看某知识库当前的授权名单（owner only）。

    实现提示：kb_crud.list_shares 用 selectinload 一次性把被授权人带出来，
    这样组装响应时不会触发异步懒加载（MissingGreenlet）。
    """
    shares = await kb_crud.list_shares(access.kb.id, db)
    items = [
        _to_out(share, share.grantee.username, share.grantee.email)
        for share in shares
        if share.grantee is not None  # 用户被硬删时 selectinload 拿不到，跳过而不是 500
    ]
    return ApiResponse.ok(items, msg="获取成功")
