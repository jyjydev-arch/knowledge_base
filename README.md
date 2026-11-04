# knowledge_base

一个 AI 本地知识库。

- **[TODO.md](TODO.md)** —— 还差什么没实现（知识库主线 M1–M6、安全收尾、工程化），做功能先看这里
- **[DEVELOPMENT.md](DEVELOPMENT.md)** —— 开发规范（分层、统一返回格式、命名、鉴权、测试、Git、依赖）

## 目录结构

```
app/
├── main.py                 # FastAPI 入口 + 统一返回格式的异常处理器
├── api/
│   ├── deps.py             # 公共依赖：DB Session、当前登录用户、管理员校验
│   └── v1/
│       └── userrouter.py   # 用户路由（main.py 里 include_router 挂载）
├── core/
│   ├── config.py           # .env 配置
│   ├── security.py         # 密码哈希 + JWT 签发/解析
│   └── exceptions.py       # 业务异常（会被统一包成 status/msg/data）
├── db/
│   ├── base.py             # DeclarativeBase
│   ├── models.py           # SQLAlchemy 模型：User / Token / KnowledgeBase / Document / Chunk
│   ├── psql_config.py      # 异步引擎、Session、get_db 依赖
│   └── crud/               # 数据访问层：user_crud / token_crud / document_crud（只读写库，不抛 HTTP 异常）
├── services/               # 业务算法层（解析/切块/向量化/检索/prompt），M2 起用
└── schemas/
    ├── common.py           # ApiResponse[T] / PageData[T] / PageParams
    ├── user_schemas.py     # 用户相关请求体与响应体
    └── document_schemas.py # 文档相关响应体（含 download_url）

scripts/
└── init_db.py              # 建表 / 重置 / 检查 / 生成 init_db.sql
```

> 包里没有 `__init__.py`，靠 Python 3.3+ 的命名空间包（PEP 420）导入，少一层维护成本。

## 快速开始

```bash
# 1. 装依赖（虚拟环境在 app/.venv）
cd app && ~/.local/bin/uv sync --group dev && cd ..
#    不想用 uv 的话: python -m venv app/.venv && app/.venv/bin/pip install -r requirements-dev.txt

# 2. 配置 .env（可直接复制 .env.example，至少要填 DATABASE_URL 和 SECRET_KEY）
cp .env.example .env

# 3. 建表（表结构真源是 app/db/models.py，按 .env 里的 DATABASE_URL 执行）
app/.venv/bin/python scripts/init_db.py            # 只建缺失的表
app/.venv/bin/python scripts/init_db.py --check    # 检查库里的表和模型是否一致
app/.venv/bin/python scripts/init_db.py --drop     # 开发期重置：删表重建（会清空数据）
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
| POST | `/upload` | 上传文件、入库并更新知识库版本信息 | 🚧 501 |
| GET | `/detail` | 文档正文预览 + 所属知识库版本信息 | 🚧 501 |
| GET | `/status` | 各解析状态的文档数量 | 🚧 501 |
| DELETE | `/delete/{document_id}` | 删除文档并同步知识库统计/版本 | 🚧 501 |
| GET | `/download/{document_id}` | 下载原始文件 | 🚧 501 |

未实现的接口统一返回 `{"status": 501, "msg": "...还没实现（见 TODO.md P0-M1）", "data": null}`，
不会静默返回 null 骗过前端。

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
app/.venv/bin/python -m pytest      # 用临时 SQLite 跑完整流程，不依赖 PostgreSQL
```
