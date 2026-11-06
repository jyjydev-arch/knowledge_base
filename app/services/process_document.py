import os
import re
import shutil

from fastapi import UploadFile

DATA_ROOT = "data"

# 拼路径前必须先过这两条：uuid 段 + 纯字母数字扩展名
# （就算 user_id/kb_id 来自数据库或 token，也不能赌它一定干净 —— 这是唯一一道防目录穿越的闸）
SAFE_UUID_SEGMENT = re.compile(r"^[0-9a-fA-F]{1}(?:-?[0-9a-fA-F]){7,63}$")
SAFE_EXT = re.compile(r"^[A-Za-z0-9]{1,10}$")


def _segment(value, name: str) -> str:
    text = str(value)
    if not SAFE_UUID_SEGMENT.match(text.replace("-", "")) or "/" in text or "\\" in text or ".." in text:
        raise ValueError(f"{name} 不是合法 uuid，拒绝拼路径: {text!r}")
    return text


def _extension(ext: str) -> str:
    text = str(ext).lower()
    if not SAFE_EXT.match(text):
        raise ValueError(f"扩展名不合法，拒绝拼路径: {ext!r}")
    return text


def kb_storage_dir(user_id, kb_id) -> str:
    """某个知识库的磁盘目录：data/{user_id}/{kb_id}"""
    return f"{DATA_ROOT}/{_segment(user_id, 'user_id')}/{_segment(kb_id, 'kb_id')}"


def document_storage_path(user_id, kb_id, document_id, ext: str) -> str:
    """唯一的存储路径约定：data/{user_id}/{kb_id}/{document_id}.{ext}

    按知识库分目录，好处是「库里有没有这个文件」可以直接 ls 整个目录做一致性核对（/status 就这么用），
    删库时也能整目录清理；相对进程 CWD（start.sh 在项目根启动才成立）。
    """
    return f"{kb_storage_dir(user_id, kb_id)}/{_segment(document_id, 'document_id')}.{_extension(ext)}"


def new_document_file_path(file: UploadFile, user_id: str, kb_id: str, document_id: str, ext: str) -> str:
    """把上传的文件落盘，返回相对路径

    - 先建目录：data/{user_id}/{kb_id}/ 第一次上传时还不存在；
    - copyfileobj 流式拷贝，不把整个文件读进内存；
    - 写失败就删掉半截文件再抛 OSError，由调用方决定怎么回滚（service 层不碰 HTTP，见 DEVELOPMENT.md）。
    """
    file_path = document_storage_path(user_id, kb_id, document_id, ext)
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    try:
        with open(file_path, "wb") as target:
            shutil.copyfileobj(file.file, target)
    except OSError:
        try:
            os.remove(file_path)
        except OSError:
            pass
        raise

    return file_path


def delete_document_file(user_id: str, kb_id: str, documents_id: str, documents_format: str) -> bool:
    """删除磁盘文件：删掉返回 True，文件本来就不在返回 False（不抛异常）"""
    file_path = document_storage_path(user_id, kb_id, documents_id, documents_format)
    if os.path.exists(file_path):
        os.remove(file_path)
        return True
    return False


def search_document_file(user_id: str, kb_id: str) -> list[str]:
    """列出某个知识库目录下的文件名，用于和数据库核对

    目录不存在（还没上传过任何文件）返回空列表 —— 不能让它抛 FileNotFoundError，
    否则 /status 对新建的空库直接 500。
    """
    directory = kb_storage_dir(user_id, kb_id)
    if not os.path.isdir(directory):
        return []
    return sorted(name for name in os.listdir(directory) if os.path.isfile(os.path.join(directory, name)))


def remove_kb_directory_if_empty(user_id: str, kb_id: str) -> bool:
    """删掉空的知识库目录（只剩目录没文件时才删，避免误删还在用的数据）"""
    directory = kb_storage_dir(user_id, kb_id)
    try:
        if os.path.isdir(directory) and not os.listdir(directory):
            os.rmdir(directory)
            return True
    except OSError:
        return False
    return False
