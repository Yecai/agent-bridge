import asyncio
from contextlib import asynccontextmanager

import asyncssh

from .errors import BridgeError


@asynccontextmanager
async def connect_target(target):
    if not target.known_hosts.is_file():
        raise BridgeError('host_untrusted', '缺少已确认的 SSH 主机密钥文件')
    try:
        connection = await asyncio.wait_for(asyncssh.connect(
            target.host, port=target.port, username=target.user,
            password=target.password.get_secret_value() if target.password else None,
            known_hosts=str(target.known_hosts), connect_timeout=10, login_timeout=15,
            keepalive_interval=10, keepalive_count_max=3), 25)
    except asyncssh.HostKeyNotVerifiable as exc:
        raise BridgeError('host_untrusted', 'SSH 主机密钥未知或发生变化') from exc
    except asyncssh.PermissionDenied as exc:
        raise BridgeError('ssh_auth_failed', 'SSH 认证失败') from exc
    except (OSError, TimeoutError, asyncssh.Error) as exc:
        raise BridgeError('ssh_unavailable', 'SSH 连接失败：' + type(exc).__name__) from exc
    try:
        yield connection
    finally:
        connection.close()
        await connection.wait_closed()


async def reverse_mcp(connection, local_port):
    try:
        return await connection.forward_remote_port('127.0.0.1', 0, '127.0.0.1', local_port)
    except (OSError, asyncssh.Error) as exc:
        raise BridgeError('forwarding_denied', '无法建立远端回环反向隧道') from exc
