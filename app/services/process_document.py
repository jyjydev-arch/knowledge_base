import os
import re
import shutil
import uuid as uuid_lib

from fastapi import UploadFile

DATA_ROOT = "data"
SHARE_ROOT = "data/share"  # 别人共享给我的库里，我上传的文件放这里

# 拼路径前必须先过这两条：uuid 段 + 纯字母数字扩展名
# （就算 user_id/kb_id 来自数据库或 token，也不能赌它一定干净 —— 这是唯一一道防目录穿越的闸）
SAFE_UUID_SEGMENT = re.compile(r"^[0-9a-fA-F]{1}(?:-?[0-9a-fA-F]){7,63}$")
SAFE_EXT = re.compile(r"^[A-Za-z0-9]{1,10}$")


def _segment(value, name: str) -> str:
    """校验并**规范化**成带连字符的 uuid 字符串

    规范化这步不是洁癖：同一个 uuid 允许写成有连字符/无连字符/大小写混三种样子，
    不统一的话同一个库会散成好几个目录，对账永远对不上。
    """
    text = str(value)
    compact = text.replace("-", "")
    if not SAFE_UUID_SEGMENT.match(compact):
        raise ValueError(f"{name} 不是合法 uuid，拒绝拼路径: {text!r}")
    return str(uuid_lib.UUID(compact))


def _extension(ext: str) -> str:
    text = str(ext).lower()
    if not SAFE_EXT.match(text):
        raise ValueError(f"扩展名不合法，拒绝拼路径: {ext!r}")
    return text


def kb_own_dir(user_id, kb_id) -> str:
    """库主人自己上传的文件目录：data/{user_id}/{kb_id}"""
    return f"{DATA_ROOT}/{_segment(user_id, 'user_id')}/{_segment(kb_id, 'kb_id')}"


def kb_share_dir(kb_id) -> str:
    """被授权人上传的文件目录：data/share/{kb_id}"""
    return f"{SHARE_ROOT}/{_segment(kb_id, 'kb_id')}"


def document_storage_path(kb_owner_id, uploader_id, kb_id, document_id, ext: str) -> str:
    """唯一的存储路径规则（两个分支都由参数完全可复算，所以事后能算出该删哪份文件）

        库主人自己传： data/{kb_owner_id}/{kb_id}/{document_id}.{ext}
        被授权的人传： data/share/{kb_id}/{document_id}.{ext}

    为什么这么分：
      - 被授权人的文件绝不写进库主人的个人目录（否则 B 的磁盘配额被 A 吃掉，
        删 A 账号时那些文件还会留在 B 目录里变成孤儿）；
      - 也不放 data/{uploader}/ —— 那会让一个库的文件散到 N 个用户目录里，
        对账/备份/删库要遍历一堆地方；
      - 所以上传者目录只装「自己的库」，共享协作一律进 data/share/，
        一个库最多两个目录，路径由 (库owner, 上传者) 决定，可复算。
    """
    if str(kb_owner_id) == str(uploader_id):
        return f"{kb_own_dir(kb_owner_id, kb_id)}/{_segment(document_id, 'document_id')}.{_extension(ext)}"
    return f"{kb_share_dir(kb_id)}/{_segment(document_id, 'document_id')}.{_extension(ext)}"


def kb_storage_dirs(kb_owner_id, kb_id) -> list[str]:
    """某个知识库可能用到的全部目录（对账时两处都要看）"""
    return [kb_own_dir(kb_owner_id, kb_id), kb_share_dir(kb_id)]


def new_document_file_path(
    file: UploadFile, kb_owner_id: str, uploader_id: str, kb_id: str, document_id: str, ext: str
) -> str:
    """把上传的文件落盘，返回相对路径

    - 先建目录：每个库第一次上传时目录还不存在；
    - copyfileobj 流式拷贝，不把整个文件读进内存；
    - 写失败就删掉半截文件再抛 OSError，由调用方决定怎么回滚（service 层不碰 HTTP）。
    """
    file_path = document_storage_path(kb_owner_id, uploader_id, kb_id, document_id, ext)
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


def delete_document_file(
    kb_owner_id: str, uploader_id: str, kb_id: str, documents_id: str, documents_format: str
) -> bool:
    """删除磁盘文件：删掉返回 True，文件本来就不在返回 False（不抛异常）

    uploader_id 传 documents.owner_id（上传者），这样能精确复算出当初落盘的位置。
    """
    file_path = document_storage_path(kb_owner_id, uploader_id, kb_id, documents_id, documents_format)
    if os.path.exists(file_path):
        os.remove(file_path)
        return True
    return False


def search_document_files(kb_owner_id, kb_id) -> list[str]:
    """列出该库两个可能目录里的文件名（合并去重）

    目录不存在返回空列表而不是抛 FileNotFoundError —— 否则 /status 对新建空库直接 500。
    """
    names: set[str] = set()
    for directory in kb_storage_dirs(kb_owner_id, kb_id):
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            if os.path.isfile(os.path.join(directory, name)):
                names.add(name)
    return sorted(names)


def missing_file_paths(file_paths: list[str]) -> list[str]:
    """库里记录说文件应该存在，实际不在 —— 按每条记录的 file_path 精确判，最可靠"""
    return [path for path in file_paths if not os.path.isfile(path)]


def remove_kb_directories_if_empty(kb_owner_id, kb_id) -> bool:
    """把空出来的库目录收掉（两个目录都清；只剩空目录才删，绝不 rmtree）"""
    removed = False
    for directory in kb_storage_dirs(kb_owner_id, kb_id):
        try:
            if os.path.isdir(directory) and not os.listdir(directory):
                os.rmdir(directory)
                removed = True
        except OSError:
            continue
    return removed
