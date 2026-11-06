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

    content_preview: str | None = Field(default=None, description="正文预览（按块序拼接后截断）")
    kb_name: str | None = Field(default=None, description="所属知识库名称")
    kb_version: int | None = Field(default=None, description="所属知识库当前版本号")
    chunk_total: int = Field(default=0, description="该文档共有多少块")


class DocumentStatusOut(BaseModel):
    """知识库状态：解析进度 + 磁盘与数据库是否一致

    判定口径：
        missing_files  = 数据库有记录、磁盘上找不到（文档打不开，需要重新上传/重索引）
        orphan_files   = 磁盘上有文件、数据库里没有对应记录（占空间的垃圾，需要清理）
    两者都空才算 healthy。
    """

    kb_id: uuid.UUID = Field(description="知识库ID")
    kb_version: int = Field(description="知识库当前版本号")
    last_indexed_at: datetime | None = Field(default=None, description="最后一次统计/索引时间")
    document_total: int = Field(default=0, description="该知识库的文档数（不含软删）")
    status_counts: dict[str, int] = Field(default_factory=dict, description="各解析状态的文档数量")
    disk_file_count: int = Field(default=0, description="知识库目录下实际的文件个数")
    missing_files: list[str] = Field(default_factory=list, description="库里有记录、磁盘上没有的文件名")
    orphan_files: list[str] = Field(default_factory=list, description="磁盘上有、库里没有记录的文件名")
    healthy: bool = Field(default=True, description="是否一致（missing 与 orphan 都为空）")
