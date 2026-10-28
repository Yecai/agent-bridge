"""Private, exact tool replies; these files are never part of public diagnostics."""
import hashlib
import json

from .errors import BridgeError
from .storage import private_dir, write_json


class Results:
    def __init__(self, root):
        self.root = private_dir(root)

    def path(self, op_id):
        return self.root / (hashlib.sha256(op_id.encode()).hexdigest() + '.json')

    def lookup(self, op_id):
        path = self.path(op_id)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def issue(self, op_id, name, arguments):
        fingerprint = hashlib.sha256(json.dumps([name, arguments], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        old = self.lookup(op_id)
        if old:
            if old['fingerprint'] != fingerprint:
                raise BridgeError('operation_conflict', '同一操作的参数发生变化')
            if old.get('result') is None:
                raise BridgeError('outcome_unknown', '原操作结果未知，禁止重放')
            return old['result']
        write_json(self.path(op_id), {'fingerprint': fingerprint, 'state': 'issued'})
        return None

    def save(self, op_id, result):
        row = self.lookup(op_id)
        row.update(state='result_saved', result=result)
        write_json(self.path(op_id), row)
