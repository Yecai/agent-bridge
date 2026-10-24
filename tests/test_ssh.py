from types import SimpleNamespace

import asyncssh
import pytest

from agentbridge.ssh import connect_target, reverse_mcp
from agentbridge.protocol import SshTarget
from agentbridge.errors import BridgeError


class Server(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == 'fixture' and password == 'fixture-password'

    def server_requested(self, listen_host, listen_port):
        return listen_host == '127.0.0.1'


@pytest.fixture
async def ssh_fixture(tmp_path):
    key = asyncssh.generate_private_key('ssh-ed25519')
    server = await asyncssh.create_server(Server, '127.0.0.1', 0, server_host_keys=[key])
    port = server.get_port()
    trusted = tmp_path / 'known_hosts'
    trusted.write_text(f'[127.0.0.1]:{port} ' + key.export_public_key().decode())
    trusted.chmod(0o600)
    target = SshTarget(host='127.0.0.1', port=port, user='fixture', password='fixture-password', known_hosts=trusted)
    try:
        yield SimpleNamespace(target=target, key=key)
    finally:
        server.close()
        await server.wait_closed()


async def test_ssh_identity_and_loopback_reverse(ssh_fixture, tmp_path):
    async with connect_target(ssh_fixture.target) as connection:
        listener = await reverse_mcp(connection, 12345)
        assert listener.get_port() > 0
        listener.close()
        await listener.wait_closed()
    unknown = ssh_fixture.target.model_copy(update={'known_hosts': tmp_path / 'missing'})
    with pytest.raises(BridgeError, match='host_untrusted'):
        async with connect_target(unknown):
            pytest.fail('untrusted')
