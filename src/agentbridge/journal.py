import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .errors import BridgeError
from .storage import private_dir


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        private_dir(self.path.parent)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS operations (execution_id TEXT, op_id TEXT, fingerprint TEXT NOT NULL, "
                       "state TEXT NOT NULL, process_id TEXT, result_json TEXT, PRIMARY KEY(execution_id, op_id))")
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, execution_id, op_id, fingerprint):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM operations WHERE execution_id=? AND op_id=?", (execution_id, op_id)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise BridgeError("operation_conflict", "同一操作身份的参数发生变化")
                return self._row(old, False)
            db.execute("INSERT INTO operations VALUES (?, ?, ?, 'running', NULL, NULL)", (execution_id, op_id, fingerprint))
            return {"created": True, "state": "running", "process_id": None, "result": None}

    @staticmethod
    def _row(row, created=False):
        return {"created": created, "state": row["state"], "process_id": row["process_id"],
                "result": json.loads(row["result_json"]) if row["result_json"] else None}

    def finish(self, execution_id, op_id, result):
        with self._connection() as db:
            db.execute("UPDATE operations SET state=?, result_json=? WHERE execution_id=? AND op_id=?",
                       (result["status"], json.dumps(result), execution_id, op_id))

    def bind_process(self, execution_id, op_id, process_id):
        with self._connection() as db:
            db.execute("UPDATE operations SET process_id=? WHERE execution_id=? AND op_id=?", (process_id, execution_id, op_id))

    def lookup(self, execution_id, op_id):
        with self._connection() as db:
            row = db.execute("SELECT * FROM operations WHERE execution_id=? AND op_id=?", (execution_id, op_id)).fetchone()
            return self._row(row) if row else None

    def reconcile(self, execution_id):
        with self._connection() as db:
            db.execute("UPDATE operations SET state='outcome_unknown' WHERE execution_id=? AND state='running' AND process_id IS NULL",
                       (execution_id,))
