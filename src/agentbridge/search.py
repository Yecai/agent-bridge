import base64
import hashlib
import json
import subprocess
import threading
from pathlib import Path

from .errors import BridgeError


class SearchTools:
    def __init__(self, workspace):
        self.workspace = workspace

    @staticmethod
    def _page(values, query, cursor, limit):
        digest = hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()
        offset = 0
        if cursor:
            try:
                decoded = json.loads(base64.urlsafe_b64decode(cursor))
                if decoded["query"] != digest:
                    raise ValueError()
                offset = int(decoded["offset"])
                if offset < 0:
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise BridgeError("invalid_cursor", "搜索游标与查询不匹配") from None
        limit = min(500, max(1, limit))
        end = offset + limit
        next_cursor = base64.urlsafe_b64encode(json.dumps({"query": digest, "offset": end}).encode()).decode() if end < len(values) else None
        return values[offset:end], next_cursor

    def glob(self, pattern, path=".", cursor=None, limit=500):
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise BridgeError("invalid_pattern", "模式必须位于授权目录内")
        root = self.workspace.resolve(path)
        values = []
        for item in sorted(root.glob(pattern)):
            if item.is_symlink():
                continue
            self.workspace.resolve(item)
            values.append(str(item))
        page, next_cursor = self._page(values, [str(root), pattern], cursor, limit)
        return {"paths": page, "next_cursor": next_cursor}

    def grep(self, pattern, path=".", cursor=None, limit=500):
        root = self.workspace.resolve(path)
        try:
            result = subprocess.run(["rg", "--json", "--hidden", "--glob", "!.git/**", "--regexp", pattern, "--", str(root)],
                                    capture_output=True, timeout=10)
        except FileNotFoundError:
            raise BridgeError("search_unavailable", "本机需要 ripgrep") from None
        except subprocess.TimeoutExpired:
            raise BridgeError("search_timeout", "搜索超过十秒") from None
        if result.returncode not in (0, 1):
            raise BridgeError("invalid_pattern", "搜索模式无效")
        values = []
        for line in result.stdout.splitlines():
            row = json.loads(line)
            if row["type"] == "match":
                data = row["data"]
                filename = data["path"].get("text")
                if filename:
                    self.workspace.resolve(filename)
                    values.append({"path": filename, "line": data["line_number"], "text": data["lines"].get("text", "")})
        page, next_cursor = self._page(values, [str(root), pattern], cursor, limit)
        return {"matches": page, "next_cursor": next_cursor}
