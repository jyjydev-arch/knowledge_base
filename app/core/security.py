from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from passlib.context import CryptContext

from app.core.config import settings

# 使用 bcrypt 算法进行密码哈希
# 注意: bcrypt 只取密码前 72 字节，所以 schema 里限制了密码长度
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def get_password_hash(password: str) -> str:
    """把明文密码变成哈希值"""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """校验明文密码和哈希值是否匹配"""
    if not plain_password or not hashed_password:
        return False
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except ValueError:
        # 哈希串格式不对（比如手工往库里塞了明文）时当成校验失败处理
        return False


def create_access_token(
    subject: str | Any,
    expires_delta: timedelta | None = None,
) -> str:
    """生成 JWT Token，subject 一般传用户 id"""
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode = {"exp": expire, "sub": str(subject), "iat": datetime.now(timezone.utc)}
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    """解析 JWT Token

    合法返回 payload，签名错误 / 过期 / 格式非法统一返回 None，
    由调用方决定给前端什么提示。
    """
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except jwt.PyJWTError:
        return None


def token_expire_datetime(expires_delta: timedelta | None = None) -> datetime:
    """算出 token 的过期时间点，登录时写进 token 表用"""
    delta = expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return datetime.now(timezone.utc) + delta
