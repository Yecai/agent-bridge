import json
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.mark.parametrize("cut", range(1, 17))
def test_stream_secret_split(cut):
    from agentbridge.redact import StreamRedactor
    value = b"before fixture-password after"
    stream = StreamRedactor([b"fixture-password"])
    output = stream.feed(value[:cut]) + stream.feed(value[cut:]) + stream.finish()
    assert output == b"before [REDACTED] after"


def test_audit_concurrent_and_private(tmp_path):
    from agentbridge.audit import AuditLog
    audit = AuditLog(tmp_path / "logs", {"execution_id": "e1"}, [b"fixture-secret"])
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda n: audit.append("tool.begin", "ok", {"value": n, "password": "fixture-secret"}), range(50)))
    rows = audit.read()["events"]
    assert len(rows) == 50
    assert len({r["event_id"] for r in rows}) == 50
    assert [r["source_seq"] for r in rows] == list(range(1, 51))
    assert "fixture-secret" not in json.dumps(rows)
    assert (tmp_path / "logs").stat().st_mode & 0o777 == 0o700


def test_artifacts_preserve_all_output_and_export(tmp_path):
    from agentbridge.artifacts import Artifacts
    store = Artifacts(tmp_path / "artifacts", secrets=[b"fixture-password"], shard_size=16)
    store.append("out", b"before fixture-pass")
    store.append("out", b"word after" * 20)
    store.finish("out")
    output = store.output("out", 0, 10000)
    assert output["data"] == b"before [REDACTED] after" + b"word after" * 19
    assert len(store.manifest()["streams"]["out"]) > 1
    assert b"fixture-password" not in b"".join(p.read_bytes() for p in (tmp_path / "artifacts").glob("*.bin"))
    assert output["next_cursor"] == len(output["data"])
