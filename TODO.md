# TODO —— 从后端主线一路推到「手机里的 Agent」

> 终局目标（你定的）：后端知识库 + 前端做完之后，**装进手机，成为一个能随身问资料的 agent**。
> 这句话对计划的真实影响是：**鉴权、流式、会话持久化、长任务通知这四件事必须按移动端的标准做**，
> 而不是按"本机 localhost 能用"的标准做。很多坑（24h 强制重登、SSE 一切后台就断、
> 进程内会话窗口在多 worker 下不存在）到手机上会一次性爆发，所以提前排在路上。

---

## 0. 进度快照（2026-11-18 实测，不是回忆）

| 模块 | 状态 | 证据 / 代码位置 |
| --- | --- | --- |
| 用户 | ✅ 8 条路由（注册/登录/登出/资料/改密/注销/管理员列表/reset），33 条测试 | `app/api/v1/userrouter.py` |
| 统一返回格式 + 异常包装 | ✅ `ApiResponse[T] {status,msg,data}` | `app/schemas/common.py`、`app/main.py` |
| JWT 鉴权 | ✅ token 存 sha256、可吊销、`get_optional_user` 已补吊销校验 | `app/api/deps.py`、`token_crud` |
| **知识库 CRUD** | ❌ **只有 3 条 `/kb/share`，`POST /kb`、`GET /kb/list`、`detail`、`update`、`DELETE` 全没做** | `app/api/v1/kbrouter.py` ← **当前最卡脖子** |
| 文档 | ✅ 6 条全实现（upload/list/detail/status/delete/download），48 条测试 | `app/api/v1/documentrouter.py` |
| 共享授权 | ✅ 3 条路由 + `kb_access` 权限矩阵，18 条测试 | `app/api/v1/kbrouter.py`、`app/services/kb_access.py` |
| 表结构 | ✅ **8 张表**（新增 chat_sessions / chat_messages），`init_db.sql` 快照已同步 | `app/db/models.py`、`scripts/init_db.py --snapshot` |
| 格式解析 | ✅ pdf/docx/pptx/md/txt + 注册表 + 预览 CLI（15 条测试） | `app/services/parsers/` |
| **切块与入库** ✅ | ✅ `chunker.py`（纯函数）+ `process_document()` 编排 + `/upload` 已调度后台任务；`status` 真流转 `pending→parsing→chunking→indexed/failed`，重跑幂等（先删后插） | 25 条测试：`tests/test_document_index.py` |
| 向量化 ⚠️ | ⚠️ **代码已就绪但一次都没连库跑过**：`app/services/vector_index.py` + `scripts/index_documents.py`；文档向量与记忆向量分 collection，metadata 必带 user_id/kb_id/document_id/chunk_id，按 document_id 幂等重建 | 待办：PG 建库 + 定 embedding 模型/维度 |
| 检索 ✅ | ✅ **M4 的第一版（关键词车道）已实现**：LIKE 召回 + 词频算分 + 每篇文档限流 2 块 + 总长预算；范围只认权限（会话的库或全部可读库），软删文档的块不参与 | `app/rag/retrieval.py`、`chunk_crud.search_chunks` |
| 会话存储与服务 ✅ | ✅ 两张表 + `chat_crud` + `app/rag/chat.py`（new/get/list/rename/delete/history/load_context/record_turn）；摘要带**水位线**不会重复折叠；28 条测试 | `app/rag/chat.py`、`app/db/crud/chat_crud.py` |
| 对话路由 ✅ | ✅ **8 条全部实现并 include**：会话 4 条 + 历史消息 + 问答（SSE/JSON 两条车道）+ 停止 + agent 目录；33 条接口测试 | `app/api/v1/aichatrouter.py`、`app/rag/answer.py` |
| 记忆（两层） | ✅ 短期窗口+压缩、长期 local/pgvector 双后端、判重与容量淘汰、68 条测试 | `app/rag/memory/`、`app/rag/llm.py` |
| 输出层（流式 + 结构化） | ✅ 五类事件帧 + 输出契约 + 懒构造 `build_agent()`；`stream_chat_tokens()` 这轮加了 `on_text` / `should_stop` 两个钩子给 /stop 与落库用 | **已接进 `POST /aichat/conversations/{id}/chat`**；结构化输出（agent 车道）留给 M8 |
| 记忆接线 ✅ | ✅ 问答链路里两层记忆都在真用：`load_context` 取长期记忆 + 摘要水位线后的窗口，`record_turn` 按配置回写长期记忆；同步阻塞的记忆调用已挪进 `run_in_threadpool` | `app/rag/chat.py`、`app/rag/answer.py` |
| 前端 | ❌ 还是 vite 初始模板：`src/` 只有 `App.vue`+`main.ts`，依赖只有 `vue` | `frontend/` |
| **PostgreSQL** | ❌ **项目从没在 PG 上跑起来过一次**：`.env` 里 postgres 密码错（真实密码在 `~/.pgpass`），且 `knowledge_base` 库不存在 | `scripts/init_db.py --check` 认证失败 |
| 占位路由 ✅ | ✅ 已消灭：`pass` 版本实测返回 `200 + null`（前端解包必炸），现在 8 条都有真实实现与统一返回体 | `app/api/v1/aichatrouter.py` |
| 双前缀 ✅ | ✅ 已修为 `prefix="/aichat"`，并加了防呆测试（router 前缀不许含 `/api`；openapi 路径里不许出现 `/api/v1/api`；aichat 接入后新增一条：8 条路径必须出现在 `/api/v1/aichat` 下且数量正好 8） | `app/api/v1/aichatrouter.py`、`tests/test_startup_health.py` |
| 健康检查 ✅ | ✅ `/health` 现在真连一次库：缺表/连不上 → **503** + 缺表清单，探库带超时（默认 2s）；响应只给类别，不含连接串与异常原文 | `app/core/startup.py`、`app/main.py` |
| 建表 ✅ | ✅ 加了 lifespan 启动检查（`app/core/startup.py`）：默认**告警不阻断**，`DB_STRICT_STARTUP=True` 直接拒绝启动，`AUTO_CREATE_TABLES=True` 启动即建表（仅开发）。内存库跳过，测试车道不受影响 | `app/core/startup.py`、`tests/test_startup_health.py`（20 条） |
| 测试 | ✅ **321 passed**（11 文件），仍**全部走临时 SQLite**；对话测试用假模型 + `NullBackend`，不出网也不写盘 | 构成：33 用户 / 48 文档 / **33 对话接口** / 28 会话服务 / 25 切块入库 / 18 共享授权 / 15 解析 / 68 记忆 / 27 输出 / 20 启动健康 / 6 表结构 |
| 路由总数 | **25 条已实现**（用户 8 + 文档 6 + 授权 3 + 对话 8） | `GET /openapi.json` |

一句话：**主线已经通了，只剩一个断点**。切块入库、会话落库、问答链路（检索→prompt→流式→落库）都接上了；
还卡着的是 **M0 知识库 CRUD**：没有 HTTP 途径能建库，`/documents/upload` 和 `chat` 的 `kb_id` 都无来源
（现在只能靠脚本建库联调）。

**本轮（对话路由接线 + 检索车道 + 停止生成）新增**：`app/rag/{answer,retrieval,generation}.py`、
`app/schemas/chat_schemas.py`、`tests/test_chat_api.py`（33 条）；`chunk_crud` 加 `search_chunks`；
`deps.py` 加 `get_session_factory`（流式收尾用）；`aichatrouter.py` 8 条实现并在 `main.py` include；
`config.py` 加 6 个 `CHAT_*` 开关；`chat.py` 把阻塞的记忆/压缩调用挪进线程池。321 passed，我写的文件 ruff 干净。

---

## 1. 移动端形态：**已定 = Vue + WebView 壳**（2026-11-14 你拍的板）

WebView 路线有两个做法，差别不是"要不要 WebView"，而是**那几座桥自己写还是别人替你写好**：

| 做法 | 出 APK | 文件/相册选择 | 系统通知 | 返回键/生命周期 | 存储持久 | 代价 |
| --- | --- | --- | --- | --- | --- | --- |
| **Capacitor 壳**（推荐） | ✅ 一条命令 | ✅ `Filesystem`/相机插件 | ✅ LocalNotifications（推送需 FCM） | ✅ 已处理 | ✅ `Preferences`（文件存储，不被 WebView 清缓存带走） | 依赖 node + Android Studio |
| 手写最小壳（1 个 Activity + WebView） | ✅ 自己写 | ❌ 必须自己实现 `WebChromeClient.onShowFileChooser` + FileProvider | ❌ 自己接 | ❌ 自己拦 `goBack()` | ⚠️ `localStorage` 会被系统/用户清缓存带走 | 看着省 2 小时，桥要一座座补 |

**结论**：壳本体确实是几十行代码的事，所以"WebView 就行"这个判断成立；
但要的恰恰是 Capacitor 已经写好的那几座桥 —— 我们**必然**要用到文件选择（上传文档）、
持久 token、后台长任务通知，手写壳等于把它们重做一遍。Tauri v2 mobile 也走系统 WebView，
但要多学一套 Rust 工具链，收益（体积小）抵不过成本，不选。

过渡形态（同一个 Vue 工程，零额外代码，**今天就能拿手机试**）：
后端 `--host 0.0.0.0` → 手机浏览器直连 `http://<局域网IP>:8000`，跑通再打包。
注意 Android 9+ / iOS ATS 默认都拒绝明文 HTTP：调试期靠 `usesCleartextTraffic`
（Capacitor 的 `android:usesCleartextTraffic` / `allowMixedContent`）放行，
**正式上线要么真 HTTPS，要么至少把明文开关限定在局域网地址段**，别图省事全局放开。

---

## 2. P0 主线：后端补齐（M0 → M6）

> 每个里程碑都能单独验收；做完一个就 `pytest` 全绿 + 更新本文件 + 一次 commit。

### M-环境 先打通 PostgreSQL（0.5 天，**排在 M0 前面**）

不打通的后果：前端一联调就撞"只有 SQLite 能跑"，而且**并发那条路径至今没被真验证过**
（`document_crud.refresh_kb_stats` 用 `with_for_update`，SQLite 直接忽略 FOR UPDATE，丢更新测不出来）。

- [ ] `.env` 的 `DATABASE_URL` 换成 `~/.pgpass` 里的真实密码（别用超户跑应用：建个 `knowledge_base` 角色 + 只给它那一个库）
- [ ] `createdb -U postgres -h 127.0.0.1 knowledge_base` → `scripts/init_db.py` 建表 → `--snapshot` 重新生成 `init_db.sql`
- [ ] `.venv/bin/python -m pytest` 之外，加一条**真 PG 冒烟**（`DATABASE_URL` 指向 PG 跑一遍现有接口测试）
- [ ] 验证 `with_for_update` 并发不丢更新（两个 session 同时 `bump_kb_version`）
- [x] ~~`/health` 探库~~ ✅ 已实现 `app/core/startup.py`：表存在性 + 503 + 超时 + 不泄露连接信息
- [x] ~~启动检查~~ ✅ lifespan `ensure_schema_on_startup()`：默认告警；生产建议 `DB_STRICT_STARTUP=True`；
      开发可 `AUTO_CREATE_TABLES=True`。README「启动检查与 `/health`」已写清五种情形
- 验收：`uvicorn app.main:app` 起来、`/docs` 能试通登录与上传、`psql \dt` 六张表都在；
  且**故意把表删掉后 `/health` 返回 503**（而不是 ok）

### M0 知识库接口（0.5 天）——**前端和 agent 的第一个依赖**

`documents.kb_id` 是 NOT NULL，而**没有任何 HTTP 途径能创建知识库**；被授权人也列不出"别人共享给我的库"。

- [ ] `app/api/v1/kbrouter.py` 补 5 条：`POST /kb`、`GET /kb/list?scope=owned|shared|all`、`GET /kb/detail`、`POST /kb/update`、`DELETE /kb/{kb_id}`（软删 + 级联清磁盘。注：`remove_kb_directories_if_empty` 已经在**文档删除**路径上被调用两处，这里缺的是**删库**那条路：文档级联删完之后再收空目录，别忘了 `data/share/{kb_id}/` 那个落点也要一起清）
- [ ] `GET /kb/list` 必须同时返回 owned + shared 两类，并标出**每条的 permission（read/write）**——移动端 UI 直接靠它决定按钮是否可用
- [ ] `app/schemas/kb_schemas.py`：`KbCreate(name, description, visibility?, chunk_size?, chunk_overlap?)` / `KbOut`（含 `document_count/chunk_count/version/last_indexed_at/permission`）
- [ ] `kb_crud` 查询一律带 `owner_id`；`refresh_kb_stats` 复用 `document_crud`
- [ ] 越权口径沿用 `kb_access`：无 read → 404（不暴露存在性），能读不能写 → 403
- 验收：建库→`version=1`→传文档→`version=2`；B 用户能在 `/kb/list` 里看到 owner 共享给他的库；A 改不了 B 的库

### M1 文档 ✅ 已完成（6 条路由 + 边界 + 判重 + 回滚 + 磁盘↔DB 核对）

留两个尾巴：
- [ ] 一次性清理脚本：把废弃布局 `data/kb/...` 的历史文件搬回约定目录或删除（`/status` 扫不到它们）
- [ ] 上传大小限制从路由常量 20MB 提成配置项（手机端传大文件前必须先定这条）

### M2 后半 切块与入库（1 天）——**agent 有没有凭据就看这里**

- [x] ~~chunker~~ ✅ `app/services/chunker.py`：按 (heading, page) 分组 → 组内滑窗，长度取 `kb.chunk_size/chunk_overlap`；
      `overlap >= chunk_size` 自动收敛（否则窗口不前进 = 死循环）；断点优先落句读，尾巴过短并进上一块
- [x] ~~编排~~ ✅ `process_document(document_id, session=None)`：路径校验 → `registry.parse_file`（线程池）→ 切块 → `chunk_crud.replace_document_chunks` → 状态与计数
- [x] ~~status 流转~~ ✅ `pending → parsing → chunking → indexed / failed`，失败原因进 `error_msg`（空文档提示"可能是扫描件，需要 OCR"）
- [x] ~~幂等重索引~~ ✅ crud 只有 `replace_*`（先删后插），没有"追加"口子；`force=False` 跳过已 indexed 的
- [x] ~~后台执行~~ ✅ `/upload` 用掉了预留的 `background_tasks`；新增开关 `DOCUMENT_INDEX_ON_UPLOAD`（默认 True，测试车道 False）
- [ ] **`failed` 的文档现在没有重跑入口**：要么暴露 `POST /documents/reindex/{id}`，要么让 `/upload` 撞重复文件时把旧文档重新索引
- [ ] 后台任务的可观测性：进程内任务崩了只能靠日志；重启时留在 `parsing/chunking` 的文档没人捡（worker 化时一起做）
- [ ] OCR（扫描件）与老 `.doc/.ppt` 预转：优先级看实际资料构成，P2
- 验收：上传一篇 10 页 PDF，`SELECT count(*) FROM chunks` 有数据、`status=indexed`、`/detail` 能出真实块

### M3 向量化 ⚠️ 代码已写好，卡在两件外部事：PG 库 + embedding 模型/维度

- 到时候：`chunks.embedding vector(N)` + HNSW；`app/services/embedding.py` 走 API 方案
  （`EMBEDDING_BASE_URL/API_KEY/MODEL/DIMS` 已存在于配置里）；**维度一旦定了不能改**，换模型要连维度带 collection 一起换
- 长期记忆的 pgvector 车道用的是同一套依赖，M3 做完顺手把 `MEMORY_BACKEND=pgvector` 打开即可

### M4 检索 ⚠️ 半程（关键词车道已上线，向量车道等 M3）

手机 agent 不需要"最准"，需要"今天就能问出带来源的答案"。所以分两步：
- [x] ~~第一版全文检索~~ ✅ `app/rag/retrieval.py`：中文按「整段 + 2-gram」造词（没分词器时段落级 ILIKE 必然 0 命中）、`chunk_crud.search_chunks` 做 LIKE 召回（转义 `%`/`_`、JOIN documents 滤软删）、词频算分、每篇文档限流 2 块、总长预算 `CHAT_RETRIEVAL_MAX_CHARS`。没用 `websearch_to_tsquery` 是要同时兼容测试车道的 SQLite，PG 全文等向量车道一起上
- [ ] 独立的 `POST /search` 还没做（前端要做"纯搜索框"时再加）。文件落点是 `app/rag/retrieval.py` 而不是 `app/services/retriever.py`：按 §2 的分工，检索属模型侧，`services/` 只放切块/解析这类业务算法
- [x] ~~越权过滤~~ ✅ 范围只认权限（会话的库 → 授权校验；没限定 → `accessible_kb_ids`），空范围=查不到而不是查全库；测试钉住：`test_问答_检索不越权_共享之后才能查到`、`test_问答_被软删文档的块不参与检索`、`test_新建会话_不能圈别人的库`
- [ ] 第二版把 SQL 换成向量 `<=>`，上层零改动（M3 时做）
- 验收：✅ 已过（关键词车道，33 条接口测试里有 5 条直接打在检索上）。B 搜不到 A 的内容是**硬要求**，向量版上线时这几条测试一行都不许改

### M5 问答 + 会话 ✅ 已完成（除断线续传）——**记忆在这里接线**

- [x] ~~新表~~ ✅ `chat_sessions`（title / summary / **summary_seq 水位线** / 计数 / last_message_at）+
      `chat_messages`（role / content / seq / token_count / sources JSON），`uq(session_id, seq)` 防并发重复；
      `kb_id` 用 `ON DELETE SET NULL`（删库不该带走历史对话）
- [x] ~~会话服务层~~ ✅ `app/rag/chat.py`：`new_chat/get_chat/list_chats/rename_chat/delete_chat/read_history`
      + `load_context()`（历史从库读、摘要从 `summary` 读、长期记忆语义检索）+ `record_turn()`
      （两条消息入库 → 按水位线折叠摘要 → 落长期记忆）。**每个查询都带 user_id**，别人的 session_id 一律 None
- [x] ~~对话路由接线~~ ✅ 8 条全部实现并 include：会话 4 条 + 历史消息 + 问答（SSE/JSON 两条车道）+ 停止 + agent 目录。非流式一律 `ApiResponse[T]`，流式按 §3.6 不套外层
- [x] ~~重复折叠防护~~ ✅ `summary_seq` 水位线 + `test_水位线之后不重复折叠` 钉住
- [x] ~~上面那三处 + 下面那条 501~~ ✅ 一并做完：`prefix="/aichat"`（上一轮已修）、`include_router` 已加、返回体统一。防呆测试升级成「8 条路径必须出现在 `/api/v1/aichat` 下且正好 8 个操作」
- [x] ~~过渡期的 501 占位~~ 随实现消失。留下的教训写进了 §12：占位函数返回 `None` 会让前端解包 `null` 时炸，以后新增接口要么 `raise NotImplementedFeatureError`，要么别挂上路由
- [x] ~~SSE 协议层~~ ✅ 已实现 `app/rag/output/sse.py`：`meta/token/references/done/error` 五类帧、
      `id:` 序号、心跳注释帧、错误帧不外泄异常原文、`sse_response()` 带 `X-Accel-Buffering: no`。
      顺带修掉原实现两个静默 bug：`HumanMessage(**m)` 把 assistant 历史变成"用户说的话"；
      把 LangGraph 的 `stream_mode` 传给 chat model（多余 kwargs 会透进厂商 SDK）
- [x] ~~结构化输出~~ ✅ `struct_response.py`：`AgentAnswer{answer,sources[],answerable,kb_id}` +
      `SourceRef`（带 chunk_id/document_id，能点回原文）；`ErrorResponse` 那种"让模型产出错误对象"
      的写法废掉 —— 模型不知道自己什么时候失败，错误由服务端产生
- [x] ~~SSE 接线~~ ✅ `POST /aichat/conversations/{id}/chat` → `sse_response(answer.stream_turn(...))`；`meta` 帧带 `session_id/model/retrieval/generation_id`，`references` 在 `done` 之前发。`stream=false` 走同一条链路返回 JSON（脚本与 CI 冒烟用）
- [ ] **断线续传**（M5 唯一剩下的）：`Last-Event-ID` → 会话已落库，所以能先判断"这轮到底答完没有"：答完了直接让前端读 `/messages` 补齐；没答完再考虑重放生成。别一上来就做"接着播 token"
- [ ] `/stop` 的进程内局限（多 worker 不共享）→ 与 P1 的多 worker 部署一起解决，见 §12
- [x] ~~问答主流程~~ ✅ `retrieval.retrieve()` 取资料 → `chat.load_context()`（水位线之后的窗口 + 长期记忆语义检索）→ `ctx.to_messages()` → 模型 → `chat.record_turn()`（**先落库再折叠**，按配置回写长期记忆）。编排集中在 `app/rag/answer.py`，两条车道共用同一个 `build_turn()`，不会出现"流式和非流式行为不一样"
- [x] ~~流式用 SSE~~ ✅ `text/event-stream` + `X-Accel-Buffering: no` + 心跳注释帧（`CHAT_STREAM_HEARTBEAT_SECONDS`）。续传见下一条；**停止生成已经能用**：`/stop` 置标志位 → 流正常发 `done` → 半截答案照常落库（不是 `Task.cancel()`）
- [x] ~~引用可点回原文~~ ✅ `sources[{index,document_id,chunk_id,kb_id,chunk_seq,page,heading,title,score,snippet}]`，随消息一起存进 `chat_messages.sources`，前端拿 `document_id` 直接跳 `/documents/detail|download`
- [x] ~~失败可见~~ ✅ 模型没配置/构造失败 → 推流**前** 503；模型中途挂 → `error` 帧 + `trace_id`（异常原文只进日志）；检索为空 → `meta.retrieval.mode` 如实给 `none/no_scope/disabled/keyword`；prompt 里的 GUARDRAIL 要求"资料里没有就说不知道"
- 验收：✅ 多轮有上下文（`load_context` 从库里读）、老对话被水位线压进摘要、答案有来源、无资料时不编造。真模型跑通要等 `.env` 填 key（测试用假模型）

### M6 慢任务与状态（0.5 天，与 M2 后半合并做也行）

- [ ] `GET /documents/status` 已有；补 `POST /documents/reindex/{id}`（幂等重试）
- [ ] 前端**轮询**而不是等服务端推 —— 手机上"上传完立刻转圈等结果"的体验最差，应立刻返回 `document_id + pending`，任务化展示

---

## 3. P0 前端（M7，1.5 天）

**环境坎先迈过去**：`frontend/package.json` 的 `engines` 要 node `^22.18 || >=24.12`，本机是 **v20.19.2**
→ 用 `fnm install 22`（你自己的 shell 里装，我这边的沙箱 PATH 看不到 `~/.local/bin`，同理装完也别指望我能跑 npm）。

- [ ] 技术栈按"业务代码为 0、此刻定最便宜"落：**Vue 3 + vue-router + Pinia + @tanstack/vue-query + Naive UI**
      （服务端状态一律交给 vue-query：重试、失效、分页、乐观更新是它的活儿，不要手搓）
- [ ] `marked` + **`dompurify`** 渲染模型输出的 Markdown —— **安全必需**，LLM 输出直接 `v-html` 等于开放 XSS
- [ ] `scripts/export_openapi.py`：离线把 schema dump 成 `frontend/src/api/schema.d.ts`
      （不要前端去请求运行中的后端 —— CI 和打包时后端不一定起着）
- [ ] 从它生成类型：`openapi-typescript` + `openapi-fetch`，接口一改就有编译错误
- [ ] 统一封装：按 `{status,msg,data}` 解包，`status!==200` 直接抛并展示 `msg`；401 跳登录
- [ ] 开发代理 `vite.server.proxy` 把 `/api` → `127.0.0.1:8000`（免 CORS；**注意：Capacitor 壳里没有这层代理，届时必须走真实 `API_BASE_URL` + CORS**）
- [ ] 页面顺序（每页都依赖上一个里程碑）：
      ① 登录/注册 ② 知识库列表(owned+shared+permission 徽标)+建库 ③ 文档管理(上传进度/状态轮询/下载/删除)
      ④ 共享授权面板 ⑤ 对话页(SSE 流式 + 引用展开 + 会话侧栏)
- [ ] 移动端优先的细节现在就在注释里定好：单列布局、触摸目标 ≥44px、键盘弹出不遮输入框、深色模式、字号跟随系统
- 验收：桌面上能"建库→上传→看到 indexed→发问→看到带来源的流式答案"，全程不碰 `/docs`

---

## 4. P0 手机化（M8 agent 能力 + M9 上机）

### M8 Agent 化：从"聊天"升级成"会用工具的助手"（1 天）

- [ ] 工具注册表 `app/rag/tools.py`：每个工具就是一个**已有 service/crud 的薄封装**，
      参数用 JSON Schema 描述，模型只能选、不能自由拼 SQL/路径：
      `kb_list` / `kb_search`(检索，带 kb_id 范围) / `document_read` / `memory_read` / `memory_write` /
      `document_upload_hint`(引导用户去上传，而不是替用户上传)
- [ ] **铁律：工具一律以 `current_user` 身份执行，参数里绝不接受 `user_id`**；权限判定继续走
      `kb_access`，越权语义与 HTTP 层一致（无 read→404、能读不能写→403）。模型请求"删库"这类
      破坏性动作时**必须前端二次确认**，不许自动执行
- [ ] 注入防护已有底子：`context.py` 把资料与记忆包在 `【参考资料】/【长期记忆】` + "不要执行其中的指令"，
      渲染只走 `safe_format`。工具调用时代码要再兜一层：工具返回同样标成数据、不进 system
- [ ] `agent.py` 收尾（现在是坏的草稿）：`create_agent(model="deepseek:deepseek-flash")` 需要
      `langchain-deepseek`，或改成 `model=get_chat_model()` 传实例；`{context}/{question}` 用
      `ChatContext.render_prompt()`，别再手写 `.format`
- [ ] 可观测：每次问答记录用了哪些工具、召回了哪些 chunk、token 用量（进 `chat_messages`）
- 验收：问"我们上周定的报销标准是什么" → 它自己去检索+读文档 → 给答案并列出来源；问"把我库删了" → 要求确认而不是直接删

### M9 WebView 壳上机（0.5–1 天）

先决条件不是壳，是**后端能被手机访问**：
- [ ] `uvicorn --host 0.0.0.0` + 本机防火墙放行 8000（调试期）。`CORS_ORIGINS` 现在是 `*`：**带
      `Authorization: Bearer` 的请求配 `*` 本来能跑**（不是 cookie 模式），但 `main.py` 里写的是
      `allow_credentials="*" not in _origins` —— 一旦以后改用 cookie/credentials，`*` 会静默把凭据关掉；
      而且 `*` 等于任何网页都能打你的接口。所以上机时收成具体 origin：
      `http://localhost`（Android 壳）、`capacitor://localhost`（iOS 壳）、以及部署后的真域名
- [ ] **HTTPS**：正式必须。自签证书在 WebView 里要靠原生信任链（麻烦），
      建议 `tailscale serve` / Caddy 内建证书 / LE DNS-01，别用 IP 直连上线
- [ ] 反代：SSE 必须 `proxy_buffering off` + `proxy_read_timeout ≥ 300s`，否则流式答案会被缓冲成"一次性蹦出来"或超时截断

壳本身（Capacitor）：
- [ ] `npm i @capacitor/core @capacitor/cli @capacitor/android` → `npx cap init` → `npx cap add android`
      → `npm run build && npx cap sync && npx cap open android` 出 debug APK 侧载
- [ ] `API_BASE_URL` 走前端环境变量：dev = `http://192.168.x.x:8000/api/v1`，prod = https 域名。
      ⚠️ **壳里没有 vite proxy**，桌面开发那套代理到手机上不存在，接口必须能直连
- [ ] 明文开关只在 debug 构建打开（`network_security_config` 限定局域网段），release 关掉
- [ ] 四座必须自己确认能用好的桥：
      ① **文件/相册上传**（`@capacitor/filesystem` + 相机；对应后端的 multipart 上传与白名单）
      ② **返回键**（Android 物理返回要拦 `webview.goBack()`，否则一问话就退到桌面）
      ③ **存储持久**（token/会话别只放 `localStorage`，用 `Preferences`；系统清缓存会把它带走 → 用户莫名掉登录）
      ④ **通知**（可选，取决于 §1 决策 4：解析/重索引完成、长答案完成）
- [ ] 键盘与滚动：`android:windowSoftInputMode="adjustResize"`，输入框高度用 `visualViewport`，
      否则聊天页键盘会把输入框顶出屏幕 —— 这是聊天类 WebApp 最高频的真机 bug
- [ ] 调试：debug 构建开 `setWebContentsDebuggingEnabled(true)`，手机连 `chrome://inspect`
      才能看到 console；线上问题没这个基本两眼一抹黑
- [ ] 断网/弱网：请求超时与重试（vue-query 配好）、上传失败可重放、列表分页，
      **移动网络下"一次拉全量"一定会超时**
- [ ] 版本：留一个 `GET /app/version` 或在登录响应里带 `min_client_version`，
  壳一旦发出去，服务端可以不兼容自己
- 验收：**手机浏览器**先完整跑通 §3 那条链路（建库→上传→indexed→提问→流式带来源答案），
  再侧载 APK 跑通同一条 —— 顺序不能反，浏览器跑不通就别怪壳
## 5. P1：安全与工程化（手机端上线前必须过一遍）

安全（多人用/公网暴露时优先级上升）：
- [ ] 真正的"忘记密码"（现在 `reset_password` 要求已登录，忘了就没路）→ 邮箱验证码 + `password_reset_tokens` 表 + SMTP
- [ ] **refresh token**（移动端刚需，见 M9）
- [ ] 登录防护：失败锁定 + 限流（`slowapi` 或 Redis 计数）；注册接口仍会暴露"邮箱已被注册"，按需收敛
- [ ] 密码策略（数字+字母、弱密码黑名单）；`data/` 里不留明文
- [x] ~~`tokens.token` 列宽偏紧~~ **这条是过期结论，作废**：现在库里存的是 **sha256 摘要**（`token_hash String(64)` + 唯一索引），JWT 原文不落库 —— 既没有长度问题，也没有「库被拖走等于票据全泄露」的问题
- [ ] 软删 + 冷静期恢复（`users.deleted_at` 列已有，注销走的是硬删）
- [ ] 审计日志：改密/注销/管理员列表/授权变更
- [ ] 公网暴露前：CORS 收成具体域名、`SECRET_KEY` 换随机、`APP_DEBUG=False`、`/docs` 生产关闭、上传目录权限与磁盘配额

工程化：
- [ ] **alembic**：ORM + `init_db.sql` 两份真源迟早漂移（`--snapshot` 只是快照，不是迁移）
- [ ] **测试车道**：加一条真 PG 的集成测试车道（当前 188 条全在 SQLite；FOR UPDATE 从未被验证）
- [ ] 记忆层：pgvector 车道的**集成测试**（现在只测了不需要连接的部分：DSN 换算/懒构造/阈值折算/降级）
- [ ] 日志：结构化 + 请求 ID；错误日志不含密码/token；LLM 调用记录不写用户资料原文
- [ ] Docker：`pgvector/pgvector:pg17` + api + worker + redis，数据卷持久化
- [ ] CI：GitHub Actions 跑 `pytest` + `~/.local/bin/uvx ruff check`（venv 里没装 ruff/pip，用 uvx；老代码基线还有 39 个 lint 问题，先只卡新增）
- [ ] 脚本：`scripts/` 补 `reindex.py`、`backup.sh`、`create_admin.py`
- [ ] `.env`：把 §配置 的新字段（LLM/EMBEDDING/MEMORY/SHORT_TERM）填成你自己的真值（占位值会被判为未配置并自动降级）

---

## 6. P2：暂缓 / 锦上添花

- 向量化（M3）与 rerank、混合检索（BM25/tsvector + 向量）、查询改写（HyDE）、分块策略可配置
- 图片 OCR、表格结构化、`.doc/.ppt` 预转
- 语音输入（手机端很实用：ASR 走系统键盘或云端）+ 答案朗读
- 知识库邀请链接 / 加入申请（现在只能 owner 按 email 手工授权）
- 标签与目录树、批量导入、去重、引用高亮与段落定位、token 计费统计
- 离线包（把常用库的资料缓存到手机本地）

---

## 7. 表结构现状与还缺的表

已就位（`app/db/models.py` 是单一真源，建表走 `scripts/init_db.py`）：

```
users / tokens / knowledge_bases / documents / chunks / kb_shares / chat_sessions / chat_messages
                         ← 8 张；文档与会话数据全链路冗余 owner_id/user_id，鉴权过滤不靠 join
```

> 加表之后记得：`scripts/init_db.py --snapshot > init_db.sql` 已同步（本轮已刷）；
> `/health` 会直接告诉你哪张表还没建 —— 这条防呆当场逼我更新了测试里的期望表清单，有效。

还缺（按新目标的优先级排序）：

```
password_reset_tokens       忘记密码验证码/重置链接 —— P1，但一旦给同事用就是硬需求
chunks.embedding vector(N)  仍未加。当前向量存在 PGVector 自己的两张表里，靠 metadata 的
                            chunk_id 对应回来；要不要在业务表上开这列，取决于 embedding
                            模型/维度拍板 —— 维度一旦建列就不能改（TODO.md M3）
（文档向量与记忆向量分别落在 collection `kb_documents` 与 `long_term_memory`，
  同一套 langchain_pg_collection/langchain_pg_embedding 表；**故意不和业务表 join**，
  也正因为如此，任何"重建"只能按 metadata 精确删，绝不能 drop_tables()）
```

---

## 8. 建议执行顺序

```
M-环境(打通 PG + 建 8 张表) → M0(知识库 5 条) ✅后端主线到这里就通了
   → M7 前端(① -④页：登录/库/文档/详情)
   → 前端⑤对话页(后端 /aichat 已就绪，只剩前端 + 断线续传)
   → M8(工具调用 agent 化 + agent 列) → M-向量(M3 定 embedding → M4 换向量车道 → index_documents 验收)
   → M9(HTTPS + refresh token + Capacitor APK 上机) → P1 安全/工程化收尾
```

为什么是这个顺序：
1. **打通 PG 排第一**是这次的新增判断 —— 手机化要求真并发、真扩展、真连接串，越晚切它，返工越多。
2. **M0 必须先于任何前端页面**：拿不到 `kb_id`，前端所有页面都是空壳。
3. ~~M2 后半必须先于问答、对话路由接线、M4 全文兜底~~ ✅ **三件都通了**：`chunks` 有数据、`/aichat` 8 条已接入、检索有关键词车道。现在**唯一的硬阻塞是 M0**（建库接口），它挡住 upload 与 chat 的 `kb_id` 来源。
4. **M4 先用关键词兜底，不等 M3** ✅ 已按这条做完：手机 agent 要的是"今天能问出带来源的答案"，语义检索是质量优化不是前置。换向量时只动 `retrieval.py` 内部，越权那几条测试一行都不许改。
5. **前端不等后端全好**：M0 做完就能动前端 ①-④ 页，第 5 页（对话）等 M5。
6. **M5 已经收工**（记忆、输出协议、会话落库、检索、流式、停止全接上了）。
   所以前端 ⑤ 对话页不再需要等后端 —— 唯一的例外是**断线续传**（`Last-Event-ID`），
   它是"手机切后台"场景下唯一还没解的对话问题，排在 M7 之后、M9 之前做。
7. **防呆测试排在 M0 之后**：双前缀、裸 `None` 返回这两类坑都是"写出来不报错、上线才发现"的，
   靠 review 拦不住，而成本只有十几行断言。
8. **`/health` 探库排在第一次部署之前**：否则"库没建/连不上"会被读成"服务正常"，
   排查方向从第一步就歪掉。

每完成一个里程碑就：`.venv/bin/python -m pytest` 全绿 + 更新本文件勾选框 + 一次 commit（push 你自己来）。
