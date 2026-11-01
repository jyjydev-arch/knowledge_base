"""用户相关的请求 / 响应模型（Pydantic v2）"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

# 用户名: 3~50 位，允许中英文、数字、下划线、中划线
USERNAME_PATTERN = r"^[A-Za-z0-9_\u4e00-\u9fa5][A-Za-z0-9_\-\u4e00-\u9fa5]{2,49}$"
# bcrypt 只使用密码的前 72 个字节，这里直接限制长度，避免注册后被静默截断
PASSWORD_MIN_LENGTH = 6
PASSWORD_MAX_LENGTH = 72


class UserBase(BaseModel):
    """注册和返回给前端共用的字段"""

    username: str = Field(min_length=3, max_length=50, pattern=USERNAME_PATTERN, description="用户名")
    email: EmailStr = Field(max_length=255, description="邮箱")

    model_config = ConfigDict(str_strip_whitespace=True)


class UserCreate(UserBase):
    """注册请求体"""

    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH, description="密码，6-72位")


class UserLogin(BaseModel):
    """登录请求体

    account 支持用户名或邮箱；为了兼容前端的旧写法，
    也允许只传 username。
    """

    account: str | None = Field(default=None, max_length=255, description="用户名或邮箱")
    username: str | None = Field(default=None, max_length=255, description="account 的别名，只填用户名时可用")
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH, description="密码")

    model_config = ConfigDict(str_strip_whitespace=True)

    @model_validator(mode="after")
    def _resolve_account(self) -> "UserLogin":
        account = (self.account or self.username or "").strip()
        if not account:
            raise ValueError("account(用户名或邮箱) 不能为空")
        self.account = account
        return self

    @field_validator("password")
    @classmethod
    def _password_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("密码不能为空")
        return value


class UserUpdate(BaseModel):
    """修改资料请求体，字段都是可选的，传什么改什么"""

    username: str | None = Field(default=None, min_length=3, max_length=50, pattern=USERNAME_PATTERN)
    email: EmailStr | None = Field(default=None, max_length=255)

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("username", "email")
    @classmethod
    def _not_empty_string(cls, value):
        if value is not None and not str(value).strip():
            raise ValueError("不能传空字符串")
        return value


class PasswordChange(BaseModel):
    """修改密码（需要校验旧密码）"""

    old_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH, description="原密码")
    new_password: str = Field(
        min_length=PASSWORD_MIN_LENGTH,
        max_length=PASSWORD_MAX_LENGTH,
        description="新密码，6-72位",
    )

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("new_password")
    @classmethod
    def _new_password_not_equal_old(cls, value: str, info):
        old = info.data.get("old_password")
        if old and value == old:
            raise ValueError("新密码不能和原密码相同")
        return value


class AccountDelete(BaseModel):
    """注销账号请求体，出于安全考虑要传密码"""

    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH, description="当前账号密码")


class UserResponse(UserBase):
    """返回给前端的用户信息（绝不会带 hashed_password）"""

    id: uuid.UUID = Field(description="用户ID")
    created_at: datetime | None = Field(default=None, description="创建时间")

    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)


class TokenResponse(BaseModel):
    """登录成功返回的 token 信息"""

    access_token: str = Field(description="JWT token，请求头带 Authorization: Bearer <token>")
    token_type: str = Field(default="bearer", description="token 类型")
    expires_in: int = Field(description="有效期，单位秒")
    user: UserResponse | None = Field(default=None, description="登录用户信息")
