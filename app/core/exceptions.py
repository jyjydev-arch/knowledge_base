"""业务异常

统一在这里定义异常，路由里只管 raise，
由 app/main.py 里的 exception_handler 统一包装成
{"status": ..., "msg": ..., "data": ...} 的返回格式。
"""


class BusinessError(Exception):
    """业务异常基类

    Args:
        msg: 给用户看的提示信息
        status: 业务状态码，同时作为 HTTP 状态码返回
        data: 需要一起带回的额外信息（一般是 None）
    """

    def __init__(self, msg: str = "请求失败", status: int = 400, data=None) -> None:
        self.msg = msg
        self.status = status
        self.data = data
        super().__init__(msg)


class ParamError(BusinessError):
    """参数不合法"""

    def __init__(self, msg: str = "请求参数不合法", data=None) -> None:
        super().__init__(msg=msg, status=400, data=data)


class UnauthorizedError(BusinessError):
    """未登录 / token 无效或过期"""

    def __init__(self, msg: str = "未登录或登录已失效", data=None) -> None:
        super().__init__(msg=msg, status=401, data=data)


class ForbiddenError(BusinessError):
    """登录了但没权限"""

    def __init__(self, msg: str = "没有权限执行该操作", data=None) -> None:
        super().__init__(msg=msg, status=403, data=data)


class NotFoundError(BusinessError):
    """资源不存在"""

    def __init__(self, msg: str = "资源不存在", data=None) -> None:
        super().__init__(msg=msg, status=404, data=data)


class ConflictError(BusinessError):
    """数据冲突，例如用户名/邮箱已被占用"""

    def __init__(self, msg: str = "数据冲突", data=None) -> None:
        super().__init__(msg=msg, status=409, data=data)
