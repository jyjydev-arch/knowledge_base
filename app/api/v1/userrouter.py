"""用户路由

所有接口统一返回 {"status": ..., "msg": ..., "data": ...} 结构
（data 的具体类型由 response_model=ApiResponse[...] 决定，Swagger 里能直接看到）。

失败时的处理方式:
    路由里直接 raise NotFoundError / ConflictError / UnauthorizedError ...，
    app/main.py 的异常处理器会把它们转成同样的格式，无需每个接口手写。
"""

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import DB, AdminUser, CurrentToken, CurrentUser, ensure_ownership
from app.core.config import settings
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    ParamError,
    UnauthorizedError,
)
from app.core.security import (
    create_access_token,
    get_password_hash,
    token_expire_datetime,
    verify_password,
)
from app.db.crud import token_crud, user_crud
from app.schemas.common import ApiResponse, PageData, PageParams
from app.schemas.user_schemas import (
    AccountDelete,
    PasswordChange,
    TokenResponse,
    UserCreate,
    UserLogin,
    UserResponse,
    UserUpdate,
)

router = APIRouter(
    prefix="/user",
    tags=["用户"],
)


@router.post(
    "/register",
    response_model=ApiResponse[UserResponse],
    summary="用户注册",
)
async def register_user(payload: UserCreate, db: DB):
    """注册新用户：用户名、邮箱都唯一，密码只存 bcrypt 哈希"""
    if await user_crud.is_username_taken(payload.username, db):
        raise ConflictError(f"用户名 {payload.username} 已被注册")
    if await user_crud.is_email_taken(payload.email, db):
        raise ConflictError("该邮箱已被注册")

    user = await user_crud.create_user(payload, get_password_hash(payload.password), db)
    if user is None:  # 并发注册撞了唯一索引
        raise ConflictError("用户名或邮箱已被注册，请更换后重试")

    return ApiResponse.ok(UserResponse.model_validate(user), msg="注册成功")


@router.post("/login", response_model=ApiResponse[TokenResponse], summary="用户登录")
async def login_user(payload: UserLogin, db: DB):
    """登录：account 支持用户名或邮箱，成功后签发 JWT 并落库（便于退出登录）"""
    user = await user_crud.get_user_by_account(payload.account, db)
    # 用户不存在和密码错误返回同一个提示，避免暴露账号是否存在
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise UnauthorizedError("用户名或密码错误")

    expires_delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    token = create_access_token(user.id, expires_delta)
    await token_crud.save_token(token, user.id, token_expire_datetime(expires_delta), db)

    data = TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=int(expires_delta.total_seconds()),
        user=UserResponse.model_validate(user),
    )
    return ApiResponse.ok(data, msg="登录成功")


@router.get("/info", response_model=ApiResponse[UserResponse], summary="获取用户信息")
async def get_user_info(
    current_user: CurrentUser,
    db: DB,
    user_id: Annotated[str | None, Query(description="要查询的用户ID，不传默认查自己")] = None,
):
    """查询用户信息：不传 user_id 就查当前登录用户，传了只能查自己"""
    if user_id is None:
        return ApiResponse.ok(UserResponse.model_validate(current_user), msg="获取成功")

    await ensure_ownership(current_user, user_id)
    target = await user_crud.get_user_by_id(user_id, db)
    if target is None:
        raise NotFoundError("用户不存在")

    return ApiResponse.ok(UserResponse.model_validate(target), msg="获取成功")


@router.post("/logout", response_model=ApiResponse[None], summary="退出登录")
async def logout_user(
    current_user: CurrentUser,
    token: CurrentToken,
    db: DB,
):
    """退出登录：把当前 JWT 从 token 表删掉，之后再拿它请求就会 401"""
    await token_crud.revoke_token(token, db)
    return ApiResponse.ok(msg="已退出登录")


@router.post("/update", response_model=ApiResponse[UserResponse], summary="修改用户资料")
async def update_user_info(payload: UserUpdate, current_user: CurrentUser, db: DB):
    """修改用户名 / 邮箱，只更新传了的字段"""
    if not payload.model_dump(exclude_unset=True):
        raise ParamError("没有需要更新的内容")

    if payload.username and payload.username != current_user.username:
        if await user_crud.is_username_taken(payload.username, db, exclude_id=current_user.id):
            raise ConflictError(f"用户名 {payload.username} 已被占用")
    if payload.email and payload.email != current_user.email:
        if await user_crud.is_email_taken(payload.email, db, exclude_id=current_user.id):
            raise ConflictError("该邮箱已被占用")

    user = await user_crud.update_user(current_user, payload, db)
    if user is None:
        raise ConflictError("用户名或邮箱已被占用，请更换后重试")

    return ApiResponse.ok(UserResponse.model_validate(user), msg="修改成功")


@router.post("/reset_password", response_model=ApiResponse[None], summary="修改密码")
async def reset_password(payload: PasswordChange, current_user: CurrentUser, db: DB):
    """改密码：需要校验原密码，改完后所有端上的 token 全部失效，需重新登录"""
    if not verify_password(payload.old_password, current_user.hashed_password):
        raise UnauthorizedError("原密码不正确")

    await user_crud.set_password(current_user, get_password_hash(payload.new_password), db)
    revoked = await token_crud.revoke_all_user_tokens(current_user.id, db)

    return ApiResponse.ok(msg=f"密码修改成功，请重新登录（已失效 token 数: {revoked}）")


@router.post("/delete", response_model=ApiResponse[None], summary="注销账号")
async def delete_user(payload: AccountDelete, current_user: CurrentUser, db: DB):
    """注销账号：需要密码确认，关联的 token / documents 会被级联删除"""
    if not verify_password(payload.password, current_user.hashed_password):
        raise UnauthorizedError("密码不正确，无法注销账号")

    if not await user_crud.delete_user(current_user, db):
        raise ConflictError("账号注销失败，请稍后重试")

    return ApiResponse.ok(msg="账号已注销")


@router.get(
    "/list",
    response_model=ApiResponse[PageData[UserResponse]],
    summary="分页查询用户列表（管理员）",
)
async def list_users(
    db: DB,
    admin: AdminUser,
    page: Annotated[int, Query(ge=1, description="页码，从1开始")] = 1,
    size: Annotated[int, Query(ge=1, le=100, description="每页条数，最大100")] = 20,
):
    """管理员接口：分页查看用户，只返回 id/用户名/邮箱/创建时间"""
    params = PageParams(page=page, size=size)
    users, total = await user_crud.list_users(db, params.offset, params.limit)
    data = PageData(
        items=[UserResponse.model_validate(u) for u in users],
        total=total,
        page=params.page,
        size=params.size,
    )
    return ApiResponse.ok(data, msg="获取成功")
