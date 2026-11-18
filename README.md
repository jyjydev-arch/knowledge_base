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
│   ├── parse_document.py       # 解析预览 CLI：看一个文件被切成哪些块（M2 排错用）
│   ├── index_documents.py      # 文档向量化入库 CLI（pgvector 车道，按 document_id 幂等）
│   └── try_memory.py           # 记忆层自检 CLI（不建库不联网也能跑）
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
    │       ├── documentrouter.py   # 文档路由（上传/列表/详情/下载/删除/状态）
    │       └── aichatrouter.py     # 对话路由（会话 CRUD + SSE 问答 + 停止 + agent 目录）
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
    │   ├── process_document.py # 存储路径规则、落盘、删除、解析→切块→入库编排
    │   ├── chunker.py          # 切块纯函数：按 heading 分组 + 滑窗（长度取 kb.chunk_size）
    │   ├── vector_index.py     # 把 chunks 向量化写入 pgvector（按 document_id 幂等）
    │   ├── text_metrics.py     # token 估算（切块与短期窗口共用一份口径）
    │   └── parsers/            # 格式解析：base / registry / pdf / docx / pptx / markdown
    └── rag/                    # 大模型侧
        ├── llm.py              # LLM 调用唯一入口（没配 key 自动降级，不让 import 炸）
        ├── chat.py             # 会话**存取**层：chat_sessions/chat_messages 是唯一存储
        ├── answer.py           # 一轮问答的**编排**：范围→检索→上下文→模型→落库（流式/一次性两条车道）
        ├── retrieval.py        # 检索层（M4 第一版：关键词召回 + 词频算分，形状与向量版一致）
        ├── generation.py       # 在跑的生成注册表（/stop 靠它；进程内，多 worker 有局限）
        ├── output/             # SSE 分帧 + 结构化输出（逐字流式与结构化输出互斥）
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

### 启动检查与 `/health`

应用**自己不会建表**（表结构真源仍是 `app/db/models.py` + `scripts/init_db.py`），所以启动时会检查一次：

| 情形 | 行为 |
| --- | --- |
| 表齐 | 日志 INFO，正常启动 |
| 缺表 / 连不上 | 默认**只告警不阻断**，但 `/health` 返回 **503** 并列出缺哪几张表 |
| `DB_STRICT_STARTUP=True` | 缺表直接拒绝启动（生产/Docker 建议打开：让编排系统第一时间发现） |
| `AUTO_CREATE_TABLES=True` | 启动即建表 —— **只给单人开发用**，正式环境该走迁移 |
| 内存库（测试车道 `:memory:`） | 跳过检查，`/health` 返回 200 并带 `database.ephemeral=true` |

```bash
curl -s localhost:8000/health | head -c 200      # 探库带超时（HEALTH_CHECK_TIMEOUT_SECONDS，默认 2s）
```
`/health` 是免鉴权接口，所以响应里**只有类别信息**（方言、缺表清单、错误类型），
连接串、用户名密码、异常原文一律只进日志。

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

上传之后**解析/切块是异步的**：接口立刻返回 `status=pending`，后台任务把状态推到
`indexed` 或 `failed`（失败原因在 `error_msg`），前端轮询 `GET /documents/status` 就行。
想关掉自动索引（比如以后换成 worker）就设 `DOCUMENT_INDEX_ON_UPLOAD=False`；
手动重跑用 `scripts/index_documents.py` 或将来补的 reindex 接口，重跑**幂等**
（切块按 document_id 先删后插，向量同理），不会越跑越多。

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

### 对话 `/aichat`（8 条，已接入）

| 方法 | 路径 | 说明 | data |
| --- | --- | --- | --- |
| POST | `/conversations` | 新建会话；`kb_id` 可把问答范围限定在某个库（必须你读得到，否则 404） | 会话信息 |
| GET | `/conversations` | 会话列表，按**最后发言时间**倒序，支持 `keyword` 标题搜索与分页 | 分页数据 |
| PUT | `/conversations/{conversation_id}` | 重命名（别人的会话 404） | 会话信息 |
| DELETE | `/conversations/{conversation_id}` | 删除会话，消息外键级联；会先取消该会话在跑的生成 | null |
| GET | `/conversations/{conversation_id}/messages` | 历史消息，按 seq 升序 + `before_seq` 游标翻页 | 会话 + 消息页 |
| POST | `/conversations/{conversation_id}/chat` | 一轮问答：默认 SSE 逐字流；`stream=false` 一次性 JSON | 流 / 回答体 |
| POST | `/conversations/{conversation_id}/stop` | 停止生成，**保留已生成的半截答案**并正常收尾 | 停止结果 |
| GET | `/agents` | 可选 agent 目录 + 当前配置下能不能用（M8 前是配置常量） | agent 列表 |

问答这条链路的四个要点：

1. **检索范围来自权限，不来自请求体**。会话限定了 `kb_id` 就只查那个库（授权被收回后查不到，
   但历史消息保留）；没限定则在「你读得到的全部库」里查（自己的 + 被共享的 + public）。
   没有「查全库」这种默认值 —— 越权是这类系统最常见的洞。
2. **检索目前是关键词车道**（`app/rag/retrieval.py`：DB 里 LIKE 召回 + 词频算分 + 每篇文档限流 2 块）。
   embedding 厂商与维度还没定，所以先不上向量；返回形状（`text` + `sources` + `mode`）按向量版定，
   换实现时路由和前端一行都不用动。`mode` 会如实说明为什么没资料：
   `keyword`（命中）/ `none`（没命中）/ `no_scope`（没有可读库）/ `disabled`（开关关了）。
3. **流式的错误分两段**。推流之前（会话不存在、模型没配置、该会话已有回答在生成）走正常 HTTP 状态码；
   推流之后只能 200 + `error` 帧，异常原文进日志、对外只给 `trace_id`。
4. **收尾落库用新开的 Session**（依赖 `get_session_factory`）：SSE 生成器活得比请求函数久，
   而且不该让一条数据库连接陪跑几十秒的模型流。

```bash
CID=$(curl -s -X POST $API/api/v1/aichat/conversations -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"title":"报销咨询"}' | jq -r .data.id)

# 流式：-N 关掉 curl 的缓冲，否则看起来像"一次性蹦出来"
curl -N -X POST $API/api/v1/aichat/conversations/$CID/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"报销流程是什么","stream":true,"top_k":4}'

# 不要 SSE 的时候（脚本 / CI 冒烟 / 手机端兜底）
curl -s -X POST $API/api/v1/aichat/conversations/$CID/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"报销流程是什么","stream":false}'
```

SSE 的帧（协议细节见 DEVELOPMENT §3.6）：

```
event: meta         {"status":200,"msg":"start","data":{"session_id":...,"retrieval":{"mode":"keyword","hits":1}}}
event: token        {"status":200,"msg":"chunk","data":{"text":"报销"}}
event: references   {"status":200,"msg":"references","data":{"items":[{"index":1,"document_id":...,"page":3}]}}
event: done         {"status":200,"msg":"done","data":{"text":"全文"}}
event: error        {"status":500,"msg":"生成中断，请重试","data":{"trace_id":"a1b2c3d4","retryable":true}}
```

> ⚠️ `/stop` 靠进程内注册表：多 worker 部署时 stop 请求必须落到同一个进程才生效（排期见 TODO M9）。

---

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

## 数据表（8 张）

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

```
chat_sessions      会话头：user_id + kb_id(可空，删库置 NULL) + title + summary + summary_seq + 计数
chat_messages      消息：session_id(级联删) + user_id(冗余，鉴权只认它) + role + content + seq + token_count + sources
```

两张会话表现在被三处用到：`app/rag/chat.py`（存取）、`app/rag/answer.py`（一轮问答的编排）、`app/api/v1/aichatrouter.py`（8 条路由，已 include）。对话页可以直接联调。
`summary_seq` 是**摘要水位线**：`seq <= summary_seq` 的消息已经折进 `summary` 了，
下次压缩从这条线之后开始 —— 没有它，同一段对话会被反复折叠、摘要越滚越长。

## 测试

```bash
.venv/bin/python -m pytest      # 用临时 SQLite 跑完整流程，不依赖 PostgreSQL
```

当前 **321 passed / 11 文件**：33 用户 · 48 文档 · 33 对话接口 · 28 会话服务 · 25 切块入库 · 18 共享授权 · 15 解析 · 68 记忆 · 27 输出 · 20 启动健康 · 6 表结构。

对话接口的测试**不出网**：模型只从 `answer.get_answer_model()` 取，测试把它换成假模型，于是 SSE 分帧、消息转换、检索、落库都是真链路；长期记忆换成 `NullBackend`，避免测试往 `data/memory/` 写文件。

> 仍然**没有真 PG 跑通过的证据**：全部用例都是临时 SQLite，`chat_messages` 的级联删除靠 `PRAGMA foreign_keys=ON` 模拟。建库那步别省（TODO 的 M-环境）。
