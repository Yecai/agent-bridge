import os
from contextlib import contextmanager
from pathlib import Path

from .errors import BridgeError


class Workspace:
    def __init__(self, cwd, read_roots, write_roots):
        self.cwd = Path(cwd).resolve(strict=True)
        self.read_roots = tuple(Path(p).resolve(strict=True) for p in read_roots)
        self.write_roots = tuple(Path(p).resolve(strict=True) for p in write_roots)

    def resolve(self, path, *, write=False):
        path = Path(path)
        candidate = (path if path.is_absolute() else self.cwd / path).resolve()
        roots = self.write_roots if write else self.read_roots
        if not any(candidate == root or candidate.is_relative_to(root) for root in roots):
            raise BridgeError("path_not_allowed", "路径不在本次授权范围")
        return candidate

    @contextmanager
    def parent(self, path, *, write=False):
        path = self.resolve(path, write=write)
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parent.parts[1:]:
                new_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = new_fd
            yield fd, path.name
        except OSError as error:
            raise BridgeError("path_unavailable", "路径不存在或已发生变化") from error
        finally:
            os.close(fd)
