import base64
import fcntl
import hashlib
import mimetypes
import os
import stat
from uuid import uuid4

from .errors import BridgeError


class FileTools:
    def __init__(self, workspace):
        self.workspace = workspace

    def workspace_context(self):
        rules = []
        for root in self.workspace.read_roots:
            for name in ("AGENTS.md", "CLAUDE.md"):
                path = root / name
                if path.is_file():
                    rules.append(str(path))
        return {"cwd": str(self.workspace.cwd), "platform": os.uname().sysname,
                "read_roots": [str(p) for p in self.workspace.read_roots],
                "write_roots": [str(p) for p in self.workspace.write_roots], "rules": rules}

    def _bytes(self, path):
        with self.workspace.parent(path) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise BridgeError("file_not_regular", "只能读取普通文件")
                data = stream.read()
            return data

    def read_file(self, path, offset=0, limit=1024 * 1024):
        if offset < 0 or limit < 1 or limit > 16 * 1024 * 1024:
            raise BridgeError("invalid_range", "读取范围无效")
        data = self._bytes(path)
        body = data[offset:offset + limit]
        result = {"path": str(self.workspace.resolve(path)), "version": hashlib.sha256(data).hexdigest(),
                  "size": len(data), "next_offset": offset + len(body), "truncated": offset + len(body) < len(data)}
        mime = mimetypes.guess_type(str(path))[0]
        if mime in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            if offset or len(body) != len(data):
                raise BridgeError("image_too_large", "图片必须完整读取")
            result.update(image=base64.b64encode(body).decode(), mime_type=mime)
        else:
            try:
                result["text"] = body.decode("utf-8")
            except UnicodeDecodeError:
                result["binary"] = True
        return result

    def list_directory(self, path=".", cursor=0, limit=500):
        root = self.workspace.resolve(path)
        values = sorted(root.iterdir(), key=lambda p: p.name)
        cursor, limit = max(0, int(cursor)), min(500, max(1, int(limit)))
        rows = [{"name": p.name, "symlink": p.is_symlink(), "directory": p.is_dir() if not p.is_symlink() else False}
                for p in values[cursor:cursor + limit]]
        return {"entries": rows, "next_cursor": cursor + limit if cursor + limit < len(values) else None}

    def write_file(self, path, content, expected_version=None, create=False):
        data = content.encode("utf-8")
        return self._write(path, data, expected_version, create)

    def _write(self, path, data, expected, create):
        with self.workspace.parent(path, write=True) as (parent, name):
            fcntl.flock(parent, fcntl.LOCK_EX)
            mode = 0o644
            before = None
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                if not create:
                    raise BridgeError("file_conflict", "新文件需要明确创建") from None
            else:
                with os.fdopen(fd, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise BridgeError("file_not_regular", "只能写入普通文件")
                    version = hashlib.sha256(stream.read()).hexdigest()
                    mode = stat.S_IMODE(before.st_mode)
                if create or version != expected:
                    raise BridgeError("file_conflict", "目标已经存在或版本发生变化")
            temporary = ".agentbridge-" + uuid4().hex
            try:
                fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode, dir_fd=parent)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if before is None:
                    try:
                        os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                    except FileExistsError:
                        raise BridgeError("file_conflict", "目标在创建期间已被创建") from None
                else:
                    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if (current.st_ino, current.st_mtime_ns, current.st_size) != (before.st_ino, before.st_mtime_ns, before.st_size):
                        raise BridgeError("file_conflict", "目标在写入期间发生变化")
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
                fcntl.flock(parent, fcntl.LOCK_UN)
        return {"path": str(self.workspace.resolve(path)), "version": hashlib.sha256(data).hexdigest(), "size": len(data)}

    def apply_patch(self, path, expected_version, old, new):
        before = self._bytes(path)
        if hashlib.sha256(before).hexdigest() != expected_version:
            raise BridgeError("file_conflict", "文件版本已改变")
        if not old or before.count(old.encode()) != 1:
            raise BridgeError("patch_conflict", "替换片段必须唯一且非空")
        return self._write(path, before.replace(old.encode(), new.encode(), 1), expected_version, False)
