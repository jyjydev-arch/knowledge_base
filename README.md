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
│   └── init_db.py              # 建表 / --drop 重置 / --check / --snapshot 生成 SQL
├── tests/                      # 接口测试 + 表结构测试
├── frontend/                   # Vue 前端（暂未接管）
├── data/                       # 上传的原始文件（gitignore）
└── app/                        # 后端代码包
    ├── main.py                 # FastAPI 入口 + 统一返回格式的异常处理器
    ├── api/
    │   ├── deps.py             # 公共依赖：DB Session、当前登录用户、管理员校验
    │   └── v1/
    │       ├── userrouter.py   # 用户路由
    │       └── documentrouter.py  # 文档路由
    ├── core/
    │   ├── config.py           # .env 配置
    │   ├── security.py         # 密码哈希 + JWT 签发/解析
    │   ├── document_hash.py    # 上传文件的流式 sha256 + 体积上限
    │   └── exceptions.py       # 业务异常（统一包成 status/msg/data）
    ├── db/
    │   ├── base.py             # DeclarativeBase
    │   ├── models.py           # 表结构唯一真源：User / Token / KnowledgeBase / Document / Chunk
    │   ├── psql_config.py      # 异步引擎、Session、get_db 依赖
    │   └── crud/               # 数据访问层（只读写库，不抛 HTTP 异常）
    ├── services/               # 业务算法层：存储路径/落盘/删除，M2 起加解析切块
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
注意 `/documents/download/{id}` 返回的是文件流，不是 `ApiResponse` 那层 JSON —— 它的**错误**响应才是统一格式。

上传的边界（`multipart/form-data`，依赖 `python-multipart`，缺了它整个应用启动即崩）：
扩展名白名单 `pdf/md/docx`（400）→ 知识库存在且属于你（404，防塞进别人的库）→
流式算 sha256 并卡 20MB（413）→ 同库判重（409）→ 落盘 `data/{user_id}/{kb_id}/{document_id}.{ext}` →
写库；任何一步失败都回滚已落盘的文件，不留垃圾。客户端的 `filename`/`Content-Length`/`Content-Type`
全部按不可信处理（展示名会 sanitize 成最后一段，MIME 由扩展名派生）。

删除文档的边界检查（顺序固定，任一条不过就**不碰磁盘**）：
未登录 → 401；`document_id` 非 uuid / 为空 → 422（路径参数类型挡掉）；
文档不存在或不属于你 → 404；`file_path` 是绝对路径、含 `..`、扩展名非法、
或与 `document_storage_path` 算出的 `data/<user_id>/<kb_id>/<document_id>.<ext>` 不一致 → 只删记录并在 `msg` 里说明。

`/documents/status` 的核对口径：拿库里每条 `file_path` 的文件名，和 `data/{user_id}/{kb_id}/` 目录里实际文件对比 ——
库里没有文件的是 `missing_files`（文档打不开，需重新上传），磁盘多出来的是 `orphan_files`（垃圾，需清理），
两者都空才算 `healthy=true`；不一致也返回 200，因为接口本身执行成功了，数据完整性要用字段表达。

## 数据表（5 张，向量化暂未启用）

```
users            账号：role(user/admin) / is_active / last_login_at / deleted_at
tokens           已签发凭证：token_hash(sha256) / expires_at / client_ip / user_agent
knowledge_bases  知识库总表：name + description(具体说明) + visibility + chunk_size/overlap
                 + document_count/chunk_count/version/last_indexed_at（文档变动时自动更新）
documents        原始文件：kb_id + owner_id + title/filename/file_path/file_size/mime_type
                 + file_hash(同库判重) + status(pending→parsing→chunking→indexed/failed) + error_msg
chunks           切块：document_id/kb_id/owner_id + seq + content + token_count + page/heading + extra
                 ※ 还没有 embedding 向量列，等 pgvector 方案定了再补（当前确认先不做）
```

`chat_history`（会话/消息表）按约定后面再建，见 TODO.md P0-M5。

## 测试

```bash
.venv/bin/python -m pytest      # 用临时 SQLite 跑完整流程，不依赖 PostgreSQL
```
