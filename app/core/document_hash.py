"""上传文件的内容指纹"""

import hashlib
from collections.abc import Awaitable, Callable

from fastapi import UploadFile

from app.core.exceptions import BusinessError

# 每次读 1MB：几 MB 的文件也不会一次性进内存
HASH_CHUNK_SIZE = 1024 * 1024


async def calculate_document_hash(
    file: UploadFile,
    *,
    max_bytes: int | None = None,
    on_progress: Callable[[int], Awaitable[None]] | None = None,
) -> tuple[str, int]:
    """流式计算 sha256，同时统计字节数

    返回 (sha256_hex, size)。

    几个必须注意的点：
      - UploadFile.read() 是 async 的，直接 hashlib.sha256(UploadFile) 会 TypeError；
      - 读完要把指针拨回 0，后面 new_document_file_path 落盘还要从头拷；
      - max_bytes 在读取过程中就判（而不是信 content-length），
        否则一个 5GB 的包会先把磁盘/内存吃掉再报错。
    """
    digest = hashlib.sha256()
    total = 0

    await file.seek(0)
    while chunk := await file.read(HASH_CHUNK_SIZE):
        total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            raise BusinessError(
                f"文件超过大小限制 {max_bytes // (1024 * 1024)}MB", status=413
            )
        digest.update(chunk)
    await file.seek(0)

    if on_progress is not None:
        await on_progress(total)

    return digest.hexdigest(), total
