import hashlib
import json
import secrets
import time
from uuid import uuid4

from . import callbacks
from .artifacts import Artifacts
from .audit import AuditLog
from .errors import BridgeError
from .files import FileTools
from .journal import Journal
from .processes import ProcessManager
from .search import SearchTools
from .workspace import Workspace

READ_TOOLS = frozenset({"workspace_context", "read_file", "list_directory", "glob", "grep"})
WRITE_TOOLS = frozenset({"write_file", "apply_patch", "exec", "process_output", "process_stop"})
HOST_TOOLS = frozenset({"dev_handoff", "dev_finish"})


class Runtime:
    def __init__(self, spec):
        self.spec, self.token, self.enabled = spec, secrets.token_urlsafe(32), True
        self.secrets = spec.secrets() + [self.token.encode()]
        self.audit = AuditLog(spec.log_dir, spec.public_context(), self.secrets)
        self.artifacts = Artifacts(spec.log_dir / "artifacts", self.secrets)
        self.journal = Journal(spec.log_dir / "operations.sqlite3")
        self.workspace = Workspace(spec.cwd, spec.read_roots, spec.write_roots)
        self.files, self.search = FileTools(self.workspace), SearchTools(self.workspace)
        self.processes = ProcessManager(self)
        self.audit.append("execution.accepted", "ok", {"access": spec.access, "mode": spec.mode})

    @property
    def allowed_tools(self):
        return READ_TOOLS if self.spec.access == "read" else READ_TOOLS | WRITE_TOOLS | (HOST_TOOLS & self.spec.callbacks.keys())

    def revoke(self):
        self.enabled = False

    def call(self, name, arguments, meta):
        started = time.monotonic()
        try:
            args_ref = self.artifacts.put(json.dumps(arguments, ensure_ascii=False).encode())
            self.audit.append("tool.begin", "running", {"tool": name}, tool_call_id=meta.tool_call_id, op_id=meta.op_id, artifact_refs=args_ref)
            if not self.enabled or meta.execution_id != self.spec.execution_id or meta.control_revision != self.spec.control_revision:
                raise BridgeError("control_lost", "执行归属已失效")
            if name not in self.allowed_tools:
                raise BridgeError("tool_not_allowed", "工具不在本次授权范围")
            mutation = name in {"write_file", "apply_patch", "process_stop"} | HOST_TOOLS
            if mutation or name == "exec":
                if not meta.op_id:
                    raise BridgeError("operation_id_required", "写操作必须有稳定操作身份")
            if mutation:
                fingerprint = hashlib.sha256(json.dumps([name, arguments], sort_keys=True).encode()).hexdigest()
                old = self.journal.begin(self.spec.execution_id, meta.op_id, fingerprint)
                if not old["created"]:
                    if old["result"] is None:
                        raise BridgeError("outcome_unknown", "原操作尚未确认")
                    self.audit.append("tool.replayed", "ok", {"tool": name}, op_id=meta.op_id)
                    self.audit.append("tool.end", old["result"]["status"], {"tool": name, "replayed": True},
                                      tool_call_id=meta.tool_call_id, op_id=meta.op_id)
                    return old["result"]
            if name in READ_TOOLS | {"write_file", "apply_patch"}:
                owner = self.search if name in {"glob", "grep"} else self.files
                data = getattr(owner, name)(**arguments)
            elif name == "exec":
                data = self.processes.start(op_id=meta.op_id, **arguments)
            elif name == "process_output":
                data = self.processes.output(**arguments)
            elif name == "process_stop":
                data = self.processes.stop(**arguments)
            else:
                data = callbacks.invoke(self.spec, name, arguments, meta)
            refs = self.artifacts.put(json.dumps(data, ensure_ascii=False).encode())
            result = {"status": "running" if data.get("state") == "running" else "ok", "data": data, "artifact_refs": refs}
            if mutation:
                self.journal.finish(self.spec.execution_id, meta.op_id, result)
            self.audit.append("tool.end", result["status"], {"tool": name}, tool_call_id=meta.tool_call_id,
                              op_id=meta.op_id, artifact_refs=refs, duration_ms=int((time.monotonic() - started) * 1000))
            return result
        except Exception as error:
            code = error.code if isinstance(error, BridgeError) else "tool_failed"
            if isinstance(error, OSError) or code == "audit_unavailable":
                self.revoke()
            try:
                self.audit.append("tool.error", "error", {"tool": name, "error": str(error)}, error_code=code,
                                  tool_call_id=meta.tool_call_id, op_id=meta.op_id)
            except Exception:
                self.revoke()
                raise BridgeError("audit_unavailable", "日志存储失败，已撤销工具权限") from error
            if isinstance(error, BridgeError):
                raise
            raise BridgeError(code, "工具执行失败，请查看本机诊断记录") from error

    def close(self):
        self.revoke()
        self.processes.stop_all()
