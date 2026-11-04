"""用户接口测试：统一返回格式 + 各条用户路由的业务逻辑"""

ENVELOPE_KEYS = {"status", "msg", "data"}


def assert_envelope(resp, status: int):
    """所有接口（含报错）都必须是 {"status","msg","data"} 三段式"""
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body.keys()) == ENVELOPE_KEYS, body
    assert body["status"] == status, body
    assert isinstance(body["msg"], str) and body["msg"]
    return body


# ---------------- 注册 ----------------
def test_register_success(register):
    body = assert_envelope(register(), 200)
    assert body["msg"] == "注册成功"
    assert body["data"]["username"] == "zhangsan"
    assert body["data"]["email"] == "zhangsan@example.com"
    assert "hashed_password" not in body["data"]
    assert body["data"]["id"]


def test_register_duplicate_username(register):
    register()
    body = assert_envelope(register(email="other@example.com"), 409)
    assert "用户名" in body["msg"]
    assert body["data"] is None


def test_register_duplicate_email(register):
    register()
    body = assert_envelope(register(username="lisi"), 409)
    assert "邮箱" in body["msg"]


def test_register_invalid_params(client):
    body = assert_envelope(
        client.post(
            "/api/v1/user/register",
            json={"username": "a", "email": "not-an-email", "password": "123"},
        ),
        422,
    )
    assert "参数校验失败" in body["msg"]
    assert body["data"]["errors"]


# ---------------- 登录 ----------------
def test_login_by_username(register, login):
    register()
    body = assert_envelope(login("zhangsan"), 200)
    data = body["data"]
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == 60 * 60 * 24
    assert data["user"]["username"] == "zhangsan"
    assert data["access_token"].count(".") == 2


def test_login_by_email(register, login):
    register()
    assert_envelope(login("zhangsan@example.com"), 200)


def test_login_wrong_password(register, login):
    register()
    body = assert_envelope(login("zhangsan", "wrong-password"), 401)
    assert body["msg"] == "用户名或密码错误"


def test_login_user_not_exist(login):
    assert_envelope(login("nobody"), 401)


def test_login_compat_username_field(register, client):
    """前端老写法：只传 username"""
    register()
    resp = client.post(
        "/api/v1/user/login", json={"username": "zhangsan", "password": "abc123456"}
    )
    assert_envelope(resp, 200)


# ---------------- 查询个人信息 ----------------
def test_info_requires_login(client):
    assert_envelope(client.get("/api/v1/user/info"), 401)


def test_info_invalid_token(client):
    assert_envelope(
        client.get("/api/v1/user/info", headers={"Authorization": "Bearer not-a-jwt"}), 401
    )


def test_info_returns_current_user(client, user_headers):
    body = assert_envelope(client.get("/api/v1/user/info", headers=user_headers), 200)
    assert body["data"]["username"] == "zhangsan"


def test_info_ignores_user_id_param(client, user_headers, register, login):
    """身份只来自 token：传别人的 user_id 也只会返回自己的信息（不再是个越权入口）"""
    register(username="lisi", email="lisi@example.com")
    other_id = login("lisi").json()["data"]["user"]["id"]

    body = assert_envelope(
        client.get(f"/api/v1/user/info?user_id={other_id}", headers=user_headers), 200
    )
    assert body["data"]["username"] == "zhangsan"


# ---------------- 修改资料 ----------------
def test_update_username(client, user_headers):
    body = assert_envelope(
        client.post("/api/v1/user/update", json={"username": "newname"}, headers=user_headers), 200
    )
    assert body["data"]["username"] == "newname"
    assert body["data"]["email"] == "zhangsan@example.com"


def test_update_conflict(client, user_headers, register, other_headers):
    body = assert_envelope(
        client.post("/api/v1/user/update", json={"username": "lisi"}, headers=user_headers), 409
    )
    assert "已被占用" in body["msg"]


def test_update_requires_payload(client, user_headers):
    assert_envelope(client.post("/api/v1/user/update", json={}, headers=user_headers), 400)


# ---------------- 修改密码 ----------------
def test_reset_password_invalidates_tokens(client, user_headers, login):
    body = assert_envelope(
        client.post(
            "/api/v1/user/reset_password",
            json={"old_password": "abc123456", "new_password": "newpass123"},
            headers=user_headers,
        ),
        200,
    )
    assert "重新登录" in body["msg"]
    # 旧 token 已失效
    assert_envelope(client.get("/api/v1/user/info", headers=user_headers), 401)
    # 新密码可以登录
    assert_envelope(login("zhangsan", "newpass123"), 200)


def test_reset_password_wrong_old_password(client, user_headers):
    assert_envelope(
        client.post(
            "/api/v1/user/reset_password",
            json={"old_password": "badpass", "new_password": "newpass123"},
            headers=user_headers,
        ),
        401,
    )


def test_reset_password_same_as_old(client, user_headers):
    assert_envelope(
        client.post(
            "/api/v1/user/reset_password",
            json={"old_password": "abc123456", "new_password": "abc123456"},
            headers=user_headers,
        ),
        422,
    )


# ---------------- 退出登录 ----------------
def test_logout(client, user_headers, login):
    body = assert_envelope(client.post("/api/v1/user/logout", headers=user_headers), 200)
    assert body["data"] is None
    assert_envelope(client.get("/api/v1/user/info", headers=user_headers), 401)
    # 重新登录后拿到新 token
    assert_envelope(login("zhangsan"), 200)


def test_logout_without_token(client):
    assert_envelope(client.post("/api/v1/user/logout"), 401)


# ---------------- 注销账号 ----------------
def test_delete_account(client, user_headers, login):
    body = assert_envelope(
        client.post("/api/v1/user/delete", json={"password": "abc123456"}, headers=user_headers),
        200,
    )
    assert "注销" in body["msg"]
    assert_envelope(client.get("/api/v1/user/info", headers=user_headers), 401)
    assert_envelope(login("zhangsan"), 401)


def test_delete_account_wrong_password(client, user_headers):
    assert_envelope(
        client.post("/api/v1/user/delete", json={"password": "wrong123"}, headers=user_headers), 401
    )


# ---------------- 管理员列表 ----------------
def test_list_requires_admin(client, user_headers):
    body = assert_envelope(client.get("/api/v1/user/list", headers=user_headers), 403)
    assert "管理员" in body["msg"]


def test_list_for_admin(client, admin_headers, register):
    register(username="zhangsan", email="zhangsan@example.com")
    body = assert_envelope(client.get("/api/v1/user/list?page=1&size=10", headers=admin_headers), 200)
    data = body["data"]
    assert data["total"] == 2
    assert data["page"] == 1 and data["size"] == 10
    assert {item["username"] for item in data["items"]} == {"admin", "zhangsan"}


def test_list_bad_pagination(client, admin_headers):
    assert_envelope(client.get("/api/v1/user/list?page=0", headers=admin_headers), 422)


# ---------------- 框架级错误也要走统一格式 ----------------
def test_unknown_route(client):
    assert_envelope(client.get("/api/v1/not-exist"), 404)


def test_method_not_allowed(client):
    assert_envelope(client.get("/api/v1/user/register"), 405)


def test_health_and_docs(client):
    assert_envelope(client.get("/health"), 200)
    schema = client.get("/openapi.json").json()
    # 统一响应模型进了 OpenAPI，前端可以直接生成 TS 类型
    ref = schema["paths"]["/api/v1/user/info"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert "$ref" in str(ref) or "ApiResponse" in str(schema["components"]["schemas"])


# ---------------- 新表字段：role / is_active / last_login_at ----------------
def test_register_role_bootstrap(register, client):
    """普通注册是 user；用户名命中 ADMIN_USERNAMES 时注册即 admin（第一个管理员的引导）"""
    first = register().json()["data"]
    assert first["role"] == "user"
    assert first["is_active"] is True

    resp = client.post(
        "/api/v1/user/register",
        json={"username": "admin", "email": "admin@example.com", "password": "abc123456"},
    )
    assert resp.json()["data"]["role"] == "admin"


def test_login_records_last_login(client, user_headers):
    """登录成功要刷新 last_login_at（tokens 表里也能看到来源 ip/UA）"""
    body = assert_envelope(client.get("/api/v1/user/info", headers=user_headers), 200)
    assert body["data"]["last_login_at"]


def test_disabled_user_is_forbidden(client, user_headers, run):
    """is_active=false 的账号：带着仍然合法的 token 也要被挡住（403）"""
    from sqlalchemy import update

    from app.db.models import User

    async def _disable(session):
        await session.execute(update(User).where(User.username == "zhangsan").values(is_active=False))
        await session.commit()

    run(_disable)
    body = assert_envelope(client.get("/api/v1/user/info", headers=user_headers), 403)
    assert "禁用" in body["msg"]


def test_login_token_is_hashed_in_db(client, user_headers, run):
    """库里的登录凭证只存 sha256，不存 JWT 原文"""
    from sqlalchemy import select

    from app.db.models import Token

    rows = run(lambda session: session.execute(select(Token)))
    tokens = rows.scalars().all()
    assert tokens, "登录应该留下一条 token 记录"
    for record in tokens:
        assert len(record.token_hash) == 64 and "." not in record.token_hash
        assert record.user_agent is not None or record.client_ip is not None
