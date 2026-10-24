import sys
import time
from types import SimpleNamespace

import pytest


@pytest.fixture
def manager(spec):
    from agentbridge.processes import ProcessManager
    from agentbridge.workspace import Workspace
    from agentbridge.audit import AuditLog
    from agentbridge.artifacts import Artifacts
    from agentbridge.journal import Journal
    context = SimpleNamespace(spec=spec, workspace=Workspace(spec.cwd, spec.read_roots, spec.write_roots),
        audit=AuditLog(spec.log_dir, spec.public_context(), spec.secrets()),
        artifacts=Artifacts(spec.log_dir / "artifacts", spec.secrets()),
        journal=Journal(spec.log_dir / "ops.sqlite3"))
    result = ProcessManager(context)
    yield result
    result.stop_all()


def test_real_command_complete_output_and_duplicate(manager, spec):
    command = [sys.executable, "-c", "import sys; print('local-proof'); print('fixture-secret', file=sys.stderr)"]
    started = manager.start(command, spec.cwd, "op1", 10)
    duplicate = manager.start(command, spec.cwd, "op1", 10)
    assert duplicate["process_id"] == started["process_id"]
    cursor, text = 0, ""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = manager.output(started["process_id"], cursor, 1)
        cursor = result["next_cursor"]
        text += result["stdout"] + result["stderr"]
        if result["state"] == "exited":
            break
    assert result["exit_code"] == 0
    assert "local-proof" in text
    assert "fixture-secret" not in text
    assert "[REDACTED]" in text


def test_stop_and_foreign_process(manager, spec):
    from agentbridge.errors import BridgeError
    started = manager.start([sys.executable, "-c", "import time; time.sleep(60)"], spec.cwd, "op2", 120)
    stopped = manager.stop(started["process_id"])
    assert stopped["state"] == "exited"
    with pytest.raises(BridgeError, match="process_not_found"):
        manager.output("foreign", 0, 0)
