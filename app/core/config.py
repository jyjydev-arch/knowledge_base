"""全局配置

所有配置都可以写进 .env（或环境变量），字段名大小写不敏感。
DATABASE_URL 兼容旧写法 PSQL_URL；两者都不填时使用下面的默认值，
这样在还没配置数据库时项目依然可以被 import / 生成 OpenAPI 文档。
"""

from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 默认连接串：postgresql+asyncpg://用户名:密码@主机:端口/库名
DEFAULT_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/knowledge_base"
DEFAULT_SECRET_KEY = "dev-only-secret-key-please-change-me"

# 占位 key 的样子：your_api_key_here / sk-xxxx / changeme 这类。
# 认成「未配置」很重要 —— 否则开发期每个请求都真去连一次远端、拿一个 401，
# 慢且 noisy，还容易被误判成"网络或模型有问题"。
_PLACEHOLDER_HINTS = ("your_", "_here", "placeholder", "changeme", "xxxx")


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
    # 环境变量只认 APP_DEBUG：裸 DEBUG 是个被用烂的名字（node 的 debug 包、各种 shell 都设过，
    # 本机就有 DEBUG=release），字段名直接叫 DEBUG 会被外面的值污染成非法布尔 → 应用起不来。
    DEBUG: bool = Field(default=True, validation_alias="APP_DEBUG")
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

    # ---------------- LLM（对话模型）----------------
    # 对话模型和向量模型**通常不是同一家**：DeepSeek 只提供 chat，没有 embedding 接口，
    # 长期记忆的向量要么另配一家（硅基流动 / 通义 / OpenAI），要么本地跑一个。
    # 两个字段都留了旧名字的别名（OPENAI_API_KEY / OPENAI_BASE_URL），app/rag 里沿用老写法也能读到。
    LLM_API_KEY: str = Field(
        default="",
        validation_alias=AliasChoices("LLM_API_KEY", "OPENAI_API_KEY"),
    )
    # 端点的旧名字有三种写法，全都认：.env 里现在用的是 OPENAI_API_BASE_URL
    LLM_BASE_URL: str = Field(
        default="https://api.deepseek.com/v1",
        validation_alias=AliasChoices("LLM_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE_URL"),
    )
    LLM_MODEL: str = "deepseek-chat"
    LLM_TEMPERATURE: float = 0.2
    LLM_TIMEOUT_SECONDS: int = 60
    EMBEDDING_API_KEY: str = Field(
        default="",
        validation_alias=AliasChoices("EMBEDDING_API_KEY", "OPENAI_EMBEDDING_API_KEY"),
    )
    EMBEDDING_BASE_URL: str = Field(
        default="",
        validation_alias=AliasChoices("EMBEDDING_BASE_URL", "OPENAI_EMBEDDING_BASE_URL"),
    )
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMS: int = 1536             # 一旦建过库就不能改，改维度要重建 collection

    # ---------------- 长期记忆（跨会话，按 user_id 隔离）----------------
    # local    = 本地 JSONL + 词面 hash 向量：开发/测试车道，不联网、不建库、不装 pgvector
    # pgvector = langchain_postgres.PGVector：生产车道，需要 PG 库 + pgvector 扩展 + embedding 服务
    # off      = 全链路 no-op（灰度或不想让记忆进 prompt 时用）
    MEMORY_BACKEND: Literal["local", "pgvector", "off"] = "local"
    MEMORY_DIR: str = "data/memory"
    MEMORY_TOP_K: int = 3
    MEMORY_SCORE_THRESHOLD: float = 0.5    # 低于这个相似度不进 prompt；调高会漏，调低会混噪音
    MEMORY_DEDUP_THRESHOLD: float = 0.95   # 高于这个相似度不再重复写
    MEMORY_MAX_ITEMS_PER_USER: int = 500   # 每用户保留条数上限，超出按最旧淘汰
    MEMORY_AUTO_WRITE: bool = True         # 每轮问答后是否自动落长期记忆
    MEMORY_EXTRACT_FACTS: bool = True      # True=用 LLM 抽「用户事实」再写；False=写一轮对话的摘要
    MEMORY_PG_COLLECTION: str = "long_term_memory"

    # ---------------- 短期记忆（单个会话内的上下文窗口）----------------
    SHORT_TERM_ENABLED: bool = True
    SHORT_TERM_MAX_TURNS: int = 6          # 一轮 = 一问一答；超出就把最老的轮次压进摘要
    SHORT_TERM_TOKEN_BUDGET: int = 2000    # 窗口 token 预算，和轮数哪个先到就压哪个
    SHORT_TERM_SUMMARY_MAX_CHARS: int = 400
    SHORT_TERM_TTL_SECONDS: int = 4 * 60 * 60   # 会话闲置多久算过期（过期即丢窗口）
    SHORT_TERM_MAX_SESSIONS: int = 512          # 进程内最多缓存多少个会话，超出按 LRU 淘汰
    SHORT_TERM_PERSIST: bool = False            # True 时把窗口落 data/memory/sessions/，重启不丢
    SHORT_TERM_USE_TIKTOKEN: bool = False       # 默认用字符启发式：tiktoken 首次要联网下载词表

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
        for name, default in (
            ("LLM_BASE_URL", "https://api.deepseek.com/v1"),
            ("LLM_MODEL", "deepseek-chat"),
            ("EMBEDDING_MODEL", "text-embedding-3-small"),
            ("MEMORY_DIR", "data/memory"),
            ("MEMORY_PG_COLLECTION", "long_term_memory"),
        ):
            value = getattr(self, name)
            if value is None or not str(value).strip():
                setattr(self, name, default)
        # pgvector 后端连不了 sqlite，配置写错了就自动降级，别让应用起不来
        if self.MEMORY_BACKEND == "pgvector" and self.DATABASE_URL.startswith("sqlite"):
            self.MEMORY_BACKEND = "local"
        return self

    @property
    def admin_usernames(self) -> set[str]:
        return {name.strip() for name in self.ADMIN_USERNAMES.split(",") if name.strip()}


    # ---- 兼容旧写法：app/rag/agent.py 里读的是 settings.OPENAI_API_KEY ----
    @property
    def OPENAI_API_KEY(self) -> str:
        return self.LLM_API_KEY

    @property
    def OPENAI_BASE_URL(self) -> str:
        return self.LLM_BASE_URL

    @staticmethod
    def _usable_key(value: str) -> bool:
        """非空、且不像占位符，才认为真的能拿去调远端"""
        text = (value or "").strip().lower()
        if not text:
            return False
        if any(hint in text for hint in _PLACEHOLDER_HINTS):
            return False
        return not set(text.removeprefix("sk-")) <= {"x", "0", "-", "_"}

    @property
    def llm_ready(self) -> bool:
        """能真正调 LLM 吗（没配 key 时记忆层自动降级成规则式摘要，不报错也不白跑一次网络）"""
        return self._usable_key(self.LLM_API_KEY)

    @property
    def embedding_ready(self) -> bool:
        return self._usable_key(self.EMBEDDING_API_KEY)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()
