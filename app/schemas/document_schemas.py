"""文档相关的请求 / 响应模型

文档 = 用户上传的一个原始文件（pdf/docx/md/txt…），挂在某个知识库（knowledge_bases）下面。
切块内容在 chunks 表，检索接口到 M3/M4 再做，这里先只管文件的增删查。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.db.models import DocStatus


class DocumentResponse(BaseModel):
    """文档基本信息（列表和详情共用）"""

    id: uuid.UUID = Field(description="文档ID")
    kb_id: uuid.UUID = Field(description="所属知识库ID")
    title: str = Field(description="展示用标题")
    filename: str = Field(description="上传时的原始文件名")
    file_size: int = Field(description="文件字节数")
    mime_type: str | None = Field(default=None, description="MIME 类型")
    status: DocStatus = Field(description="入库状态: pending/parsing/chunking/indexed/failed")
    error_msg: str | None = Field(default=None, description="失败原因，成功时为 null")
    chunk_count: int = Field(default=0, description="切出的块数量")
    token_count: int = Field(default=0, description="正文字符数（近似 token）")
    created_at: datetime | None = Field(default=None, description="上传时间")
    updated_at: datetime | None = Field(default=None, description="最后更新时间")

    model_config = ConfigDict(from_attributes=True)

    @computed_field(description="下载地址（相对路径，前端拼上 API 前缀即可）")
    @property
    def download_url(self) -> str:
        return f"/documents/download/{self.id}"


class DocumentDetail(DocumentResponse):
    """详情：额外带上正文（截断）与所属知识库的版本信息"""

    content_preview: str | None = Field(default=None, description="正文前 2000 字，供预览")
    kb_name: str | None = Field(default=None, description="所属知识库名称")
    kb_version: int | None = Field(default=None, description="所属知识库当前版本号")
