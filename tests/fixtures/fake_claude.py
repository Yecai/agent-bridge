"""Real standalone process acting as Claude over HTTP MCP, without a model."""
import json
import os
import subprocess
import sys
from urllib.request import Request, urlopen


def main():
    args = sys.argv[1:]
    if args == ['--help']:
        print('--tools --strict-mcp-config --settings --setting-sources --disable-slash-commands --json-schema --session-id --resume')
        return
    if args == ['--version']:
        print('fixture-claude')
        return
    if args == ['auth', 'status']:
        print('{"loggedIn":true}')
        return
    prompt = sys.stdin.read()
    if os.environ.get('AGENTBRIDGE_FAKE_DELAY'):
        __import__('time').sleep(float(os.environ['AGENTBRIDGE_FAKE_DELAY']))
    config = json.load(open(args[args.index('--mcp-config') + 1]))['mcpServers']['local']
    counter = 0

    def rpc(method, params):
        nonlocal counter
        counter += 1
        request = Request(config['url'], data=json.dumps({'jsonrpc': '2.0', 'id': counter, 'method': method, 'params': params}).encode(),
                          headers={**config['headers'], 'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'})
        with urlopen(request, timeout=20) as response:
            return json.load(response)['result']

    rpc('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'fake', 'version': '1'}})
    tools={v['name'] for v in rpc('tools/list', {})['tools']}
    assert 'workspace_context' in tools
    def call(name, arguments):
        result = rpc('tools/call', {'name': name, 'arguments': arguments})
        assert not result.get('isError'), result
        return result['structuredContent']['data']
    context = call('workspace_context', {})
    if os.environ.get('AGENTBRIDGE_FAKE_HOLD'):
        call('exec', {'command':[sys.executable,'-c','import time;time.sleep(180)'],'op_id':'held-command'})
        __import__('time').sleep(180)
        return
    if os.environ.get('AGENTBRIDGE_FAKE_ROLES'):
        call('read_file', {'path':'AGENTS.md'})
        action=next((name for name in ['dev_handoff','dev_finish'] if name in tools),None)
        if action:
            from pathlib import Path
            directory=Path(args[args.index('--settings')+1]).parent
            def hook(event):
                completed=subprocess.run([sys.executable,str(directory/'hooks.py'),str(directory/'hook.json')],
                    input=json.dumps(event),capture_output=True,text=True,check=True)
                return json.loads(completed.stdout)
            if action=='dev_handoff':
                for name in ['bridge-plan.md','bridge-spec.md']:
                    call('write_file',{'path':name,'content':'# isolated callback proof\n','create':True,'op_id':name})
                arguments={'plan':str(Path(context['cwd'])/'bridge-plan.md'),'spec':str(Path(context['cwd'])/'bridge-spec.md')}
            else:
                arguments={'summary':'isolated real SSH callback delivery'}
            session=args[args.index('--session-id')+1]
            event={'hook_event_name':'PreToolUse','tool_name':'mcp__local__'+action,'tool_use_id':'business-proof',
                   'session_id':session,'tool_input':arguments}
            output=hook(event)['hookSpecificOutput']
            assert output['permissionDecision']=='allow'
            arguments=output['updatedInput']
            receipt=call(action,arguments)
            assert receipt['state']=='accepted'
            hook({**event,'hook_event_name':'PostToolUse','tool_input':arguments,'tool_response':receipt})
            __import__('time').sleep(60)  # Only the receipt ACK and supervisor may stop this model.
            return
        if 'exec' in tools:
            call('write_file', {'path':'proof.txt','content':'real-ssh-local-proof','create':True,'op_id':'write-1'})
            process=call('exec', {'command':['pwd'],'op_id':'cmd-1'})
            while True:
                output=call('process_output', {'process_id':process['process_id'],'wait_seconds':1})
                if output['state']=='exited':break
            assert context['cwd'] in output['stdout']
            call('process_stop', {'process_id':process['process_id'],'op_id':'stop-1'})
        session=args[args.index('--session-id')+1]
        print(json.dumps({'type':'system','subtype':'init','session_id':session}),flush=True)
        print(json.dumps({'type':'result','is_error':False,'result':'local-tools-proof','session_id':session,
                          'structured_output':{'passed':True,'cwd':context['cwd']}}),flush=True)
        return
    call('write_file', {'path': 'proof.txt', 'content': 'remote-model-local-file', 'create': True, 'op_id': 'write-1'})
    file = call('read_file', {'path': 'proof.txt'})
    call('apply_patch', {'path': 'proof.txt', 'expected_version': file['version'], 'old': 'remote-model', 'new': 'remote-claude', 'op_id': 'patch-1'})
    call('list_directory', {})
    call('glob', {'pattern': '*.txt'})
    call('grep', {'pattern': 'local-file'})
    process = call('exec', {'command': ['pwd'], 'op_id': 'cmd-1'})
    cursor, stdout = 0, ''
    while True:
        result = call('process_output', {'process_id': process['process_id'], 'wait_seconds': 1, 'cursor': cursor})
        cursor = result['next_cursor']
        stdout += result['stdout']
        if result['state'] == 'exited':
            break
    assert context['cwd'] in stdout
    call('process_stop', {'process_id': process['process_id'], 'op_id': 'stop-1'})
    session = args[args.index('--session-id') + 1] if '--session-id' in args else args[args.index('--resume') + 1]
    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': session}), flush=True)
    print(json.dumps({'type': 'result', 'result': 'local-tools-proof', 'is_error': False, 'session_id': session,
                      'structured_output': {'passed': True, 'cwd': context['cwd']}, 'usage': {'output_tokens': 1}}), flush=True)


if __name__ == '__main__':
    main()
