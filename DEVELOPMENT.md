# 开发规范（DEVELOPMENT）

> 适用于 `knowledge_base` 后端（`app/`）。前端 `frontend/` 暂不约束。
> 目标：新增模块（文档/检索/问答）时不用互相猜约定，照抄用户模块的写法即可。

## 1. 技术栈与环境

| 项 | 约定 |
| --- | --- |
| Python | **3.11+**（`app/pyproject.toml: requires-python`） |
| 依赖管理 | 以 `app/pyproject.toml` + `app/uv.lock` 为准，`uv sync --group dev`；`requirements*.txt` 只给 pip 用户，改动要同步 |
| Web | FastAPI（全异步）+ uvicorn |
| ORM | SQLAlchemy **2.0/2.1 风格**：`Mapped[...]` + `mapped_column(...)`，禁止旧 `Column()` 写法 |
| 校验 | Pydantic **v2**（`ConfigDict` / `field_validator`），禁止 v1 的 `class Config: orm_mode` |
| 数据库 | PostgreSQL + pgvector；驱动 `asyncpg`（异步必需 `greenlet`） |
| 测试 | pytest + `fastapi.testclient.TestClient` + 临时文件 SQLite（不依赖本机 PG） |
| 启动 | 项目根执行 `./start.sh`（= `uvicorn app.main:app --reload`） |
| 建表 | **`scripts/init_db.py`**（`--drop` / `--check` / `--snapshot`），表结构真源是 `app/db/models.py` |

## 2. 分层与依赖方向（只能上层调下层）

```
api（路由/依赖）  →  schemas（入参出参）  →  services（业务算法）  →  crud（读写库）  →  models（表）
      ↑ core（config / security / exceptions）被所有层共用，不反向依赖任何层
```

- **路由（`app/api/v1/*`）要薄**：只做「取参 → 调 crud/service → 组装 `ApiResponse`」。单函数超过 ~40 行、或含算法（切块、检索、prompt 拼装），抽到 `app/services/xxx.py`。
- **CRUD（`app/db/crud/*`）不抛 HTTP 异常**：查不到返回 `None`，唯一索引冲突 `except IntegrityError: await db.rollback(); return None`，由路由决定提示语。
- **service 层不碰 Session 之外的东西**：纯函数优先（chunker、prompt、解析器），方便单测。
- `app/api/deps.py` 只放依赖注入（DB/鉴权/权限），路由文件里不重复写鉴权逻辑。
- 包内**不放 `__init__.py`**（PEP 420 命名空间包），导入一律写全路径：`from app.db.crud import user_crud`。

## 3. 接口规范

### 3.1 统一返回格式（强制）
所有 JSON 接口返回 `{"status": int, "msg": str, "data": T | null}`，由 `app/schemas/common.py:ApiResponse[T]` 描述：

```python
@router.post("/register", response_model=ApiResponse[UserResponse], summary="用户注册")
async def register_user(payload: UserCreate, db: DB):
    ...
    return ApiResponse.ok(UserResponse.model_validate(user), msg="注册成功")
```

- `status` **与 HTTP 状态码保持一致**，`200` 成功；不要出现「HTTP 200 + status 500」。
- `data` 里只放业务数据；失败时为 `null`（除非要回传明细，如校验错误列表）。
- 泛型必须显式写进 `response_model`，前端要从 `/openapi.json` 生成类型。

### 3.2 失败一律 raise，不手写返回体
```python
from app.core.exceptions import ParamError, UnauthorizedError, ForbiddenError, NotFoundError, ConflictError
if user is None:
    raise NotFoundError("用户不存在")
```
`app/main.py` 的异常处理器会把 `BusinessError / RequestValidationError / 404·405 / Exception` 全部包成统一格式；路由里**不要**自己 `return ApiResponse.fail(...)`（除非确有特殊语义）。

### 3.3 状态码 / 错误码约定

| status | 场景 | msg 写法 |
| --- | --- | --- |
| 200 | 成功 | 中文动作结果，如「登录成功」「修改成功」 |
| 400 `ParamError` | 语义不合法（空更新体等） | 说清该怎么做 |
| 401 `UnauthorizedError` | 未登录 / token 无效或过期 / 密码错 | 不区分「用户不存在」与「密码错误」 |
| 403 `ForbiddenError` | 已登录但无权限（改别人数据、非管理员） | 「需要管理员权限」 |
| 404 `NotFoundError` | 资源不存在 | 「文档不存在」 |
| 409 `ConflictError` | 唯一冲突（用户名/邮箱/文件名重复） | 带上冲突字段值 |
| 413 | 上传文件超限（M1 起用） | 「文件超过 20MB 限制」 |
| 422 | Pydantic 校验失败（框架自动） | 「参数校验失败: field 原因」 |
| 429 | 限流（P1 起用） | 「操作过于频繁，请稍后再试」 |
| 500 | 兜底 | 生产只给「服务器繁忙」，细节进日志 |

`msg` 面向最终用户：中文、可直接展示、**不得包含 SQL、堆栈、文件绝对路径、他人数据**。

### 3.4 路径与方法
- 版本前缀统一 `settings.API_V1_PREFIX`（`/api/v1`），模块前缀在 `APIRouter(prefix="/document")` 里写。
- **新模块走 REST 风格**：`GET /document/list`、`GET /document/{id}`、`POST /document`、`PUT /document/{id}`、`DELETE /document/{id}`；`GET` 必须无副作用。
- 用户模块现存 `POST /update`、`POST /delete`、`POST /reset_password` 是历史遗留（非幂等动作用 POST 可以理解），**不要求现在改**，但别在新模块里继续扩散这种「GET 也能删数据」或「POST 查列表还带副作用」的写法。
- 路径内部用 `snake_case`（`reset_password`）保持一致；query 参数用 snake_case。

### 3.5 分页 / 列表
统一用 `PageParams`（`page>=1`、`1<=size<=100`）+ `PageData[T]`（`items/total/page/size`）：
```python
async def list_documents(db: DB, user: CurrentUser, page: int = 1, size: int = 20):
    params = PageParams(page=page, size=size)
    items, total = await document_crud.list_documents(user.id, params.offset, params.limit, db)
    return ApiResponse.ok(PageData[DocumentOut](items=[...], total=total, page=params.page, size=params.size))
```
crud 返回 `tuple[Sequence[Model], int]`（数据 + 总数），不要各写各的。

### 3.6 例外：流式接口
LLM 流式回答（SSE / `text/event-stream`）**不能**套 `ApiResponse` 外层，改为每条事件一行 JSON：`{"status":200,"msg":"chunk","data":{"text":"..."}}`，结束发 `{"status":200,"msg":"done","data":{"references":[...]}}`；并在该路由上设 `response_class=StreamingResponse`、`response_model=None`，避免 FastAPI 试图校验。

### 3.7 新增接口 checklist
1. `app/schemas/{module}_schemas.py` 定 `XxxCreate/XxxUpdate/XxxOut`
2. `app/db/models.py` 加/改表 → 同步 alembic 迁移（或 `init_db.sql`）
3. `app/db/crud/{module}_crud.py` 写查询（返回 `None`/值，不抛 HTTP 异常）
4. `app/services/{module}.py` 放业务算法（可选）
5. `app/api/v1/{module}router.py` 加路由 + `response_model=ApiResponse[...]` + 中文 `summary`
6. `app/main.py` `include_router`（新模块时）
7. `tests/test_{module}_api.py` 成功 + 每种失败各一条 + 越权一条
8. `app/.venv/bin/python -m pytest` 全绿；`/docs` 里格式正确

## 4. Schema（Pydantic v2）规范

- 请求体一律显式模型，禁止裸 `dict` 入参；`Field(..., description=...)` 必填（进 Swagger）。
- 响应模型必须 `model_config = ConfigDict(from_attributes=True)`，用 `XxxOut.model_validate(orm_obj)` 转换，**不要在路由里手拼 dict**。
- 敏感字段永不出现：`hashed_password` 等只能在 `models` 里；需要可选返回时用 `model_dump(exclude={...})`。
- 密码长度 **6–72**（bcrypt 只用前 72 字节）；用户名 `pattern=USERNAME_PATTERN`；邮箱用 `EmailStr`（依赖 `email-validator`）。
- 只改部分字段的更新用 `model_dump(exclude_unset=True)` 判断「传了什么」。
- 字段别名兼容（如登录 `account`/`username`）用 `model_validator(mode="after")`，别用 `mode="before"` 读未解析字段。

## 5. CRUD / 数据库规范

表一览：`users` / `tokens` / `knowledge_bases` / `documents` / `chunks`（`chat_history` 还没建）。

- **表结构只有一个真源：`app/db/models.py`**。改完必须做两件事：
  ```bash
  app/.venv/bin/python scripts/init_db.py --drop        # 开发期重建（会清数据）
  app/.venv/bin/python scripts/init_db.py --snapshot > init_db.sql   # 刷新那份 SQL 快照
  ```
  `init_db.sql` 是生成物，别手改；它只 `IF NOT EXISTS`，不会给已有表加列。
- **uuid 传参**：`sa.Uuid` 在 SQLite 上只接受 `uuid.UUID` 对象，传 str 会报 `'str' object has no attribute 'hex'`。
  接口入参一律在 crud 里先过 `_to_uuid()`（非法返回 `None`，绝不拼进 SQL）；测试里直接构造 ORM 对象时记得转 `uuid.UUID`。
- **owner_id 冗余**：`documents/kb/chunks` 都带 `owner_id`，鉴权过滤只加一个 WHERE，不靠 join 父表；
  新增用户数据表时照这个做，并且**每条查询都要带上它**（越权是这类系统最容易出的洞）。
- **凭证不落明文**：`tokens.token_hash = sha256(JWT)`，别改库存 JWT 原文。
- **状态用常量**：`UserRole / KbVisibility / DocStatus`（`app/db/models.py` 里的 `StrEnum`），
  列类型是 varchar，不要在 PG 上 `CREATE TYPE ... AS ENUM`（改值太痛）；状态流转 `pending → parsing → chunking → indexed / failed`。
- **统计与版本**：文档增删后必须调 `document_crud.refresh_kb_stats(kb_id, db)`（重算 document_count/chunk_count、`version+1`、`last_indexed_at`），别让知识库的版本号和真实内容脱节。
- 参数顺序固定：**业务参数在前，`db: AsyncSession` 永远是最后一个位置参数**（`get_user_by_id(user_id, db)`、`list_documents(owner_id, db, offset, limit, kb_id=...)`）。
- 只用 `select()` + 参数化，禁止拼接 SQL 字符串；`like`/`ilike` 的值要转义。
- `commit/rollback` 只发生在 crud 层；路由里不 `commit`。`get_db` 已负责异常回滚与关闭。
- 异步 Session 每请求一个，不要跨请求共享；`expire_on_commit=False` 已配置，commit 后可继续读对象。
- 唯一冲突要兜底（并发注册）：`except IntegrityError: await db.rollback(); return None`。
- **多租户隔离**：任何用户数据的查询都必须带 `WHERE user_id = :uid`（文档/块/会话/检索全适用），并写一条跨用户越权测试。
- 敏感写操作可用 `select(...).with_for_update()`（见 `get_user_by_id_for_update`）。
- 表命名用复数（`users/tokens/knowledge_bases/documents/chunks`），列 `snake_case`；
  时间统一 `DateTime(timezone=True)`，`created_at` 用 `server_default=CURRENT_TIMESTAMP`，`updated_at` 用 `onupdate=func.now()`。
- UUID 主键只用 `default=uuid.uuid4`（Python 端生成），不依赖 `gen_random_uuid()`，这样 SQLite 测试和 PG 共用一份模型。
- 索引在 `__table_args__` 里声明（`Index`/`UniqueConstraint`），命名 `ix_<表>_<字段>` / `uq_<表>_<字段>`。

## 6. 鉴权与安全

- 需要登录的路由用 `current_user: CurrentUser`；管理员用 `admin: AdminUser`；需要原始 token（登出）用 `token: CurrentToken`。
- 鉴权链路固定四步：**有 header → JWT 能解析 → 用户存在 → token 未被吊销/过期**（见 `app/api/deps.py:get_current_user`）。
- 让旧 token 失效的动作：登出删单条、改密/改邮箱删该用户全部（`token_crud.revoke_all_user_tokens`）、注销级联清除。
- 密码只能 `get_password_hash` 入库；校验用 `verify_password`；日志/返回体禁止出现密码、`access_token`、`SECRET_KEY`。
- `SECRET_KEY` 生产用随机值（`python -c "import secrets;print(secrets.token_urlsafe(48))"`）；生产 `DEBUG=False`（`DEBUG=True` 时 500 会把异常细节回给客户端）、`CORS_ORIGINS` 收敛成具体域名。
- 上传文件：白名单扩展名 + MIME 双校验、大小限制、文件名 sanitize、落盘路径不含用户输入、存储目录不可执行。

## 7. 配置规范

- 新配置项必须同时改三处：`app/core/config.py`（字段+默认值）、`.env.example`（注释说明）、`README.md`（若是用户可感知开关）。
- 不在代码里硬编码密钥、URL、模型名、维度、阈值；魔法数字要提成模块常量并注释来由（如 `PASSWORD_MAX_LENGTH = 72`）。
- `.env` 永不提交（`.gitignore` 已含）；`Settings` 允许空值回退默认，保证「没配数据库也能 import 起服务/生成文档」。

## 8. 代码风格

- 类型注解必写（函数签名 + 返回值）；`Optional` 用 `X | None`。
- 行宽 ~120；双引号；导入顺序：标准库 → 第三方 → `app.*`（组间空行）。
- docstring 用中文一句话说明「做什么」，复杂设计补一句「为什么」（参考 `app/schemas/common.py`、`app/db/crud/token_crud.py` 的头注释）。
- 提交前建议 `ruff check app tests` + `ruff format`（未接 CI 前先本地跑）。
- 注释与命名解释意图，不解释语法；TODO 必须写清条件，例：`# TODO(M4): 混合检索加 tsvector`。

## 9. 测试规范

- 文件 `tests/test_{module}_api.py`，公共夹具在 `tests/conftest.py`（临时文件 SQLite + `dependency_overrides[get_db]`，每个用例一个库）。
- 夹具速查：`client`（TestClient）、`register/login`、`user_headers/admin_headers/other_headers`、
  `logged_in`（返回 `{"headers":..., "user_id":...}`）、`engine`、`run`（直接在 ORM 上跑一段异步代码造种子数据）。
- `engine` 夹具已经开了 `PRAGMA foreign_keys=ON`（SQLite 默认关），所以级联删除行为跟 PG 一致，别在测试里再关。
- **撞唯一索引之后那个 session 就废了**（`PendingRollbackError`），预期失败的用例要拆成独立的 `run(...)` 调用。
- 建数据用 `tests/test_schema_models.py` 里的 `_add_user/_add_kb/_add_doc`，接口测试从 `logged_in` 拿 user_id，别手抄 uuid。
- 每条路由至少覆盖：1 条成功 + 1 条最相关失败（401/403/404/409/422）+ 越权访问他人数据。
- **所有断言走 `assert_envelope(resp, status)`**（`tests/test_user_api.py`），确保统一格式不被破坏：键必须恰好是 `status/msg/data`。
- 服务层（chunker、prompt、retriever、解析器）写纯单测，不建 HTTP；涉及 embedding/LLM 时用假实现注入，测试不联网、不烧钱。
- 命令：`app/.venv/bin/python -m pytest`（裸 `pytest` 依赖根目录 `pytest.ini` 的 `pythonpath=.`）。
- 已知坑：`aiosqlite` 线程在受限沙箱里会挂起（宿主机正常）；`app/.venv` 是 uv 建的、里面没有 pip。

## 10. Git 规范

- 分支：`main` 保持可用；功能分支 `feat/document-upload`、修复 `fix/login-race`。
- 提交信息用 Conventional Commits，中文描述可：
  ```
  feat(document): 文档上传与解析入库

  - POST /document/upload 支持 pdf/docx/md/txt，大小与类型校验
  - Document.status 流转 pending→parsing→indexed
  - 新增 documents 缺失列与 alembic 迁移
  ```
  类型：`feat` / `fix` / `refactor` / `docs` / `test` / `chore`；标题 ≤50 字、一句话说清做了什么，正文列要点。
- **一次 commit 只做一件事**，且必须让 `pytest` 全绿（不要提交已知红灯）。
- 禁止提交：`.env`、`data/`、真实业务文档、`app/.venv/`、`frontend/node_modules/`、模型权重、`__pycache__`。
- push 前自检：`git status` 干净 → `pytest` 绿 → `/docs` 看一眼返回格式 → 更新 `TODO.md` 勾选框。
- 本项目 SSH 走 `ssh.github.com:443`（`~/.ssh/config`），remote 用 `git@github.com:<owner>/<repo>.git`。

## 11. 加依赖的规则

- 先在 `app/pyproject.toml` 的 `dependencies` / `[dependency-groups].dev` 加约束，再 `uv sync`，最后同步 `requirements.txt`（运行时）或 `requirements-dev.txt`（开发/测试）。
- 重量级依赖（`torch`、`sentence-transformers`、`unstructured`）单独评估：能否用 API 方案替代、装不进 CI 时的降级路径、是否放到 `[project.optional-dependencies].local`。
- 已知版本约束：`bcrypt>=4.0.1,<4.1`（passlib 1.7.4 认不了 4.1+ 的版本号）；SQLAlchemy 异步必须有 `greenlet`；TestClient 必须有 `httpx`。

## 12. 现状与已知不一致（新增时别再抄）

| 位置 | 情况 | 处理建议 |
| --- | --- | --- |
| `userrouter.py` | `POST /update·/delete·/reset_password` 非 REST | 历史遗留，保留兼容；新模块用 REST（`/documents` 已在用 `DELETE`） |
| `documentrouter.py` | `GET /detail`、`GET /status` 用 query 参数、`DELETE /delete/{id}` 路径重复动词 | M1 实现时收敛成 `GET /documents/{id}`、`DELETE /documents/{id}` |
| `documentrouter.py` | 除 `/list` 外都是 501 占位；`document_crud` 已备好对应函数 | 按 TODO.md M0/M1 填实现，函数签名别再改一遍 |
| `app/services/process_document.py` | 只有 `import os`，空壳 | M2 解析/切块的落点，删不删随你 |
| `main.py` | 无日志、无请求 ID、无 429 限流 | P1 工程化补齐 |
| `chunks.embedding` | 还没有向量列（按要求先不做 pgvector） | M3 再加列 + HNSW 索引，并回填 `knowledge_bases.embedding_model/embedding_dim` |
| 已修复（别当坑抄） | `tokens` 存 JWT 原文 → 改 sha256；`get_optional_user` 漏校验吊销 token；`/user/info?user_id=` 越权面；`init_db.sql` 与 ORM 漂移 | — |
