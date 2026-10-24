import asyncio
import hashlib
import json
import shlex
import time
from pathlib import Path
from uuid import uuid4

import asyncssh

from . import state
from .errors import BridgeError
from .ssh import connect_target
from .storage import atomic_write, file_lock, write_json

CHECKS = ('ssh', 'host_identity', 'python', 'claude_auth', 'mcp_roundtrip', 'print_schema', 'interactive_audit', 'stop_confirmation')
PROBE = r'''
import json,subprocess,sys
request=json.load(sys.stdin)
result={'python':list(sys.version_info[:3])}
for name,args in [('help',['--help']),('version',['--version']),('auth',['auth','status'])]:
    try:
        row=subprocess.run(request['claude_exe']+args,capture_output=True,text=True,timeout=20)
        result[name]={'code':row.returncode,'stdout':row.stdout[:100000]}
    except Exception as exc:
        result[name]={'error':type(exc).__name__}
print(json.dumps(result))
'''


async def challenge(target):
    try:
        key = await asyncio.wait_for(asyncssh.get_server_host_key(target.host, port=target.port), 15)
    except Exception as exc:
        raise BridgeError('ssh_unavailable', '无法获取 SSH 主机密钥') from exc
    if key is None:
        raise BridgeError('host_untrusted', 'SSH 服务未返回主机密钥')
    identifier = str(uuid4())
    value = {'id': identifier, 'host': target.host, 'port': target.port, 'known_hosts': str(target.known_hosts),
             'fingerprint': key.get_fingerprint(), 'public_key': key.export_public_key().decode(), 'expires': time.time() + 600}
    write_json(state.root() / 'challenges' / (identifier + '.json'), value)
    return {'challenge_id': identifier, 'fingerprint': value['fingerprint'], 'expires': value['expires']}


async def trust(target, challenge_id, fingerprint):
    identifier = str(__import__('uuid').UUID(challenge_id))
    path = state.root() / 'challenges' / (identifier + '.json')
    with file_lock(path.with_suffix('.lock')):
        value = json.loads(path.read_text())
        if (value['host'], value['port'], value['known_hosts'], value['fingerprint']) != (target.host, target.port, str(target.known_hosts), fingerprint) or time.time() > value['expires']:
            raise BridgeError('challenge_invalid', '主机检查挑战过期或指纹不匹配')
        key = await asyncio.wait_for(asyncssh.get_server_host_key(target.host, port=target.port), 15)
        if key is None or key.get_fingerprint() != fingerprint:
            raise BridgeError('host_changed', '检查后主机密钥发生变化')
        host = target.host if target.port == 22 else f'[{target.host}]:{target.port}'
        if any(ch.isspace() for ch in host) or '\x00' in host:
            raise BridgeError('invalid_host', 'SSH 主机格式无效')
        before = target.known_hosts.read_bytes() if target.known_hosts.exists() else b''
        # Keep unrelated entries. A key change requires explicit trust with a fresh challenge.
        lines = [line for line in before.splitlines(keepends=True) if not line.startswith((host + ' ').encode())]
        after = b''.join(lines)
        if after and not after.endswith(b'\n'):
            after += b'\n'
        atomic_write(target.known_hosts, after + (host + ' ' + value['public_key'].strip() + '\n').encode())
        path.unlink()
        return {'trusted': True, 'fingerprint': fingerprint}


async def doctor(spec):
    result = {'schema_version': 1, 'checks': {name: {'status': 'untested'} for name in CHECKS}}
    try:
        async with connect_target(spec.target) as connection:
            result['checks']['ssh'] = {'status': 'ok'}
            result['checks']['host_identity'] = {'status': 'ok', 'fingerprint': connection.get_server_host_key().get_fingerprint()}
            process = await connection.create_process('python3 -c ' + shlex.quote(PROBE))
            stdout, _ = await asyncio.wait_for(process.communicate(json.dumps({'claude_exe': spec.claude_exe})), 65)
            if process.exit_status:
                raise BridgeError('python_unavailable', '远端 Python 探测失败')
            probe = json.loads(stdout)
            result['checks']['python'] = {'status': 'ok' if tuple(probe['python']) >= (3, 11) else 'failed', 'version': probe['python']}
            auth = probe.get('auth', {})
            try:
                configured = auth.get('code') == 0 and json.loads(auth.get('stdout', '{}')).get('loggedIn') is True
            except ValueError:
                configured = False
            result['checks']['claude_auth'] = {'status': 'untested' if configured else 'failed', 'configured': configured,
                                              'detail': '配置存在不代表模型认证通过；需真实模型调用验证'}
            help_text = probe.get('help', {}).get('stdout', '')
            required = ('--tools', '--strict-mcp-config', '--settings', '--setting-sources', '--disable-slash-commands', '--json-schema', '--session-id', '--resume')
            result['claude'] = {'version': probe.get('version', {}).get('stdout', '').strip(),
                                'missing_flags': [flag for flag in required if flag not in help_text]}
    except BridgeError as exc:
        result['error_code'] = exc.code
        result['checks']['ssh'] = {'status': 'failed', 'error_code': exc.code}
        if exc.code == 'host_untrusted':
            result['checks']['host_identity'] = {'status': 'failed', **await challenge(spec.target)}
    return result
