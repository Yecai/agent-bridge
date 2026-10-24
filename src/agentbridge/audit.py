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

    @staticmethod
    def _previous_sequence(path, offset):
        with path.open('rb') as stream:
            stream.seek(max(0, offset - 16 * 1024 * 1024))
            tail = stream.read(min(offset, 16 * 1024 * 1024))
        for line in reversed(tail.splitlines()):
            try:
                value = json.loads(line)
                if isinstance(value, dict) and type(value.get('source_seq')) is int:
                    return value['source_seq']
            except (ValueError, UnicodeDecodeError):
                continue
        return 0

    def read(self, cursor=0, limit=1000):
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or limit < 1:
            raise BridgeError('invalid_cursor', '日志游标或分页大小无效')
        events, gaps = [], []
        previous = 0
        previous_location = None
        remaining, position = int(cursor), int(cursor)
        with file_lock(self.root / ".audit.lock"):
            for path in sorted(self.root.glob("trace.[0-9]*.jsonl")) + [self.root / "trace.jsonl"]:
                if not path.exists():
                    continue
                size = path.stat().st_size
                if remaining >= size:
                    remaining -= size
                    if size:
                        previous_location = path, size
                    continue
                with path.open("rb") as stream:
                    if remaining:
                        previous_location = path, remaining
                    stream.seek(remaining)
                    remaining = 0
                    for line in stream:
                        if not line.endswith(b"\n"):
                            gaps.append({"file": path.name, "offset": stream.tell() - len(line), "reason": "partial_line"})
                            break
                        position += len(line)
                        try:
                            event = json.loads(line)
                            if not isinstance(event, dict) or type(event.get('source_seq')) is not int:
                                raise ValueError('invalid sequence')
                            if previous_location:
                                previous = self._previous_sequence(*previous_location)
                                previous_location = None
                            if event['source_seq'] != previous + 1:
                                gaps.append({'file': path.name, 'reason': 'sequence_gap',
                                             'expected': previous + 1, 'actual': event['source_seq']})
                            previous = event['source_seq']
                            events.append(event)
                        except (ValueError, UnicodeDecodeError):
                            gaps.append({"file": path.name, "reason": "invalid_event"})
                        if len(events) >= limit:
                            return {"events": events, "next_cursor": position, "gaps": gaps, "complete": False}
        return {"events": events, "next_cursor": position, "gaps": gaps, "complete": not gaps}
