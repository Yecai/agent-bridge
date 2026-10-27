import asyncio
import base64
import json

import pytest
import asyncssh

from agentbridge.errors import BridgeError
from agentbridge.runner import Runner


async def test_control_uses_existing_remote_socket_without_spawning_python(spec):
    class Writer:
        frame = None
        closed = False

        def write(self, value):
            self.frame = json.loads(value)

        async def drain(self):
            pass

        def close(self):
            self.closed = True

        async def wait_closed(self):
            pass

    writer = Writer()

    class Reader:
        async def readline(self):
            return json.dumps({'result': {'accepted': writer.frame['action']}}) + '\n'

    class Connection:
        opened = 0

        async def open_unix_connection(self, path, **kwargs):
            self.opened += 1
            assert path == '/private/control.sock'
            assert kwargs == {'encoding': 'utf-8'}
            return Reader(), writer

        async def create_process(self, command):
            pytest.fail('A running supervisor must not spawn a Python process for every key')

    runner = Runner(spec)
    runner.connection = Connection()
    runner.remote_control_path = '/private/control.sock'
    assert await runner.remote('input', data=base64.b64encode(b'key').decode()) == {'accepted': 'input'}
    assert writer.frame['execution_id'] == spec.execution_id
    assert writer.frame['revision'] == spec.control_revision
    assert writer.frame['request_id']
    assert not writer.closed
    assert await runner.remote('status') == {'accepted': 'status'}
    assert runner.connection.opened == 1


async def test_socket_input_lost_receipt_never_falls_back_or_replays(spec):
    class Writer:
        def write(self, value):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    class Reader:
        async def readline(self):
            return ''

    class Connection:
        attempts = 0

        async def open_unix_connection(self, *args, **kwargs):
            self.attempts += 1
            return Reader(), Writer()

        async def create_process(self, command):
            pytest.fail('Input with an unknown outcome cannot use a fallback')

    runner = Runner(spec)
    runner.connection = connection = Connection()
    runner.remote_control_path = '/private/control.sock'
    with pytest.raises(BridgeError) as failed:
        await runner.remote('input', data=base64.b64encode(b'once').decode())
    assert failed.value.code == 'input_outcome_unknown'
    assert connection.attempts == 1
    assert not runner.runtime.enabled


async def test_disabled_socket_forwarding_falls_back_before_sending_input(spec):
    class Process:
        exit_status = 0

        async def communicate(self, payload):
            assert json.loads(payload)['action'] == 'input'
            return '{"accepted": true}', ''

    class Connection:
        opened = 0
        spawned = 0

        async def open_unix_connection(self, *args, **kwargs):
            self.opened += 1
            raise asyncssh.ChannelOpenError(asyncssh.OPEN_ADMINISTRATIVELY_PROHIBITED, 'disabled')

        async def create_process(self, command):
            self.spawned += 1
            return Process()

        def is_closed(self):
            return False

    runner = Runner(spec)
    runner.connection = connection = Connection()
    runner.remote_python = '/python'
    runner.remote_control_path = '/private/control.sock'
    for _ in range(2):
        assert await runner.remote('input', data=base64.b64encode(b'key').decode()) == {'accepted': True}
    assert connection.opened == 1
    assert connection.spawned == 2
    assert runner.runtime.enabled


async def test_mouse_burst_preserves_input_and_control_with_ssh_channel_limit(spec):
    class LimitedConnection:
        active = 0
        maximum = 0
        received = []

        async def create_process(self, command):
            if self.active >= 2:
                raise OSError('SSH session channel limit reached')
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            connection = self

            class Process:
                exit_status = 0

                async def communicate(self, payload):
                    try:
                        frame = json.loads(payload)
                        connection.received.append(frame)
                        await asyncio.sleep(.01)
                        return json.dumps({'accepted': frame['action']}), ''
                    finally:
                        connection.active -= 1

            return Process()

        def is_closed(self):
            return False

    runner = Runner(spec)
    runner.connection = connection = LimitedConnection()
    runner.remote_python = '/python'
    mouse = base64.b64encode(b'\x1b[<35;33;21M').decode()
    actions = ['input'] * 16 + ['renew', 'status'] + ['input'] * 16
    replies = await asyncio.gather(*(runner.remote(action, **({'data': mouse} if action == 'input' else {}))
                                     for action in actions), return_exceptions=True)
    assert not [reply for reply in replies if isinstance(reply, Exception)]
    assert [frame['action'] for frame in connection.received] == actions
    assert all(reply == {'accepted': action} for reply, action in zip(replies, actions))
    assert connection.maximum <= 2
    assert runner.runtime.enabled


async def test_failed_input_logs_cause_and_is_never_replayed(spec):
    class Connection:
        attempts = 0

        async def create_process(self, command):
            self.attempts += 1
            raise OSError('private transport details must not enter the audit log')

    runner = Runner(spec)
    runner.connection = connection = Connection()
    runner.remote_python = '/python'
    with pytest.raises(BridgeError) as failed:
        await runner.remote('input', data=base64.b64encode(b'key').decode())
    assert failed.value.code == 'input_outcome_unknown'
    assert not runner.runtime.enabled
    assert connection.attempts == 1
    events = runner.runtime.audit.read()['events']
    event = next((row for row in events if row['event'] == 'ssh.control_error'), None)
    assert event is not None
    assert event['details'] == {'action': 'input', 'error_type': 'OSError'}
    assert 'private transport details' not in json.dumps(events)


async def test_input_failure_revokes_authority_even_when_error_logging_fails(spec, monkeypatch):
    class Connection:
        async def create_process(self, command):
            raise OSError('channel unavailable')

    runner = Runner(spec)
    runner.connection = Connection()
    runner.remote_python = '/python'
    append = runner.runtime.audit.append

    def unavailable(event, *args, **kwargs):
        if event == 'ssh.control_error':
            raise OSError('audit unavailable')
        return append(event, *args, **kwargs)

    monkeypatch.setattr(runner.runtime.audit, 'append', unavailable)
    with pytest.raises(OSError):
        await runner.remote('input', data=base64.b64encode(b'key').decode())
    assert not runner.runtime.enabled


async def test_terminal_burst_is_batched_without_parallel_requests(monkeypatch):
    from agentbridge import cli
    received = []
    entered, release = asyncio.Event(), asyncio.Event()
    async def command(execution_id, action, **fields):
        received.append((action, fields))
        if len(received) == 1:
            entered.set()
            await release.wait()
        return {'accepted': True}
    monkeypatch.setattr(cli, 'command', command)
    terminal = cli.TerminalInput('execution')
    worker = asyncio.create_task(terminal.run())
    try:
        terminal.feed(b'first')
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(500):
            terminal.feed(b'\x1b[<35;33;21M')
        terminal.resize(24, 80)
        terminal.resize(40, 120)
        await asyncio.sleep(.01)
        assert len(received) == 1
        release.set()
        for _ in range(100):
            if len(received) >= 3:
                break
            await asyncio.sleep(.001)
        assert [action for action, _ in received] == ['input', 'resize', 'input']
        assert b''.join(base64.b64decode(fields['data']) for action, fields in received if action == 'input') == b'first' + b'\x1b[<35;33;21M' * 500
        assert received[1][1] == {'rows': 40, 'columns': 120}
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


async def test_terminal_input_failure_is_observable_and_not_replayed(monkeypatch):
    from agentbridge import cli
    calls = []
    async def command(*args, **fields):
        calls.append(fields)
        raise BridgeError('input_outcome_unknown', 'unknown')
    monkeypatch.setattr(cli, 'command', command)
    terminal = cli.TerminalInput('execution')
    terminal.feed(b'once')
    with pytest.raises(BridgeError, match='input_outcome_unknown'):
        await terminal.run()
    assert len(calls) == 1


async def test_terminal_input_buffer_overflow_fails_without_partial_send(monkeypatch):
    from agentbridge import cli
    terminal = cli.TerminalInput('execution')
    terminal.feed(b'x' * 65536)
    terminal.feed(b'y')
    with pytest.raises(BridgeError, match='input_queue_full'):
        await terminal.run()
