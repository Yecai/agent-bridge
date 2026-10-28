import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentbridge import cli, recovery, state
from agentbridge.errors import BridgeError
from agentbridge.storage import write_json


@pytest.fixture
def abandoned(spec, tmp_path, monkeypatch):
    monkeypatch.setenv('AGENTBRIDGE_STATE', str(tmp_path / 'state'))
    cli.ensure_request_identity(spec)
    root = state.execution(spec.execution_id)
    row = {'execution_id': spec.execution_id, 'revision': spec.control_revision,
           'pid': 987654321, 'start_marker': 'old supervisor', 'log_dir': str(spec.log_dir),
           'state': 'stop_unconfirmed', 'stop_confirmed': False, 'exit_code': 1}
    write_json(root / 'status.json', row)
    return spec, root, row


async def test_cancel_recovers_dead_supervisor_and_persists_both_proofs(abandoned, monkeypatch):
    spec, root, row = abandoned
    async def remote(request):
        assert request.execution_id == spec.execution_id
        return {'execution_id': spec.execution_id, 'revision': 0, 'stop_confirmed': True}
    monkeypatch.setattr(recovery, 'remote_proof', remote)
    stopped = await cli.command(spec.execution_id, 'cancel')
    assert stopped['stop_confirmed'] is True
    assert stopped['exit_code'] == 1  # lost/failed model results never become success
    assert stopped['remote_stop_evidence']['execution_id'] == spec.execution_id
    assert json.loads((root / 'status.json').read_text()) == stopped
    assert (await cli.command(spec.execution_id, 'cancel')) == stopped


def test_legacy_execution_uses_original_private_request(abandoned, tmp_path, monkeypatch):
    spec, root, _ = abandoned
    legacy = tmp_path / 'old-request.json'
    Path((root / 'request-path').read_text()).rename(legacy)
    async def remote(request):
        return {'execution_id': request.execution_id, 'revision': 0, 'stop_confirmed': True}
    monkeypatch.setattr(recovery, 'remote_proof', remote)
    assert recovery.recover(spec.execution_id, legacy)['stop_confirmed'] is True


@pytest.mark.parametrize('change', ['revision', 'execution', 'fingerprint', 'missing_fingerprint', 'writer', 'network'])
def test_unproven_recovery_preserves_capacity(abandoned, monkeypatch, change):
    spec, root, original = abandoned
    request = Path((root / 'request-path').read_text())
    data = json.loads(request.read_text())
    if change == 'revision':
        data['control_revision'] += 1
    if change == 'execution':
        from uuid import uuid4
        data['execution_id'] = str(uuid4())
    if change == 'fingerprint':
        data['target']['host'] = 'other.invalid'
    request.write_text(json.dumps(data))
    if change == 'missing_fingerprint':
        (root / 'request-fingerprint').unlink()
    if change == 'writer':
        write_json(spec.log_dir / 'processes/bad.json', {'execution_id': 'other', 'pid': 99})
    async def remote(request):
        if change == 'network':
            raise OSError('unreachable')
        return {'execution_id': spec.execution_id, 'revision': 0, 'stop_confirmed': True}
    monkeypatch.setattr(recovery, 'remote_proof', remote)
    with pytest.raises(BridgeError):
        recovery.recover(spec.execution_id)
    assert json.loads((root / 'status.json').read_text()) == original


def test_live_supervisor_is_never_replaced(abandoned, monkeypatch):
    spec, root, row = abandoned
    monkeypatch.setattr(recovery, 'marker', lambda pid: row['start_marker'])
    assert recovery.recover(spec.execution_id) is None
    assert json.loads((root / 'status.json').read_text()) == row


def test_reused_process_group_is_not_signalled(monkeypatch):
    monkeypatch.setattr(recovery, 'group_alive', lambda pid: True)
    monkeypatch.setattr(recovery, 'marker', lambda pid: 'new process')
    monkeypatch.setattr(recovery.os, 'killpg', lambda *args: pytest.fail('must not kill reused PID'))
    with pytest.raises(ValueError, match='identity_changed'):
        recovery.stop_group({'pid': 99, 'start_marker': 'old process'})


def test_recovery_stops_a_real_owned_process_group():
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
    try:
        proof = recovery.stop_group({'pid': proc.pid, 'start_marker': recovery.marker(proc.pid)})
        assert proof['group_stopped']
        proc.wait(timeout=5)
        assert not recovery.group_alive(proc.pid)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, 9)
        proc.wait()


def test_stale_remote_status_is_verified_without_remote_package(tmp_path):
    from uuid import uuid4
    identifier = str(uuid4())
    root = tmp_path / '.local/share/agentbridge/executions' / identifier
    row = {'execution_id': identifier, 'revision': 3, 'pid': 987654321,
           'start_marker': 'old remote', 'stop_confirmed': False, 'state': 'running'}
    write_json(root / 'status.json', row)
    launcher = 'from pathlib import Path; import runpy,sys; Path.home=lambda:Path(sys.argv[1]); runpy.run_path(sys.argv[2],run_name="__main__")'
    result = subprocess.run([sys.executable, '-c', launcher, str(tmp_path), recovery.__file__],
        input=json.dumps({'execution_id': identifier, 'revision': 3}),
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['stop_confirmed'] is True
    assert json.loads((root / 'status.json').read_text()) == row


def test_remote_refuses_different_control_revision(tmp_path, monkeypatch):
    from uuid import uuid4
    identifier = str(uuid4())
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path / '.local/share/agentbridge/executions' / identifier
    write_json(root / 'status.json', {'execution_id': identifier, 'revision': 4, 'stop_confirmed': True})
    with pytest.raises(ValueError, match='identity_changed'):
        recovery.remote_stop({'execution_id': identifier, 'revision': 3})


def test_remote_uses_configured_execution_root(tmp_path, monkeypatch):
    from uuid import uuid4
    identifier = str(uuid4())
    monkeypatch.setenv('AGENTBRIDGE_REMOTE_ROOT', str(tmp_path / 'custom'))
    row = {'execution_id': identifier, 'revision': 0, 'stop_confirmed': True}
    write_json(tmp_path / 'custom' / identifier / 'status.json', row)
    assert recovery.remote_stop({'execution_id': identifier, 'revision': 0}) == row


async def test_remote_live_supervisor_receives_cancel_without_signalling(tmp_path, monkeypatch):
    import asyncio
    from uuid import uuid4
    from agentbridge.supervisor import ControlServer
    identifier = str(uuid4())
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path / '.local/share/agentbridge/executions' / identifier
    row = {'execution_id': identifier, 'revision': 0, 'stop_confirmed': False}
    write_json(root / 'status.json', row)
    actions = []
    async def handle(frame):
        actions.append(frame['action'])
        return {**row, 'stop_confirmed': True}
    monkeypatch.setattr(recovery, 'stop_group', lambda row: pytest.fail('live owner must handle cancellation'))
    async with ControlServer(root / 'control.sock', identifier, 0, handle):
        result = await asyncio.to_thread(recovery.remote_stop, {'execution_id': identifier, 'revision': 0})
    assert result['stop_confirmed']
    assert actions == ['cancel']


def test_remote_stale_socket_recovers_by_original_group(tmp_path, monkeypatch):
    import socket
    from uuid import uuid4
    from agentbridge.supervisor import socket_path
    identifier = str(uuid4())
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path / '.local/share/agentbridge/executions' / identifier
    write_json(root / 'status.json', {'execution_id': identifier, 'revision': 0,
        'pid': 987654321, 'start_marker': 'old', 'stop_confirmed': False})
    path = socket_path(root / 'control.sock')
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(path))
    try:
        assert recovery.remote_stop({'execution_id': identifier, 'revision': 0})['stop_confirmed']
    finally:
        path.unlink(missing_ok=True)
