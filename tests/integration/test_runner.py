import asyncio
import os
import shlex
import sys
from pathlib import Path

import asyncssh
import pytest

from conftest import make_spec
from agentbridge.runner import Runner
from agentbridge.protocol import SshTarget


class Server(asyncssh.SSHServer):
    def begin_auth(self, username):
        return False

    def server_requested(self, host, port):
        return host == '127.0.0.1'


async def test_real_ssh_runner_and_local_tools(tmp_path, monkeypatch):
    remote_home = tmp_path / 'remote-home'
    remote_home.mkdir()
    class SFTP(asyncssh.SFTPServer):
        def map_path(self, path):
            return path if path.startswith(b'/') else os.fsencode(remote_home) + b'/' + path
    command_errors = []
    async def process_factory(channel):
        argv = shlex.split(channel.command)
        if argv[0] == 'python3':
            argv[0] = sys.executable
        env = {**os.environ, 'AGENTBRIDGE_REMOTE_ROOT': str(remote_home / '.local/share/agentbridge/executions')}
        proc = await asyncio.create_subprocess_exec(*argv, cwd=remote_home, env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        data = (await channel.stdin.read()).encode()
        stdout, stderr = await proc.communicate(data)
        if proc.returncode:
            command_errors.append(stderr.decode())
        channel.stdout.write(stdout.decode())
        channel.stderr.write(stderr.decode())
        channel.exit(proc.returncode)
    key = asyncssh.generate_private_key('ssh-ed25519')
    server = await asyncssh.create_server(Server, '127.0.0.1', 0, server_host_keys=[key], process_factory=process_factory,
                                         sftp_factory=SFTP)
    known_hosts = tmp_path / 'known_hosts'
    known_hosts.write_text(f'[127.0.0.1]:{server.get_port()} ' + key.export_public_key().decode())
    spec = make_spec(tmp_path)
    spec.target = SshTarget(host='127.0.0.1', port=server.get_port(), user='fixture', known_hosts=known_hosts)
    spec.claude_exe = [sys.executable, str(Path(__file__).parents[1] / 'fixtures/fake_claude.py')]
    spec.prompt = 'use local tools'
    runner = Runner(spec)
    try:
        result = await asyncio.wait_for(runner.run(), 60)
        if command_errors:
            print(command_errors[0])
        assert not command_errors, command_errors
        assert result['exit_code'] == 0, (result, runner.runtime.artifacts.output('remote:stderr')['data'].decode())
        assert result['stop_confirmed']
        assert (spec.cwd / 'proof.txt').read_text() == 'remote-claude-local-file'
        assert runner.result['structured_output']['cwd'] == str(spec.cwd.resolve())
        assert runner.remote_status['stop_confirmed']
    finally:
        server.close()
        await server.wait_closed()
