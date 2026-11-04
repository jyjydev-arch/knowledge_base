"""路由层公共依赖：数据库 Session、当前登录用户"""

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import decode_access_token
from app.db.crud import token_crud, user_crud
from app.db.models import User
from app.db.psql_config import get_db

# auto_error=False: 没带 Authorization 时由我们自己返回统一的 {"status":401,...}
bearer_scheme = HTTPBearer(auto_error=False, description="JWT，格式: Bearer <access_token>")

# 把 Session 类型固定下来，路由里写 `db: DB` 即可
DB = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: DB,
) -> User:
    """从 Authorization 头解析 JWT，取出当前登录用户

    校验链路: 有 token -> token 能解析 -> 用户还在 -> token 没被登出/过期
    """
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("未登录，请先携带 Authorization: Bearer <token>")

    token = credentials.credentials
    payload = decode_access_token(token)
    if not payload or not payload.get("sub"):
        raise UnauthorizedError("登录凭证无效或已过期，请重新登录")

    user = await user_crud.get_user_by_id(payload["sub"], db)
    if user is None or user.deleted_at is not None:
        raise UnauthorizedError("账号不存在或已注销，请重新登录")
    if not user.is_active:
        raise ForbiddenError("账号已被禁用，请联系管理员")

    if not await token_crud.is_token_active(token, db):
        raise UnauthorizedError("登录已失效（已登出或被踢下线），请重新登录")

    return user


async def get_current_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> str:
    """取请求头里的原始 JWT 字符串（退出登录时要按它删掉库里的记录）"""
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("未登录，请先携带 Authorization: Bearer <token>")
    return credentials.credentials


async def get_optional_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: DB,
) -> User | None:
    """可选登录：没带 token 返回 None，带了就必须是合法的"""
    if credentials is None or not credentials.credentials:
        return None

    payload = decode_access_token(credentials.credentials)
    if not payload or not payload.get("sub"):
        raise UnauthorizedError("登录凭证无效或已过期，请重新登录")

    user = await user_crud.get_user_by_id(payload["sub"], db)
    if user is None or user.deleted_at is not None or not user.is_active:
        return None
    # 之前漏了这一步：已吊销的 token 不该再算登录
    if not await token_crud.is_token_active(credentials.credentials, db):
        return None
    return user


async def require_admin_dep(user: User = Depends(get_current_user)) -> User:
    """管理员校验：直接看 users.role

    第一个管理员从哪来：注册时用户名命中 settings.ADMIN_USERNAMES 就写 role=admin，
    之后正常权限都走 role 字段，不再依赖配置。
    """
    if not user.is_admin:
        raise ForbiddenError("需要管理员权限")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin_dep)]
CurrentToken = Annotated[str, Depends(get_current_token)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
