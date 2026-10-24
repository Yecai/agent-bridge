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


def test_exited_leader_does_not_leave_child(manager, spec):
    import os
    import sys
    from agentbridge.processes import ProcessManager
    from agentbridge.procutil import group_alive
    row = manager.start([sys.executable, '-c', 'import subprocess,sys;subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"])'],
                        cwd=str(spec.cwd), op_id='child-tree', timeout=10)
    import time
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        output = manager.output(row['process_id'], wait_seconds=1)
        if output['state'] == 'exited':
            break
    try:
        assert output['state'] == 'exited'
        assert not group_alive(row['pid'])
    finally:
        manager.stop_all()


def test_registration_failure_never_releases_command_gate(manager,spec,monkeypatch):
    import agentbridge.processes as processes
    from agentbridge.procutil import group_alive
    pid=[]
    original=processes.subprocess.Popen
    def launch(*args,**kwargs):
        proc=original(*args,**kwargs);pid.append(proc.pid);return proc
    monkeypatch.setattr(processes.subprocess,'Popen',launch)
    def unavailable(*args,**kwargs):
        raise OSError('fixture journal full')
    monkeypatch.setattr(manager.runtime.journal,'bind_process',unavailable)
    try:
        with pytest.raises(Exception):
            manager.start([sys.executable,'-c','from pathlib import Path;Path("must-not-exist").write_text("bad")'],spec.cwd,'registration-failure')
        assert not (spec.cwd/'must-not-exist').exists()
        assert pid and not group_alive(pid[0])
    finally:
        for item in manager.rows.values():
            from agentbridge.procutil import stop_group
            stop_group(item['process'],item['row']['start_marker'])
            item['done']=True


def test_owner_kill_stops_local_command_group(spec,tmp_path):
    import json,os,signal,subprocess
    from agentbridge.cli import private_request
    from agentbridge.procutil import group_alive,start_marker
    request=tmp_path/'request.json';request.write_text(private_request(spec));request.chmod(0o600)
    script=tmp_path/'owner.py'
    script.write_text('from pathlib import Path\nfrom agentbridge.protocol import RunSpec\nfrom agentbridge.runtime import Runtime\nimport json,sys,time\nspec=RunSpec.model_validate_json(Path(sys.argv[1]).read_text())\nruntime=Runtime(spec)\nrow=runtime.processes.start([sys.executable,"-c","import time;time.sleep(60)"],spec.cwd,"owner-kill")\nprint(json.dumps(row),flush=True)\ntime.sleep(60)\n')
    owner=subprocess.Popen([sys.executable,str(script),str(request)],stdout=subprocess.PIPE,text=True)
    row=None
    try:
        row=json.loads(owner.stdout.readline())
        os.kill(owner.pid,signal.SIGKILL);owner.wait(timeout=5)
        deadline=time.monotonic()+5
        while group_alive(row['pid']) and time.monotonic()<deadline:
            time.sleep(.05)
        assert not group_alive(row['pid'])
    finally:
        if owner.poll() is None:
            owner.kill();owner.wait()
        owner.stdout.close()
        if row and group_alive(row['pid']) and start_marker(row['pid'])==row['start_marker']:
            os.killpg(row['pid'],signal.SIGKILL)
