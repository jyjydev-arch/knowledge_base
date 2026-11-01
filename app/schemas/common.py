"""统一响应格式

所有接口都返回:
    {"status": 200, "msg": "success", "data": {...}}

用法:
    # 1. 作为 FastAPI 的 response_model，可以拿到带具体类型的 Swagger 文档
    @router.get("/info", response_model=ApiResponse[UserResponse])
    async def info(user: CurrentUser):
        return ApiResponse.ok(UserResponse.model_validate(user), msg="获取成功")

    # 2. 没有返回体时用 ApiResponse[None]
    return ApiResponse.ok(msg="操作成功")

    # 3. 失败一般不用手写，直接 raise BusinessError / NotFoundError，
    #    由 app/main.py 的异常处理器统一转成这个格式。
"""

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

# 数据体的类型占位符
T = TypeVar("T")

SUCCESS_STATUS = 200


class ApiResponse(BaseModel, Generic[T]):
    """通用接口返回格式"""

    status: int = Field(default=SUCCESS_STATUS, description="业务状态码，与 HTTP 状态码保持一致，200 为成功")
    msg: str = Field(default="success", description="提示信息，失败时是错误原因")
    data: T | None = Field(default=None, description="业务数据，没有数据时为 null")

    model_config = {"json_schema_extra": {"example": {"status": 200, "msg": "success", "data": {}}}}

    @classmethod
    def ok(cls, data: T | None = None, msg: str = "success", status: int = SUCCESS_STATUS) -> "ApiResponse[T]":
        """成功返回"""
        return cls(status=status, msg=msg, data=data)

    @classmethod
    def fail(cls, msg: str = "error", status: int = 400, data: T | None = None) -> "ApiResponse[T]":
        """失败返回"""
        return cls(status=status, msg=msg, data=data)

    @classmethod
    def build(cls, *, status: int, msg: str, data: T | None = None) -> "ApiResponse[T]":
        return cls(status=status, msg=msg, data=data)


class PageData(BaseModel, Generic[T]):
    """列表接口的分页数据体"""

    items: list[T] = Field(default_factory=list, description="当前页数据")
    total: int = Field(default=0, description="总条数")
    page: int = Field(default=1, description="当前页码，从 1 开始")
    size: int = Field(default=20, description="每页条数")

    @property
    def pages(self) -> int:
        return (self.total + self.size - 1) // self.size if self.size else 0


class PageParams(BaseModel):
    """分页查询参数"""

    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    size: int = Field(default=20, ge=1, le=100, description="每页条数，最大 100")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size

    @property
    def limit(self) -> int:
        return self.size
