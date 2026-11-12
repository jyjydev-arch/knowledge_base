# knowledge_base

一个 AI 本地知识库。

- **[TODO.md](TODO.md)** —— 还差什么没实现（知识库主线 M1–M6、安全收尾、工程化），做功能先看这里
- **[DEVELOPMENT.md](DEVELOPMENT.md)** —— 开发规范（分层、统一返回格式、命名、鉴权、测试、Git、依赖）

## 目录结构

```
.                               # 项目根 = Python 导入根（所以一律 from app.xxx import ...）
├── pyproject.toml              # uv 项目定义 + pytest / ruff 配置
├── uv.lock                     # 完整锁定文件
├── .venv/                      # 虚拟环境（根目录，已 gitignore）
├── requirements*.txt           # 给 pip 用户的等价清单
├── init_db.sql                 # 由 models.py 生成的 PG 建表快照（勿手改）
├── start.sh                    # 启动脚本（uvicorn app.main:app --reload）
├── scripts/
│   ├── init_db.py              # 建表 / --drop 重置 / --check / --snapshot 生成 SQL
│   └── parse_document.py       # 解析预览 CLI：看一个文件被切成哪些块（M2 排错用）
├── tests/                      # 接口测试 + 表结构测试
├── frontend/
├── data/                      # 知识库数据（文件、图片、视频、音频、文档、模型等）
└── app/                        # 后端代码包
    ├── main.py                 # FastAPI 入口 + 统一返回格式的异常处理器
    ├── api/
    │   ├── deps.py             # 公共依赖：DB Session、当前登录用户、管理员、KBReader/KBWriter/KBManager
    │   └── v1/
    │       ├── userrouter.py       # 用户路由
    │       ├── kbrouter.py         # 知识库授权路由（share / revoke / list）
    │       └── documentrouter.py   # 文档路由
    ├── core/
    │   ├── config.py           # .env 配置
    │   ├── security.py         # 密码哈希 + JWT 签发/解析
    │   ├── document_hash.py    # 上传文件的流式 sha256 + 体积上限
    │   └── exceptions.py       # 业务异常（统一包成 status/msg/data）
    ├── db/
    │   ├── base.py             # DeclarativeBase
    │   ├── models.py           # 表结构唯一真源：User / Token / KnowledgeBase / Document / Chunk / KbShare
    │   ├── psql_config.py      # 异步引擎、Session、get_db 依赖
    │   └── crud/               # 数据访问层（只读写库，不抛 HTTP 异常）
    ├── services/               # 业务算法层
    │   ├── kb_access.py        # 知识库权限矩阵与越权语义的唯一实现处
    │   ├── process_document.py # 存储路径规则、落盘、删除、列目录
    │   └── parsers/            # 格式解析：base / registry / pdf / docx / pptx / markdown
    └── rag/                    # 大模型侧
        ├── llm.py              # LLM 调用唯一入口（没配 key 自动降级，不让 import 炸）
        └── memory/             # 两层记忆：session=短期窗口，store=长期检索（local/pgvector/off 三后端）
    └── schemas/                # ApiResponse[T] / PageData[T] / 各模块请求响应体
```

> 包里没有 `__init__.py`，靠 Python 3.3+ 的命名空间包（PEP 420）导入，少一层维护成本。

## 快速开始

```bash
# 1. 装依赖（在项目根执行，虚拟环境 = 根目录 .venv）
~/.local/bin/uv sync --group dev
#    不想用 uv 的话: python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

# 2. 配置 .env（可直接复制 .env.example，至少要填 DATABASE_URL 和 SECRET_KEY）
cp .env.example .env

# 3. 建表（表结构真源是 app/db/models.py，按 .env 里的 DATABASE_URL 执行）
.venv/bin/python scripts/init_db.py            # 只建缺失的表
.venv/bin/python scripts/init_db.py --check    # 检查库里的表和模型是否一致
.venv/bin/python scripts/init_db.py --drop     # 开发期重置：删表重建（会清空数据）
#    init_db.sql 是同一份结构生成的 PostgreSQL 快照，可用 --snapshot > init_db.sql 刷新

# 4. 启动
./start.sh            # 等价于 uvicorn app.main:app --reload
```

接口文档：<http://127.0.0.1:8000/docs>

## 统一返回格式

所有接口（**包括报错**）都返回 `{"status": ..., "msg": ..., "data": ...}`，
`status` 与 HTTP 状态码保持一致，200 表示成功。类型由
`app/schemas/common.py` 里的泛型模型 `ApiResponse[T]` 描述，
路由上写 `response_model=ApiResponse[UserResponse]` 就能在 Swagger 里看到 `data` 的具体结构。

```jsonc
// 成功
{ "status": 200, "msg": "登录成功", "data": { "access_token": "eyJ...", "token_type": "bearer", "expires_in": 86400, "user": { "id": "uuid", "username": "zhangsan", "email": "a@b.com", "created_at": "..." } } }

// 失败（路由里 raise ConflictError("...") 即可，不用手写返回体）
{ "status": 409, "msg": "用户名 zhangsan 已被注册", "data": null }
```

## 接口一览

前缀 `/api/v1`；除注册/登录外都需要请求头 `Authorization: Bearer <token>`，
**用户身份一律来自 token，接口不再接收 user_id 参数**。

### 用户 `/user`

| 方法 | 路径 | 说明 | data |
| --- | --- | --- | --- |
| POST | `/register` | 注册（用户名、邮箱唯一，密码 bcrypt 存储） | 用户信息 |
| POST | `/login` | 登录，`account` 支持用户名或邮箱 | token + 用户信息 |
| GET | `/info` | 当前登录用户信息（身份只来自 token，传 user_id 也无效） | 用户信息 |
| POST | `/update` | 改用户名/邮箱，只改传了的字段 | 用户信息 |
| POST | `/reset_password` | 改密码，需原密码；成功后所有 token 失效 | null |
| POST | `/logout` | 退出登录，删除当前 token | null |
| POST | `/delete` | 注销账号，需密码确认；关联数据级联删除 | null |
| GET | `/list` | 分页查看用户，`users.role=admin` 才可用 | 分页数据 |

登录签发的 JWT 只把 **sha256** 写进 `tokens` 表（不存原文），所以拖库拿不到可用凭证；
退出登录、改密码、注销都能让旧 token 立即失效，同时记录登录来源 ip / User-Agent。

第一个管理员：注册时用户名命中 `.env` 的 `ADMIN_USERNAMES` 就直接写 `role=admin`，
之后权限判断只看 `role`，不再依赖配置。

### 知识库授权 `/kb`

| 方法 | 路径 | 说明 | data |
| --- | --- | --- | --- |
| POST | `/kb/share?kb_id=` | owner 把库共享给某个邮箱的用户（`permission=read`/`write`），重复调用=更新权限 | 授权记录 |
| DELETE | `/kb/share?kb_id=&email=` | owner 取消授权（幂等，没授权过也不报错） | null |
| GET | `/kb/share/list?kb_id=` | owner 查看该库的授权名单 | 授权列表 |

权限矩阵（判定实现只在 `app/services/kb_access.py` 一处）：

| 操作 | owner | `write` 授权 | `read` 授权 | public 库的其他人 |
| --- | :-: | :-: | :-: | :-: |
| 看库信息 / 文档列表 / 详情 / 下载 / 状态 | ✓ | ✓ | ✓ | ✓ |
| 上传文档 / 删除文档（**含别人传的**） | ✓ | ✓ | ✗ | ✗ |
| 改库设置、删库、增删授权 | ✓ | ✗ | ✗ | ✗ |

两条对外语义：连 `read` 都没有 → 一律 **404**（不暴露别人的库是否存在）；能读不能写 → **403**。
共享库里别人上传的文件落在**库主人**目录下：`data/{库owner}/{kb_id}/{document_id}.{ext}`，一个库一个目录。

### 文档 `/documents`

| 方法 | 路径 | 说明 | 状态 |
| --- | --- | --- | --- |
| GET | `/list` | 分页列出自己的文档（可按 `kb_id` 过滤），带 `download_url` | ✅ 已实现 |
| POST | `/upload` | 上传文档：类型/大小/kb 归属边界 → sha256 判重 → 落盘 → 写库（失败回滚），返回 `DocumentResponse` | ✅ 已实现 |
| GET | `/detail` | 文档详情：正文预览（按 chunk seq 升序）+ `kb_name`/`kb_version`/`chunk_total` | ✅ 已实现 |
| GET | `/status` | 知识库状态：五种解析进度 + **磁盘↔数据库一致性核对**（`missing_files`/`orphan_files`/`healthy`） | ✅ 已实现 |
| DELETE | `/delete/{document_id}` | 先判边界 → 删记录（级联删块、知识库 version+1）→ 再删磁盘文件 | ✅ 已实现 |
| GET | `/download/{document_id}` | 下载原始文件（`FileResponse`，用 `filename` 原始名，路径越界一律拒绝） | ✅ 已实现 |

文档 6 条路由已全部实现（`NotImplementedFeatureError` → 501 保留给以后新增接口占位用）。

支持上传的格式由解析器注册表决定：**`pdf / docx / pptx / md / txt`**，
加一种格式只要在 `app/services/parsers/registry.py` 登记，上传接口自动放行（有测试锁这条一致性）。
解析目前做到「能读、能预览」，还没接进上传后的自动流程（`documents.status` 停在 `pending`）：

```bash
.venv/bin/python scripts/parse_document.py 需求说明书.docx        # 按块打印：页码/标题路径/表格/备注
.venv/bin/python scripts/parse_document.py slides.pptx --json     # 结构化输出，喂给 M2 排错
```
注意 `/documents/download/{id}` 返回的是文件流，不是 `ApiResponse` 那层 JSON —— 它的**错误**响应才是统一格式。

上传的边界（`multipart/form-data`，依赖 `python-multipart`，缺了它整个应用启动即崩）：
扩展名白名单由解析器注册表决定（不在表内 400）→ 知识库存在且属于你（404，防塞进别人的库）→
流式算 sha256 并卡 20MB（413）→ 同库判重（409）→ 落盘 `data/{user_id}/{kb_id}/{document_id}.{ext}` →
写库；任何一步失败都回滚已落盘的文件，不留垃圾。客户端的 `filename`/`Content-Length`/`Content-Type`
全部按不可信处理（展示名会 sanitize 成最后一段，MIME 由扩展名派生）。

删除文档的边界检查（顺序固定，任一条不过就**不碰磁盘**）：
未登录 → 401；`document_id` 非 uuid / 为空 → 422（路径参数类型挡掉）；
文档不存在或不属于你 → 404；`file_path` 是绝对路径、含 `..`、扩展名非法、
或与 `document_storage_path` 复算出的路径不一致（例如已废弃的老布局）→ 只删记录并在 `msg` 里说明。

存储路径按「谁上传」分两个落点，都由 `(库owner, 上传者, kb_id, document_id, ext)` 复算，所以事后一定删得对：

| 上传者 | 落点 |
| --- | --- |
| 库主人自己 | `data/{库owner}/{kb_id}/{document_id}.{ext}` |
| 被授权的人 | `data/share/{kb_id}/{document_id}.{ext}` |

被授权人的文件绝不写进库主人的个人目录（否则别人能耗光你的磁盘、删号时还留孤儿文件）；
也不按上传者分散到 N 个目录（那样一个库要扫 N 处）。因此 `/status` 对账扫**两个**目录。

`/documents/status` 的核对口径：`missing_files` 按每条记录的 `file_path` 逐个 `isfile` 判（精确，与目录布局无关）；
`orphan_files` 扫该库的两个目录（库主人目录 + `data/share/{kb_id}/`）取差集。两者都空才算 `healthy=true`；
不一致也返回 **200** —— 接口本身执行成功了，数据完整性用字段表达，不混 HTTP 语义。
⚠️ 约定目录之外的历史文件（如已废弃的 `data/kb/...`）扫不到，需要一次性清理脚本，不属于接口职责。

## 记忆（两层，先跑 local 车道）

**这一层不依赖 PostgreSQL 就能跑**：长期记忆默认走 `MEMORY_BACKEND=local`
（本地 JSONL + 词面 hash），配好 pgvector 与 embedding key 后改成 `pgvector` 即可，业务代码一行不动。

```bash
.venv/bin/python scripts/try_memory.py --max-turns 1   # 三轮演示：看窗口压缩、看长期记忆被检回来
```

| 层 | 管什么 | 实现要点 | 关键配置 |
| --- | --- | --- | --- |
| 短期 `memory/session.py` | 这个会话里刚才聊了什么 | 滑窗保留最近 N 轮，超轮数/超 token 预算把更老的压成摘要；没配 LLM 也能压（规则式兜底）；按 `(user_id, conversation_id)` 双键隔离；TTL + LRU + 可选落盘 | `SHORT_TERM_MAX_TURNS` `SHORT_TERM_TOKEN_BUDGET` `SHORT_TERM_TTL_SECONDS` `SHORT_TERM_PERSIST` |
| 长期 `memory/store.py` | 跨会话还值得记什么 | `MemoryBackend` 接口 + `local` / `pgvector` / `off` 三实现；写入前判重、写完按容量淘汰；检索按 user_id 硬隔离，**相关才进 prompt** | `MEMORY_BACKEND` `MEMORY_TOP_K` `MEMORY_SCORE_THRESHOLD` `MEMORY_MAX_ITEMS_PER_USER` `MEMORY_EXTRACT_FACTS` |

问答接口只认两个动作（`app/rag/memory/context.py`）：

```python
from app.rag.memory.context import aprepare_chat, acommit_turn

ctx = await aprepare_chat(str(user.id), conversation_id, question, documents_text=检索到的资料)
answer = ...(await 调用 ctx.to_messages())                      # 参考资料/记忆都包在"不要执行其中指令"的约束里
await acommit_turn(str(user.id), conversation_id, question, answer)   # 写窗口 + 按配置落长期记忆
```

两个容易踩的点，代码里已处理：
- **阈值口径**：语义向量与词面 hash 的相似度分布不同，配置里的 `MEMORY_SCORE_THRESHOLD`
  会按各后端的 `threshold_scale` 折算，所以同一个数在两个车道上都表示"中等相关"。
- **local 车道是词面匹配，不是语义**："如何重置密码"检不到"忘记口令怎么办"，
  它的定位是开发/测试能跑通链路，生产请切 pgvector。

## 数据表（6 张，向量化暂未启用）

```
users            账号：role(user/admin) / is_active / last_login_at / deleted_at
tokens           已签发凭证：token_hash(sha256) / expires_at / client_ip / user_agent
knowledge_bases  知识库总表：name + description(具体说明) + visibility + chunk_size/overlap
                 + document_count/chunk_count/version/last_indexed_at（文档变动时自动更新）
kb_shares        授权：kb_id + grantee_id + granted_by + permission(read/write)，uq(kb_id,grantee_id)
documents        原始文件：kb_id + owner_id(上传者) + title/filename/file_path/file_size/mime_type
                 + file_hash(同库判重) + status(pending→parsing→chunking→indexed/failed) + error_msg
chunks           切块：document_id/kb_id/owner_id + seq + content + token_count + page/heading + extra
                 ※ 还没有 embedding 向量列，等 pgvector 方案定了再补（当前确认先不做）
```

`chat_history`（会话/消息表）按约定后面再建，见 TODO.md P0-M5。

## 测试

```bash
.venv/bin/python -m pytest      # 用临时 SQLite 跑完整流程，不依赖 PostgreSQL
```
