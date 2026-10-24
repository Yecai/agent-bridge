import base64
import hashlib
import json
import os
import signal
import subprocess
import threading
import time
from uuid import uuid4

from .errors import BridgeError
from .procutil import group_alive, start_marker, stop_group
from .storage import private_dir, write_json


class ProcessManager:
    def __init__(self, runtime):
        self.runtime = runtime
        self.root = private_dir(runtime.spec.log_dir / "processes")
        self.rows = {}
        self.condition = threading.Condition(threading.RLock())

    def start(self, command, cwd, op_id, timeout=1800):
        if self.runtime.spec.access != "full":
            raise BridgeError("tool_not_allowed", "只读执行不能运行命令")
        cwd = self.runtime.workspace.resolve(cwd, write=True)
        if not isinstance(command, (str, list)) or not command:
            raise BridgeError("invalid_command", "命令不能为空")
        argv = ["bash", "-lc", command] if isinstance(command, str) else command
        if not all(isinstance(value, str) and "\x00" not in value for value in argv):
            raise BridgeError("invalid_command", "命令参数必须是字符串")
        fingerprint = hashlib.sha256(json.dumps([argv, str(cwd), timeout]).encode()).hexdigest()
        spec = self.runtime.spec
        with self.condition:
            previous = self.runtime.journal.begin(spec.execution_id, op_id, fingerprint)
            if not previous["created"]:
                if previous["result"]:
                    return previous["result"]["data"]
                if previous["process_id"]:
                    return {"process_id": previous["process_id"], "state": previous["state"]}
                raise BridgeError("outcome_unknown", "原命令结果尚未确认")
            process_id = str(uuid4())
            self.runtime.audit.append("process.begin", "running", {"command": command, "cwd": str(cwd)}, op_id=op_id, process_id=process_id)
            env = os.environ.copy()
            for key in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "CLAUDECODE"):
                env.pop(key, None)
            if os.environ.get("VIRTUAL_ENV"):
                own_bin = os.path.join(os.environ["VIRTUAL_ENV"], "bin")
                env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep) if p != own_bin)
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            row = {"process_id": process_id, "execution_id": spec.execution_id, "pid": process.pid,
                   "state": "running", "exit_code": None, "op_id": op_id, "start_marker": start_marker(process.pid)}
            self.rows[process_id] = {"row": row, "process": process, "done": False}
            write_json(self.root / f"{process_id}.json", row)
            self.runtime.journal.bind_process(spec.execution_id, op_id, process_id)
            self.runtime.journal.finish(spec.execution_id, op_id, {"status": "running", "data": dict(row)})
            threading.Thread(target=self._monitor, args=(process_id, max(1, min(timeout, 86400))), daemon=True).start()
            return dict(row)

    def _reader(self, process_id, channel, stream):
        stream_id = f"{process_id}:{channel}"
        try:
            while data := stream.read1(8192):
                refs = self.runtime.artifacts.append(stream_id, data)
                self.runtime.audit.append("process.output", "running", {"channel": channel, "bytes": len(data)},
                                          process_id=process_id, artifact_refs=refs)
                with self.condition:
                    self.condition.notify_all()
            self.runtime.artifacts.finish(stream_id)
        finally:
            stream.close()
            with self.condition:
                self.condition.notify_all()

    def _monitor(self, process_id, timeout):
        item = self.rows[process_id]
        proc = item["process"]
        readers = [threading.Thread(target=self._reader, args=(process_id, name, stream), daemon=True)
                   for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr))]
        for reader in readers:
            reader.start()
        timer = threading.Timer(timeout, lambda: self.stop(process_id, reason="timeout"))
        timer.daemon = True
        timer.start()
        try:
            code = proc.wait()
            if group_alive(proc.pid):
                stop_group(proc, item['row']['start_marker'])
            for reader in readers:
                reader.join()
            with self.condition:
                row = item["row"]
                row.update(state="exited", exit_code=code)
                write_json(self.root / f"{process_id}.json", row)
                self.runtime.journal.finish(row["execution_id"], row["op_id"], {"status": "ok" if code == 0 else "error", "data": dict(row)})
                self.runtime.audit.append("process.end", "ok" if code == 0 else "error", {"exit_code": code}, process_id=process_id)
                item["done"] = True
                self.condition.notify_all()
        finally:
            timer.cancel()

    def output(self, process_id, cursor=0, wait_seconds=0):
        with self.condition:
            item = self.rows.get(process_id)
            if item is None:
                raise BridgeError("process_not_found", "进程不属于本次执行")
            offsets = {"stdout": 0, "stderr": 0} if not cursor else json.loads(base64.urlsafe_b64decode(cursor))
            deadline = time.monotonic() + min(30, max(0, wait_seconds))
            while True:
                outputs = {name: self.runtime.artifacts.output(f"{process_id}:{name}", offsets[name]) for name in offsets}
                if item["done"] or any(v["data"] for v in outputs.values()) or time.monotonic() >= deadline:
                    break
                self.condition.wait(timeout=min(0.1, max(0, deadline - time.monotonic())))
            next_cursor = base64.urlsafe_b64encode(json.dumps({name: value["next_cursor"] for name, value in outputs.items()}).encode()).decode()
            return {**item["row"], "stdout": outputs["stdout"]["data"].decode("utf-8", "replace"),
                    "stderr": outputs["stderr"]["data"].decode("utf-8", "replace"), "next_cursor": next_cursor,
                    "artifact_refs": outputs["stdout"]["artifact_refs"] + outputs["stderr"]["artifact_refs"]}

    def stop(self, process_id, reason="cancelled"):
        with self.condition:
            item = self.rows.get(process_id)
            if item is None:
                raise BridgeError("process_not_found", "进程不属于本次执行")
            proc = item["process"]
            self.runtime.audit.append("process.stop", "running", {"reason": reason}, process_id=process_id)
        if not item["done"]:
            stop_group(proc, item['row']['start_marker'])
            deadline = time.monotonic() + 10
            with self.condition:
                while not item['done'] and time.monotonic() < deadline:
                    self.condition.wait(timeout=.05)
        if not item["done"]:
            raise BridgeError("stop_unconfirmed", "无法确认命令进程组已退出")
        return dict(item["row"])

    def stop_all(self):
        for process_id in list(self.rows):
            self.stop(process_id)
