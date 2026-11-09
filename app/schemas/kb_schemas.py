"""知识库共享授权的请求 / 响应模型"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.db.models import KbPermission


class KbShareCreate(BaseModel):
    """授权请求体

    只按 email 找人（用户名可能重名语义不明确，且 email 是唯一的）；
    重复授权 = 更新权限，所以这个接口是幂等的。
    """

    email: EmailStr = Field(description="被授权用户的邮箱")
    permission: KbPermission = Field(
        default=KbPermission.READ,
        description="read=只读（可看列表/详情/下载）；write=可读写（可上传、删除文档）",
    )

    model_config = ConfigDict(str_strip_whitespace=True)


class KbShareOut(BaseModel):
    """一条授权记录（被授权人信息一并返回，省得前端再查一次用户）"""

    share_id: uuid.UUID = Field(description="授权记录ID")
    kb_id: uuid.UUID = Field(description="知识库ID")
    grantee_id: uuid.UUID = Field(description="被授权用户ID")
    grantee_username: str = Field(description="被授权用户名")
    grantee_email: EmailStr = Field(description="被授权用户邮箱")
    permission: KbPermission = Field(description="read / write")
    created_at: datetime | None = Field(default=None, description="授权时间")
