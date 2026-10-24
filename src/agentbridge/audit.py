import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .errors import BridgeError
from .redact import redact
from .storage import file_lock, private_dir, write_json


class AuditLog:
    def __init__(self, log_dir, public_context, secrets=(), *, shard_size=64 * 1024 * 1024):
        self.root = private_dir(Path(log_dir))
        self.context = public_context
        self.secrets = secrets
        self.shard_size = shard_size

    def append(self, event, status, details=None, *, critical=True, **payload):
        try:
            with file_lock(self.root / ".audit.lock"):
                state_path = self.root / ".audit-seq.json"
                state = json.loads(state_path.read_text()) if state_path.exists() else {"seq": 0, "segment": 0}
                active = self.root / "trace.jsonl"
                if active.exists() and active.stat().st_size >= self.shard_size:
                    os.replace(active, self.root / f"trace.{state['segment']:08d}.jsonl")
                    state["segment"] += 1
                state["seq"] += 1
                now = datetime.now(timezone.utc).isoformat()
                row = {**self.context, "schema_version": 1, "event_id": str(uuid4()), "source": "local",
                       "source_seq": state["seq"], "timestamp": now, "received_at": now,
                       "event": event, "status": status, "details": details or {}, **payload}
                row = redact(row, self.secrets)
                write_json(state_path, state)
                encoded = (json.dumps(row, ensure_ascii=False) + "\n").encode()
                fd = os.open(active, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "ab") as stream:
                    stream.write(encoded)
                    stream.flush()
                    if critical:
                        os.fsync(stream.fileno())
                return row
        except OSError as error:
            raise BridgeError("audit_unavailable", "审计记录无法写入") from error

    def read(self, cursor=0, limit=1000):
        events, gaps = [], []
        remaining, position = int(cursor), int(cursor)
        with file_lock(self.root / ".audit.lock"):
            for path in sorted(self.root.glob("trace.[0-9]*.jsonl")) + [self.root / "trace.jsonl"]:
                if not path.exists():
                    continue
                size = path.stat().st_size
                if remaining >= size:
                    remaining -= size
                    continue
                with path.open("rb") as stream:
                    stream.seek(remaining)
                    remaining = 0
                    for line in stream:
                        if not line.endswith(b"\n"):
                            gaps.append({"file": path.name, "offset": stream.tell() - len(line), "reason": "partial_line"})
                            break
                        position += len(line)
                        try:
                            events.append(json.loads(line))
                        except (ValueError, UnicodeDecodeError):
                            gaps.append({"file": path.name, "reason": "invalid_event"})
                        if len(events) >= limit:
                            return {"events": events, "next_cursor": position, "gaps": gaps, "complete": False}
        return {"events": events, "next_cursor": position, "gaps": gaps, "complete": not gaps}
