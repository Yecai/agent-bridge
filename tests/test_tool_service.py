import json
import os
import subprocess
import sys
import time
from uuid import uuid4

import pytest


@pytest.fixture
def tool(spec, tmp_path):
    values = spec.model_dump(mode='json')
    for key in ('target', 'claude_exe', 'model', 'effort', 'prompt', 'session_id', 'resume', 'result_schema', 'callback_token'):
        values.pop(key, None)
    values.update(mode='tools', callbacks={})
    env = {**os.environ, 'AGENTBRIDGE_STATE': str(tmp_path / 'private')}
    def invoke(action, body=None, *, check=True):
        argv = [sys.executable, '-m', 'agentbridge', 'tools', action]
        if action != 'start':
            argv += ['--execution', spec.execution_id]
        if body is not None:
            path = tmp_path / f'{uuid4()}.json'
            path.write_text(json.dumps(body)); path.chmod(0o600)
            argv += ['--request-file', str(path)]
        proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
        if check:
            assert proc.returncode == 0, proc.stderr
            return json.loads(proc.stdout)
        return proc
    invoke('start', values)
    try:
        yield invoke
    finally:
        assert invoke('cancel')['stop_confirmed'] is True


def call(tool, name, arguments, op='one', check=True):
    return tool('call', {'name': name, 'arguments': arguments, 'event_id': op,
                        'op_id': op, 'control_revision': 0}, check=check)


def test_discovery_has_no_execution_state(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENTBRIDGE_STATE', str(tmp_path / 'absent'))
    reply = subprocess.run([sys.executable, '-m', 'agentbridge', 'capabilities'], capture_output=True, text=True)
    assert reply.returncode == 0, reply.stderr
    assert json.loads(reply.stdout)['tool_service']['version'] == 1
    assert not (tmp_path / 'absent').exists()


def test_file_call_is_durable_and_conflicts_are_rejected(tool, spec):
    args = {'path': 'file.txt', 'content': 'hello', 'create': True}
    first = call(tool, 'write_file', args)
    assert call(tool, 'write_file', args) == first
    assert (spec.cwd / 'file.txt').read_text() == 'hello'
    proc = call(tool, 'write_file', {**args, 'content': 'changed'}, check=False)
    assert proc.returncode != 0 and 'operation_conflict' in proc.stderr
    tool('freeze')
    assert call(tool, 'write_file', args) == first
    denied = call(tool, 'write_file', {**args, 'path': 'blocked'}, op='two', check=False)
    assert denied.returncode != 0 and 'action_pending' in denied.stderr
    assert call(tool, 'read_file', {'path': 'file.txt'}, op='read')['data']['text'] == 'hello'


def test_exec_replay_is_same_receipt_after_process_exits(tool, spec):
    args = {'command': [sys.executable, '-c', 'from pathlib import Path; p=Path("counter"); p.write_text(p.read_text()+"x" if p.exists() else "x")']}
    first = call(tool, 'exec', args)
    pid = first['data']['process_id']
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = call(tool, 'process_output', {'process_id': pid, 'wait_seconds': 1}, op=str(uuid4()))
        if result['data']['state'] == 'exited':
            break
    assert result['data']['exit_code'] == 0
    assert call(tool, 'exec', args) == first
    assert (spec.cwd / 'counter').read_text() == 'x'


def test_cancel_stops_process_groups_and_revokes_future_calls(tool):
    receipt = call(tool, 'exec', {'command': [sys.executable, '-c', 'import time; time.sleep(60)']})
    assert receipt['data']['state'] == 'running'
    proof = tool('cancel')
    assert proof['stop_confirmed'] is True
    denied = call(tool, 'exec', {'command': ['true']}, op='again', check=False)
    assert denied.returncode != 0


def test_dead_supervisor_can_reconcile_owned_process_stop(tool):
    import signal
    from agentbridge.procutil import group_alive
    receipt = call(tool, 'exec', {'command': [sys.executable, '-c', 'import time; time.sleep(60)']})
    supervisor = tool('status')
    os.kill(supervisor['pid'], signal.SIGKILL)
    proof = tool('cancel')
    assert proof['stop_confirmed'] is True
    assert proof['stop_evidence'] == 'recovered_process_groups'
    assert not group_alive(receipt['data']['pid'])


def test_escaped_process_output_can_be_paged_without_freezing(tool):
    first = call(tool, 'exec', {'command': [sys.executable, '-c', 'import sys;sys.stdout.write("\\n"*65536);sys.stderr.write("\\n"*65536)']})
    pid = first['data']['process_id']
    cursor, output = 0, {'stdout': '', 'stderr': ''}
    for _ in range(30):
        result = call(tool, 'process_output', {'process_id': pid, 'cursor': cursor, 'wait_seconds': 1}, op=str(uuid4()))
        assert result['status'] != 'error'
        body = result['data']
        for channel in output:
            output[channel] += body[channel]
        next_cursor = body['next_cursor']
        if body['state'] == 'exited' and next_cursor == cursor:
            break
        cursor = next_cursor
    assert {name: len(text) for name, text in output.items()} == {'stdout': 65536, 'stderr': 65536}
    assert not tool('status')['frozen']


def test_large_search_result_is_recoverable_and_does_not_freeze(tool):
    call(tool, 'write_file', {'path': 'long.txt', 'content': 'MATCH' + 'x' * 300000, 'create': True}, op='large-file')
    result = call(tool, 'grep', {'pattern': 'MATCH', 'limit': 1}, op='search')
    assert result['error_code'] == 'result_too_large'
    assert not tool('status')['frozen']
    page = call(tool, 'read_file', {'path': 'long.txt', 'offset': 0, 'limit': 1024}, op='page')
    assert page['data']['truncated'] and page['data']['next_offset'] == 1024
