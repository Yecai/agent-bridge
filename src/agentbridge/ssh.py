import asyncio
import ipaddress
import json
import shlex
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


async def reverse_mcp(connection, local_port, remote_port=0):
    try:
        return await connection.forward_remote_port('127.0.0.1', remote_port, '127.0.0.1', local_port)
    except (OSError, asyncssh.Error) as exc:
        raise BridgeError('forwarding_denied', '无法建立远端回环反向隧道') from exc


LISTENER_PROBE = r'''
import ipaddress,json,subprocess,sys
port=int(json.load(sys.stdin)['port'])
addresses=[]
if sys.platform=='linux':
    for table in ['/proc/net/tcp','/proc/net/tcp6']:
        try:
            with open(table) as source:
                for line in list(source)[1:]:
                    row=line.split()
                    address,number=row[1].split(':')
                    if int(number,16)!=port or row[3]!='0A':continue
                    raw=bytes.fromhex(address)
                    raw=b''.join(raw[i:i+4][::-1] for i in range(0,len(raw),4))
                    addresses.append(str(ipaddress.ip_address(raw)))
        except FileNotFoundError:
            pass
else:
    result=subprocess.run(['lsof','-nP','-a','-iTCP:'+str(port),'-sTCP:LISTEN','-Fn'],capture_output=True,text=True,timeout=10)
    addresses=[line[1:].rsplit(':',1)[0].strip('[]') for line in result.stdout.splitlines() if line.startswith('n')]
print(json.dumps({'addresses':addresses}))
'''


async def verify_reverse_listener(connection, port):
    """检查实际监听地址，拒绝 sshd GatewayPorts 把请求改为公网监听。"""
    if type(port) is not int or not 1 <= port <= 65535:
        raise BridgeError('forwarding_denied', '反向端口无效')
    try:
        process = await connection.create_process('python3 -c ' + shlex.quote(LISTENER_PROBE))
        stdout, _ = await asyncio.wait_for(process.communicate(json.dumps({'port': port})), 15)
        addresses = json.loads(stdout)['addresses']
        if process.exit_status != 0 or not addresses or not all(ipaddress.ip_address(address).is_loopback for address in addresses):
            raise ValueError('listener')
    except (ValueError, KeyError, OSError, TimeoutError, asyncssh.Error) as error:
        raise BridgeError('forwarding_exposed', '无法确认反向端口仅监听回环地址，已拒绝授予工具权限') from error
