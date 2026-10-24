import hashlib
import json
import os
import zipfile
from pathlib import Path

from .errors import BridgeError
from .redact import StreamRedactor
from .storage import file_lock, private_dir, write_json


class Artifacts:
    def __init__(self, root, secrets=(), *, shard_size=64 * 1024 * 1024):
        self.root = private_dir(Path(root))
        self.secrets, self.shard_size = secrets, shard_size
        self.redactors = {}

    def manifest(self):
        path = self.root / "manifest.json"
        return json.loads(path.read_text()) if path.exists() else {"streams": {}, "artifacts": {}}

    def append(self, stream_id: str, data: bytes):
        return self._append(stream_id, self.redactors.setdefault(stream_id, StreamRedactor(self.secrets)).feed(data))

    def finish(self, stream_id: str):
        stream = self.redactors.pop(stream_id, None)
        return self._append(stream_id, stream.finish() if stream else b"")

    def _append(self, stream_id, data):
        with file_lock(self.root / ".lock"):
            manifest = self.manifest()
            refs = manifest["streams"].setdefault(stream_id, [])
            while data:
                if not refs or manifest["artifacts"][refs[-1]]["size"] >= self.shard_size:
                    ref = hashlib.sha256(f"{stream_id}:{len(refs)}".encode()).hexdigest()
                    refs.append(ref)
                    manifest["artifacts"][ref] = {"file": ref + ".bin", "size": 0}
                item = manifest["artifacts"][refs[-1]]
                count = min(len(data), self.shard_size - item["size"])
                fd = os.open(self.root / item["file"], os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "ab") as stream:
                    stream.write(data[:count])
                    stream.flush()
                    os.fsync(stream.fileno())
                item["size"] += count
                item["sha256"] = hashlib.sha256((self.root / item["file"]).read_bytes()).hexdigest()
                data = data[count:]
            write_json(self.root / "manifest.json", manifest)
            return list(refs)

    def put(self, data: bytes):
        stream_id = "content-" + hashlib.sha256(data).hexdigest()
        self.append(stream_id, data)
        self.finish(stream_id)
        return self.manifest()["streams"][stream_id]

    def read(self, ref, offset=0, limit=65536):
        with file_lock(self.root / ".lock"):
            item = self.manifest()["artifacts"].get(ref)
            if item is None:
                raise BridgeError("artifact_not_found", "附件不存在")
            with (self.root / item["file"]).open("rb") as stream:
                stream.seek(max(0, offset))
                return stream.read(min(max(limit, 1), 1024 * 1024))

    def output(self, stream_id, cursor=0, limit=65536):
        data, skip = bytearray(), max(0, cursor)
        manifest = self.manifest()
        for ref in manifest["streams"].get(stream_id, []):
            size = manifest["artifacts"][ref]["size"]
            if skip >= size:
                skip -= size
                continue
            chunk = self.read(ref, skip, limit - len(data))
            data.extend(chunk)
            skip = 0
            if len(data) >= limit:
                break
        return {"data": bytes(data), "next_cursor": cursor + len(data), "artifact_refs": manifest["streams"].get(stream_id, [])}

    def export(self, destination):
        with file_lock(self.root / ".lock"):
            manifest = self.manifest()
            with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest))
                for item in manifest["artifacts"].values():
                    archive.write(self.root / item["file"], item["file"])
        os.chmod(destination, 0o600)
        return Path(destination)
