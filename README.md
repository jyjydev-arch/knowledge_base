# knowledge_base

一个 AI 本地知识库。

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
│   ├── models.py           # SQLAlchemy 模型：User / Token / Document
│   ├── psql_config.py      # 异步引擎、Session、get_db 依赖
│   └── crud/               # 数据访问层：user_crud.py / token_crud.py（只读写库，不抛 HTTP 异常）
└── schemas/
    ├── common.py           # ApiResponse[T] / PageData[T] / PageParams
    └── user_schemas.py     # 用户相关请求体与响应体
```

> 包里没有 `__init__.py`，靠 Python 3.3+ 的命名空间包（PEP 420）导入，少一层维护成本。

## 快速开始

```bash
# 1. 装依赖（虚拟环境在 app/.venv）
cd app && ~/.local/bin/uv sync --group dev && cd ..
#    不想用 uv 的话: python -m venv app/.venv && app/.venv/bin/pip install -r requirements-dev.txt

# 2. 配置 .env（可直接复制 .env.example，至少要填 DATABASE_URL 和 SECRET_KEY）
cp .env.example .env

# 3. 初始化数据库（pgvector 扩展 + users/token/documents 三张表）
psql -U postgres -d knowledge_base -f init_db.sql

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

## 用户接口

前缀 `/api/v1/user`，除注册/登录外都需要请求头 `Authorization: Bearer <token>`。

| 方法 | 路径 | 说明 | data |
| --- | --- | --- | --- |
| POST | `/register` | 注册（用户名、邮箱唯一，密码 bcrypt 存储） | 用户信息 |
| POST | `/login` | 登录，`account` 支持用户名或邮箱 | token + 用户信息 |
| GET | `/info` | 当前用户信息；`?user_id=` 只能查自己 | 用户信息 |
| POST | `/update` | 改用户名/邮箱，只改传了的字段 | 用户信息 |
| POST | `/reset_password` | 改密码，需原密码；成功后所有 token 失效 | null |
| POST | `/logout` | 退出登录，删除当前 token | null |
| POST | `/delete` | 注销账号，需密码确认；关联数据级联删除 | null |
| GET | `/list` | 分页查看用户，仅 `ADMIN_USERNAMES` 里的账号可用 | 分页数据 |

登录签发的 JWT 会写进 `token` 表，因此退出登录、改密码、注销都能让旧 token 立即失效。

## 测试

```bash
app/.venv/bin/python -m pytest      # 用临时 SQLite 跑完整流程，不依赖 PostgreSQL
```
