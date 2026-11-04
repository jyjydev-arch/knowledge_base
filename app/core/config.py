"""全局配置

所有配置都可以写进 .env（或环境变量），字段名大小写不敏感。
DATABASE_URL 兼容旧写法 PSQL_URL；两者都不填时使用下面的默认值，
这样在还没配置数据库时项目依然可以被 import / 生成 OpenAPI 文档。
"""

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 默认连接串：postgresql+asyncpg://用户名:密码@主机:端口/库名
DEFAULT_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/knowledge_base"
DEFAULT_SECRET_KEY = "dev-only-secret-key-please-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------------- 应用信息 ----------------
    APP_NAME: str = "AI知识库系统"
    APP_DESCRIPTION: str = "这是一个ai知识库系统"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"

    # ---------------- 数据库 ----------------
    DATABASE_URL: str = Field(
        default=DEFAULT_DATABASE_URL,
        validation_alias=AliasChoices("DATABASE_URL", "PSQL_URL"),
    )
    # 开发时想看打印的 SQL 就把 SQL_ECHO 设成 True
    SQL_ECHO: bool = False
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # ---------------- JWT ----------------
    SECRET_KEY: str = DEFAULT_SECRET_KEY
    ALGORITHM: str = "HS256"
    # 单位：分钟，默认 1 天
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24
    # 引导用：注册时命中这里列出的用户名，users.role 直接给 admin
    # （库里已有 role 字段，权限判断一律看 role，这个配置只负责「第一个管理员从哪来」）
    ADMIN_USERNAMES: str = "admin"

    # ---------------- 跨域 ----------------
    # 逗号分隔，例如 http://localhost:5173,http://127.0.0.1:5173
    CORS_ORIGINS: str = "*"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _origins_to_str(cls, value):
        """允许 .env 里写成 JSON 数组或者普通字符串"""
        if isinstance(value, list):
            return ",".join(str(v) for v in value)
        return value if value is not None else "*"

    @model_validator(mode="after")
    def _fill_blank_values(self):
        """.env 里写了空的 key（例如 `PSQL_URL =`）时，回退到默认值而不是报错"""
        if not self.DATABASE_URL or not self.DATABASE_URL.strip():
            self.DATABASE_URL = DEFAULT_DATABASE_URL
        if not self.SECRET_KEY or not self.SECRET_KEY.strip():
            self.SECRET_KEY = DEFAULT_SECRET_KEY
        return self

    @property
    def admin_usernames(self) -> set[str]:
        return {name.strip() for name in self.ADMIN_USERNAMES.split(",") if name.strip()}

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()
