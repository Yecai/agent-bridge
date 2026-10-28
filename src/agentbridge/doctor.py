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
from .runner import Runner
from .ssh import connect_target
from .storage import atomic_write, file_lock, private_dir, write_json

CHECKS = ('ssh', 'host_identity', 'python', 'claude_auth', 'mcp_roundtrip', 'print_schema', 'interactive_audit', 'stop_confirmation')
REQUIRED_FLAGS = ('--tools', '--strict-mcp-config', '--settings', '--setting-sources',
                  '--json-schema', '--session-id', '--resume', '--dangerously-skip-permissions')
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


def _evidence(runner, nonce):
    rows = runner.runtime.audit.read(limit=10000)['events']
    tools = {row.get('details', {}).get('tool') for row in rows
             if row.get('event') == 'tool.end' and row.get('status') in {'ok', 'running'}}
    assistant = any(row.get('event') == 'claude.transcript' and (row.get('data') or {}).get('type') == 'assistant'
                    and nonce in json.dumps((row.get('data') or {}).get('message', {})) for row in rows)
    stopped = any(row.get('event') == 'claude.hook' and row.get('details', {}).get('hook') == 'Stop' for row in rows)
    return tools, assistant and stopped


async def _probe_run(runner, nonce, *, timeout=180):
    task = asyncio.create_task(runner.run())
    deadline = time.monotonic() + timeout
    ready = False
    while not task.done() and time.monotonic() < deadline:
        if runner.spec.mode == 'interactive':
            tools, audited = _evidence(runner, nonce)
            if {'workspace_context', 'read_file'} <= tools and audited:
                ready = True
                runner.cancelled.set()
                break
        await asyncio.sleep(.2)
    if not task.done():
        runner.cancelled.set()
    try:
        status = await asyncio.wait_for(asyncio.shield(task), 75)
    except Exception as error:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return {'state': 'stop_unconfirmed', 'stop_confirmed': False, 'error_code': type(error).__name__}, ready
    return status, ready


async def probe_tools(spec):
    """显式调用模型，在独立本机目录验证能力；日志与未知停止状态保留。"""
    root = private_dir(spec.log_dir)
    nonce = str(uuid4())
    workspace = private_dir(root / ('probe-' + nonce))
    atomic_write(workspace / 'probe.txt', nonce.encode())
    checks = {key: {'status': 'untested'} for key in CHECKS[3:]}
    common = dict(task_id=None, callbacks={}, callback_token=None, cwd=workspace, read_roots=[workspace],
                  resume=False, session_id=None, workspace_id='doctor-' + nonce)
    command = ['python3', '-c', f"from pathlib import Path;Path('command-proof').write_text({nonce!r})"]
    prompt = ('这是隔离的 AgentBridge 能力测试。调用 workspace_context、read_file 读取 probe.txt；'
              '用 write_file 创建 file-proof，内容与 probe.txt 完全一致；通过 exec 执行下面 JSON 命令数组，'
              'process_output 等待退出码 0，再 process_stop 确认停止。最后用 StructuredOutput 返回 '
              '{nonce: probe.txt 的完整内容, cwd: workspace_context 的真实 cwd}。命令：' + json.dumps(command))
    schema = {'type': 'object', 'properties': {'nonce': {'type': 'string'}, 'cwd': {'type': 'string'}},
              'required': ['nonce', 'cwd'], 'additionalProperties': False}
    print_spec = spec.model_copy(update={**common, 'execution_id': str(uuid4()), 'mode': 'print',
        'write_roots': [workspace], 'access': 'full', 'log_dir': root / 'print', 'prompt': prompt, 'result_schema': schema})
    printing = Runner(print_spec)
    print_status, _ = await _probe_run(printing, nonce)
    tools, _ = _evidence(printing, nonce)
    final = printing.result or {}
    schema_ok = final.get('structured_output') == {'nonce': nonce, 'cwd': str(workspace)}
    checks['claude_auth'] = {'status': 'ok' if final and not final.get('is_error') and not printing.fatal_error else 'failed',
                             'error_code': print_status.get('error_code')}
    def proof(name):
        path = workspace / name
        return path.is_file() and not path.is_symlink() and path.stat().st_size < 1024 and path.read_text() == nonce
    tools_ok = {'workspace_context', 'read_file', 'write_file', 'exec', 'process_output', 'process_stop'} <= tools
    checks['mcp_roundtrip'] = {'status': 'ok' if tools_ok and proof('file-proof') and proof('command-proof') else 'failed'}
    checks['print_schema'] = {'status': 'ok' if schema_ok and print_status.get('exit_code') == 0 else 'failed'}
    statuses = [print_status]
    if checks['claude_auth']['status'] == 'ok':
        interactive_spec = spec.model_copy(update={**common, 'execution_id': str(uuid4()), 'mode': 'interactive',
            'write_roots': [], 'access': 'read', 'log_dir': root / 'interactive', 'result_schema': None,
            'prompt': '调用 workspace_context，read_file 读取 probe.txt，然后仅回复它的完整内容。不要运行命令或修改文件。'})
        interactive = Runner(interactive_spec)
        interactive_status, ready = await _probe_run(interactive, nonce)
        statuses.append(interactive_status)
        checks['interactive_audit'] = {'status': 'ok' if ready else 'failed'}
    checks['stop_confirmation'] = {'status': 'ok' if all(row.get('stop_confirmed') is True for row in statuses) else 'failed'}
    result = {'checks': checks, 'log_dir': str(root), 'executions': statuses}
    write_json(root / 'doctor-result.json', result)
    return result


async def doctor(spec, *, test_tools=False):
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
            result['claude'] = {'version': probe.get('version', {}).get('stdout', '').strip(),
                                'missing_flags': [flag for flag in REQUIRED_FLAGS if flag not in help_text]}
    except BridgeError as exc:
        result['error_code'] = exc.code
        check = 'python' if exc.code == 'python_unavailable' else 'ssh'
        result['checks'][check] = {'status': 'failed', 'error_code': exc.code}
        if exc.code == 'host_untrusted':
            result['checks']['host_identity'] = {'status': 'failed', **await challenge(spec.target)}
    if test_tools and result['checks']['ssh']['status'] == 'ok' and result['checks']['python']['status'] == 'ok' and not result['claude']['missing_flags']:
        deep = await probe_tools(spec)
        result['checks'].update(deep['checks'])
        result.update(log_dir=deep['log_dir'], executions=deep['executions'])
    return result
