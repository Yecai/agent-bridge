import json


class JsonLines:
    """Incremental transcript parser retaining incomplete UTF-8 bytes and tail."""
    def __init__(self):
        self.pending = bytearray()

    def feed(self, data):
        self.pending.extend(data)
        result = []
        while b'\n' in self.pending:
            line, _, rest = self.pending.partition(b'\n')
            self.pending = bytearray(rest)
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                result.append(event if isinstance(event, dict) else {'type': 'bridge.invalid_event', 'raw': line.decode('utf-8', 'replace')})
            except (ValueError, UnicodeDecodeError):
                result.append({'type': 'bridge.invalid_event', 'raw': line.decode('utf-8', 'replace')})
        return result

    def finish(self):
        result = [{'type': 'bridge.partial_event', 'raw': self.pending.decode('utf-8', 'replace')}] if self.pending else []
        self.pending.clear()
        return result
