import json

SENSITIVE = frozenset({"password", "token", "authorization", "cookie", "secret", "private_key", "callback_token"})


def environment_secrets(env):
    values = []
    for name, value in env.items():
        if name.startswith(('ANTHROPIC_', 'CLAUDE_CODE_')) and name.endswith(('KEY', 'TOKEN')) and value:
            values.append(value.encode())
            if name == 'ANTHROPIC_API_KEY' and len(value) > 20:
                # Claude's first-use confirmation displays the final twenty characters.
                values.append(value[-20:].encode())
    return values


class StreamRedactor:
    def __init__(self, secrets=()):
        self.secrets = sorted({s.encode() if isinstance(s, str) else s for s in secrets if s}, key=len, reverse=True)
        self.keep = max((len(s) for s in self.secrets), default=1) - 1
        self.pending = bytearray()

    def feed(self, value: bytes) -> bytes:
        self.pending.extend(value)
        return self._drain(final=False)

    def finish(self) -> bytes:
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> bytes:
        limit = len(self.pending) if final else max(0, len(self.pending) - self.keep)
        output = bytearray()
        cursor = 0
        while cursor < limit:
            match = next((secret for secret in self.secrets if self.pending.startswith(secret, cursor)), None)
            if match is None:
                output.append(self.pending[cursor])
                cursor += 1
            else:
                output.extend(b"[REDACTED]")
                cursor += len(match)
        del self.pending[:cursor]
        return bytes(output)


def redact_bytes(value: bytes, secrets=()) -> bytes:
    stream = StreamRedactor(secrets)
    return stream.feed(value) + stream.finish()


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if k.lower() in SENSITIVE else redact(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        return redact_bytes(value.encode(), secrets).decode("utf-8", "replace")
    return value
