# 待实现清单（TODO / Gap Analysis）

> 更新时间：2026-11-04（表结构已重做）。配套规范见 [DEVELOPMENT.md](DEVELOPMENT.md)。

## 0. 进度快照

| 模块 | 状态 | 代码位置 |
| --- | --- | --- |
| 用户系统（注册/登录/登出/资料/改密/注销/管理列表） | ✅ 已实现（48 条测试全绿） | `app/api/v1/userrouter.py`、`app/db/crud/user_crud.py` |
| 统一返回格式 + 全局异常包装 | ✅ 已实现 | `app/schemas/common.py`、`app/main.py` |
| JWT 鉴权 + token 落库（可吊销） | ✅ 已实现 | `app/api/deps.py`、`app/db/crud/token_crud.py` |
| 文档模块 | ✅ 6 条路由全部实现：`/list`、`/upload`、`/detail`、`/status`（磁盘↔数据库核对）、`DELETE /delete/{id}`、`/download/{id}` | `app/api/v1/documentrouter.py` |
| 文档解析（M2 前半） | ✅ 四种格式 + 注册表 + 预览 CLI（`scripts/parse_document.py`），15 条测试 | `app/services/parsers/` |
| 切块 + 解析入库（M2 后半） | ❌ 未做：解析结果还没接进上传流程，`documents.status` 停在 pending | `app/services/process_document.py`、待建 `chunker.py` |
| 语义检索 / RAG 问答 | ❌ 未实现。仅有草案：`app/api/v1/aichatrouter.py`（9 条路由全 pass，未 include）、`app/rag/agent.py`（导入期就读到不存在的 settings 字段，会 AttributeError） | 待建 `app/api/v1/searchrouter.py`；`conversations`/`messages` 与 `chat_history` 表按要求后面再建 |
| 记忆层（M5 前半） | ✅ 短期窗口 + 压缩、长期双后端（local/pgvector）+ 判重与容量淘汰、拼装入口 `context.prepare_chat/commit_turn`，68 条测试；**未接进 aichatrouter（还是 pass）** | `app/rag/memory/`、`app/rag/llm.py`、`scripts/try_memory.py` |
| 知识库共享授权（read/write） | ✅ `kb_shares` 表 + 3 条路由 + `kb_access` 权限矩阵，6 条文档路由的越权口径已同步改造 | `app/api/v1/kbrouter.py`、`app/services/kb_access.py` |
| 向量库（chunks 的 embedding 列） | ⏸️ **按要求先不做**：`chunks` 还没有 embedding 列，`init_db.sql` 里的 `CREATE EXTENSION vector` 保持注释。※ 顺带更正一条旧结论：本机 PG **是有** pgvector 的（`pg_available_extensions` 里 vector 0.8.7），只是任何库里都没执行过 `CREATE EXTENSION` —— M3 不被环境卡住，卡的是库还没建 | `app/db/models.py` |
| 数据库迁移 | ⚠️ 无 alembic；已改为「ORM 单一真源」+ `scripts/init_db.py`（建表/重置/检查/生成 SQL 快照） | `scripts/init_db.py` |
| 后台任务（解析/向量化是慢活） | ❌ 无 | — |
| 日志 / 限流 / 监控 / Docker / CI | ❌ 无 | — |

**一句话：账号系统 + 5 张业务表已经就位，文档只通了「列表」这一条；解析→入库→检索→问答还没动。**

---

## 1. P0：跑通知识库主线（RAG 闭环）

> 建议按里程碑自上而下做，每个里程碑都能单独验收。

### M0 知识库（knowledge_bases）接口（约 0.5 天，先做这个）

`documents.kb_id` 是 NOT NULL，所以没有知识库接口就传不进文档 —— 这是当前最卡脖子的一环。

- [ ] `app/api/v1/kbrouter.py`：`POST /kb`（建库）、`GET /kb/list?scope=owned|shared|all`、`GET /kb/detail`、`POST /kb/update`（改 name/description/chunk 配置）、`DELETE /kb/{kb_id}`（软删 + 级联清理）
      —— 授权三条已在同一个文件里做好了，新增的 5 条直接用 `KBReader/KBWriter/KBManager` 即可
- [ ] `app/schemas/kb_schemas.py`：`KbCreate(name, description, visibility?, chunk_size?, chunk_overlap?)` / `KbOut`（含 document_count、chunk_count、**version**、last_indexed_at）
- [ ] `app/db/crud/kb_crud.py`：查询一律带 `owner_id`；`refresh_kb_stats` 已在 `document_crud` 里，直接复用
- [ ] `documentrouter` 的 upload/detail/status 接收 `kb_id` 参数（不传则用用户的默认库：`is_default` 或最早创建的那个）
- 验收：建库 → 看到 version=1 → 建文档 → version=2、document_count=1（`test_create_document_bumps_kb_version` 已覆盖 CRUD 层）

### M1 文档上传与元数据（约 1 天）
- ✅ M1 六条全部实现（含边界、判重、回滚、kb 版本联动、磁盘↔数据库一致性核对）
- ⚠️ **M0 知识库接口仍然没做，现在比之前更关键**：有了共享授权之后，被授权人**没有任何接口能列出「别人共享给我的库」**（只能靠 owner 把 kb_id 告诉他）。`GET /kb/list` 必须同时返回 owned + shared 两类并标出各自 permission
- 已完成的相邻能力：`POST/DELETE /kb/share`、`GET /kb/share/list`（owner only）、`kb_shares` 表、`KBReader/KBWriter/KBManager` 依赖
- 依赖提醒：用了 `UploadFile`/`File(...)` 就必须装 `python-multipart`（已进 pyproject + requirements.txt），
  缺它不是接口报错，而是 `import app.main` 直接失败、服务起不来
- 文件落地按约定 **`data/{user_id}/{kb_id}/{document_id}.{ext}`**，库里 `documents.file_path` 存的就是这一串相对路径；按知识库分目录后 `/status` 才能直接 ls 整个目录做一致性核对；`.gitignore` 已忽略 `/data/`
- 唯一来源 `document_storage_path(kb_owner_id, uploader_id, kb_id, document_id, ext)`；
  `new_document_file_path` / `delete_document_file` / `search_document_files` / `kb_storage_dirs` 都按 (库owner, 上传者) 复算
- 校验：类型白名单（pdf/docx/md/txt）、大小上限（默认 20MB，超限 `raise BusinessError(status=413)`）、文件名 sanitize
- `Document.status` 流转：`pending → parsing → chunking → indexed / failed`（+ `error_msg`）
- ~~补 documents 缺失列~~ ✅ 表已重做：title/file_path/file_size/mime_type/file_hash/status/error_msg/chunk_count/token_count/updated_at/deleted_at 都在，CRUD 也齐了（`create_document` 会自动 `refresh_kb_stats` 更新知识库版本）
- 验收：上传→查列表→查状态→删除→级联文件被清理；owner 隔离（A 不能看/删 B 的文档，`document_crud` 每个查询都带 `owner_id`）
- ✅ 删除部分已完成并有 9 条测试覆盖（成功/文件不存在/目录穿越/路径不一致/越权 404/非 uuid 422/重复删 404/未登录 401/服务函数单测）

### M2 解析 + 切块（约 1–2 天）—— 解析部分 ✅ 已完成，切块与入库待做

已完成（`app/services/parsers/`，15 条测试）：

| 模块 | 能力 | 已知边界 |
| --- | --- | --- |
| `base.py` | `ParsedBlock/ParsedDocument`、`clean_text` 归一化、`DocumentParseError` | docx/PDF 里的 `\x07`、no-break space 都在这层统一收 |
| `registry.py` | 扩展名 → 解析器；白名单与 MIME 唯一来源 | 先判扩展名再开文件；三方异常统一收口成 `DocumentParseError` |
| `markdown.py` | ATX/Setext 标题栈、front-matter、代码块保护，块带 `heading` 路径 | — |
| `pdf.py` | 按页出块（`page`），加密/损坏/无文字分别报错 | **扫描件/图片型 PDF 需要 OCR，还没做**；中文提取取决于源文件是否内嵌字体 |
| `docx.py` | 按 body 顺序遍历段落+表格，样式名识别 1–9 级标题 | 只支持 `.docx`，老 `.doc` 需要 LibreOffice 预转；docx 无可靠页码，`page` 留空 |
| `pptx.py` | 每页一块 + 备注单独成块（`kind=notes`）+ 表格 | 只支持 `.pptx`；纯图片页会得到空结果并报错 |

剩余（按顺序做）：

- [ ] `app/services/chunker.py`：按 `heading` 分组 → 组内滑窗切块（长度用 `kb.chunk_size/chunk_overlap`），块继承 `page/heading/extra`
- [ ] 把解析+切块接进上传后的流程：`app/services/process_document.py` 里加 `process_document(document_id)`，
      用 `registry.parse_file(document.file_path)` → `chunker` → 批量写 `chunks` → 更新 `documents.status/chunk_count/token_count`
- [ ] `documents.status` 流转真正跑起来（`pending → parsing → chunking → indexed / failed` + `error_msg`），
      失败要能重索引且幂等（先按 `document_id` 删旧块再插）
- [ ] 后台执行：先 FastAPI `BackgroundTasks`（`/upload` 里那个 `background_tasks` 参数就是留的坑），量大再换 arq/celery
- [ ] OCR 通道（扫描件）与 `.doc/.ppt` 老格式预转 —— 优先级看实际资料构成
- 落点在 `app/services/parsers/`（✅ 已建：base/registry/pdf/docx/pptx/markdown），编排入口留给 `process_document.process_document(document_id)`
- ✅ 路径约定已定：`data/{user_id}/{kb_id}/{document_id}.{ext}`，文档/注释已全部对齐（没有多余的 `documents/` 一层）。
- ✅ 上传实现时把路径拼法收敛成唯一来源：`process_document.document_storage_path(user_id, document_id, ext)`，
  落盘 / 删除 / 列目录 / 边界校验（`documentrouter._cleanup_plan`）都用它；复算失败只删记录不碰磁盘
- ⏸️ 仍先不做：`UPLOAD_ROOT` 不提配置（路径相对进程 CWD，靠 `start.sh` 在项目根启动）、上传大小限制仍是路由里的常量 20MB
- [ ] 一次性清理脚本：把已废弃布局（`data/kb/...`）里的历史文件搬回约定目录或直接删除（`/status` 扫不到它们）
- 新依赖：`pypdf`、`python-docx`、（可选）`unstructured`；统一输出纯文本 + 页码/章节，写进 `chunks.page/heading/extra`
- `app/services/chunker.py`：按段落/标题边界 + `chunk_size`(默认 500–800 token) + `overlap`(10–15%)；纯函数，先写单测再写库
- ~~新表 chunks~~ ✅ 已建好（`id/document_id/kb_id/owner_id/seq/content/token_count/page/heading/extra`），只差 `embedding vector(N)` 列 + HNSW 索引
- 验收：同一篇文档重复入库结果幂等（先删旧 chunk 再插），中文与英文样本都能切出合理块

### M3 向量化入库（约 1–2 天）⏸️ 当前明确先不做

> 你已确认 pgvector / 向量化这块先不处理，下面整段是到时候的备忘，不是本轮任务。
> 另外提醒：本机 PostgreSQL 17 **没装 pgvector 扩展**（`/usr/share/postgresql/17/extension/` 里没有 vector.control），
> 所以现在跑 `CREATE EXTENSION vector` 会直接报错；到 M3 需要先 `apt install postgresql-17-pgvector` 或改用 Docker 的 pgvector 镜像。
- `app/services/embedding.py`：接口 `async def embed(texts: list[str]) -> list[list[float]]`
  - 方案 A 本地：`sentence-transformers`（bge-m3 / text2vec-large-chinese，维度 768/1024，需要 torch，首次拉模型较大）
  - 方案 B API：OpenAI 兼容 `/v1/embeddings`（`text-embedding-3-small` 1532 维）走 `httpx`，配 `EMBEDDING_BASE_URL/API_KEY/MODEL/DIMS`
  - **维度必须写进配置**，`vector(?)` 建列和检索都依赖它，改了要重建表
- `init_db.sql` 补：`CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops)`；确认 PG 装了 pgvector ≥0.5
- 入库流程：解析→切块→批量 embedding→`INSERT chunks`→更新 `documents.status/indexed`
- 验收：一篇 10 页 PDF 全链路进库，`SELECT count(*) FROM chunks` 有数据，向量非空

### M4 检索（约 1 天）
- `app/services/retriever.py`：`search_embedding(vec, user_id, top_k)`，SQL 用 `<=>` 余弦距离 + `WHERE user_id = :uid`
- `POST /search`：`{query, top_k, min_score, document_ids?}` → `{status,msg,data:{items:[{chunk_id,document_id,content,score,...}]}}`
- 进阶：pgvector + PG 全文（`tsvector`）混合检索，再 `rerank`（bge-reranker 或 LLM rerank）；查询文本要先 embedding
- ⚠️ 越权是这类系统最常见的洞：**任何检索都必须带 `user_id` 过滤**，并把"跨用户检索"写成一条测试
- 验收：问一句能召回正确段落；B 用户搜不到 A 用户内容

### M5 问答（RAG）+ 会话（约 2 天）
- `app/services/llm.py`：`chat(messages)` 抽象，支持流式；配置 `LLM_BASE_URL/API_KEY/MODEL`
- `POST /chat`（或 `/qa`）：检索 top-k → prompt 模板（含引用编号、"资料里没有就回答不知道"）→ 调 LLM → 回传 `references`
- 流式：SSE `text/event-stream`；注意流式接口不能套 `ApiResponse` 外层，需单独约定（见 DEVELOPMENT.md 第 3.6 节）
- 新表 `chat_sessions` / `chat_messages`（存问题、答案、引用的 chunk_id 列表）
- 验收：多轮对话带上下文、答案带可点击来源、无检索结果时不瞎编

### M6 慢任务异步化（约 1 天）
- 解析+向量化很耗时：先用 FastAPI `BackgroundTasks`（最简）→ 量大再换 `arq`/`celery` + Redis
- 要求：接口立刻返回 `document_id` + `status=pending`，前端轮询 `GET /document/status`；失败可 `POST /document/{id}/reindex` 重试且幂等

---

## 2. P1：用户/安全收尾

已完成（本轮顺手收掉）：`users.role` 权限、`is_active` 禁用拦截（403）、`last_login_at`、
`tokens` 改存 sha256 + 记录 ip/UA、`get_optional_user` 补上 token 吊销校验、`/user/info` 去掉 `user_id` 参数。

- [ ] **真正的"忘记密码"**：现在 `reset_password` 要求已登录，用户忘了密码就没路 → 邮箱验证码/重置链接（需 SMTP 配置 + `password_reset_tokens` 表）
- [ ] **refresh token**：现在 24h 过期就要重登；加 `POST /user/refresh`（或双 token）
- [ ] **登录防护**：失败次数锁定 + 限流（`slowapi` 或自写 Redis 计数）；防用户名枚举（现有提示已统一，但注册接口仍会暴露"邮箱已被注册"，按需收敛）
- [ ] **密码策略**：至少含数字+字母/强弱提示、常见弱密码黑名单；`data/` 里别留明文
- [x] ~~role 字段~~ ✅ 已做：`users.role` + `User.is_admin`，`require_admin_dep` 只看 role；`ADMIN_USERNAMES` 降级为「第一个管理员从哪来」的注册引导
- [ ] **软删除 + 恢复**：`users.deleted_at`、`documents.deleted_at`，注销给冷静期
- [ ] **审计**：敏感操作（改密/注销/管理员列表）写日志
- [ ] 修小坑：`get_optional_user` 没查 token 表（吊销后仍视为已登录）；`token.token` 列 `VARCHAR(255)` 对 JWT 偏紧（实测 ~200 字符，再加 `jti` 就溢出）→ 扩到 512 或只存哈希

## 3. P1：工程化

- [ ] **alembic 迁移**：取代手写 `init_db.sql`（当前 ORM 与 SQL 两份定义已经开始漂移：`documents` 缺 `title/file_path/...`）
- [ ] **日志**：结构化（`loguru` 或 `logging` + JSON formatter）、请求 ID、SQL 只在 `SQL_ECHO` 开；错误日志不能包含密码/token
- [ ] **测试**：M1–M5 每个里程碑配接口测试；服务层（chunker/retriever/prompt）做纯单测；给 embeddings/LLM 做假实现（`fixture` 注入）以免跑测试烧钱
- [ ] **Docker**：`docker-compose.yml`（pgvector 镜像 `pgvector/pgvector:pg17` + api + worker + redis），数据卷持久化
- [ ] **CI**：GitHub Actions 跑 `pytest` + `ruff check`（本地 `.venv` 没有 pip，装包用 `uv`）
- [ ] **脚本**：`scripts/`（当前空目录）放 `init_db.py`、`reindex.py`、`backup.sh`、`create_admin.py`
- [ ] **配置**：`.env` 现在只有空的 `PSQL_URL` → 至少填 `DATABASE_URL`、`SECRET_KEY`（生产随机）、`EMBEDDING_*`、`LLM_*`
- [ ] 文档：接口示例、错误码表、部署步骤（README 已有骨架）

## 4. P2：锦上添花

- 混合检索 BM25/tsvector、查询改写（HyDE）、rerank、分块策略可配置、图片 OCR、表格结构化
- 知识库分享（链接/团队空间）、标签与目录树、批量导入、去重（文件 hash）
- 引用高亮与段落定位、答案流式截断保护、token 计费统计
- 前端（本轮不弄）：`frontend/` 还是 vite 初始模板，需要 axios/fetch 封装（按 `{status,msg,data}` 解包）、路由守卫、上传进度、对话页

## 5. 表结构现状与还缺的表

已就位（`app/db/models.py`，建表走 `scripts/init_db.py`）：

```
users / tokens / knowledge_bases / documents / chunks / kb_shares
                         ← 6 张；文档类数据全链路冗余 owner_id，授权表只挂 (kb_id, grantee_id)
```

还缺：

```
kb_invite / kb_join_request  邀请链接、加入申请（现在只能 owner 按 email 手工授权）
password_reset_tokens 「忘记密码」验证码/重置链接，P1
chat_sessions / chat_messages  会话与消息（含引用的 chunk_id 列表），P0-M5，你说了后面再建
chunks.embedding     vector(N) + HNSW 索引，P0-M3，pgvector 方案定了再加（当前不做）
```

## 6. 建议顺序（一周内可完成的版本）

`M1 上传 → M2 解析切块 → M3 向量化 → M4 检索 → 安全收尾(越权测试 + 忘记密码) → M5 问答 → M6 异步 → Docker/CI`

每完成一个里程碑就：`.venv/bin/python -m pytest` 全绿 + 更新本文件勾选框 + 一次 commit。
