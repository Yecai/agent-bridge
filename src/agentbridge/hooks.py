"""Controlled Claude hooks. This module also runs as a standalone hook program."""
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from uuid import UUID, uuid5

MUTATIONS = {'write_file', 'apply_patch', 'exec', 'process_stop', 'dev_handoff', 'dev_finish'}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def operation_id(execution_id, tool_use_id):
    return str(uuid5(UUID(execution_id), tool_use_id))


def decide(event, execution_id, *, allow_structured=False):
    if event.get('hook_event_name') != 'PreToolUse':
        return {}
    name = event.get('tool_name', '')
    permitted = name.startswith('mcp__local__') or (name == 'StructuredOutput' and allow_structured)
    output = {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow' if permitted else 'deny'}
    if output['permissionDecision'] == 'deny':
        output['permissionDecisionReason'] = '只能通过 AgentBridge 操作本机项目'
    elif name.removeprefix('mcp__local__') in MUTATIONS:
        if not event.get('tool_use_id'):
            output.update(permissionDecision='deny', permissionDecisionReason='工具身份缺失')
        else:
            output['updatedInput'] = {**event.get('tool_input', {}), 'op_id': operation_id(execution_id, event['tool_use_id'])}
    return {'hookSpecificOutput': output}


def settings(command):
    names = ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'Stop', 'SessionEnd']
    return {'hooks': {name: [{'matcher': '*', 'hooks': [{'type': 'command', 'command': command, 'timeout': 10}]}]
                      for name in names}, 'enabledPlugins': {}, 'enableAllProjectMcpServers': False}


def main():
    # Trusted configuration lives next to the deployed hook, never in model arguments.
    config = json.loads(Path(sys.argv[1]).read_text())
    event = json.loads(sys.stdin.buffer.read(8 * 1024 * 1024))
    request = Request(config['url'], data=json.dumps(event).encode(), method='POST',
                      headers={'Authorization': 'Bearer ' + config['token'], 'Content-Type': 'application/json'})
    try:
        if event.get('session_id') == config.get('session_id') and event.get('transcript_path') and config.get('session_hint'):
            destination = Path(config['session_hint'])
            fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix='.session-')
            try:
                with os.fdopen(fd, 'w') as stream:
                    json.dump({'session_id': event['session_id'], 'transcript_path': event['transcript_path']}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=8) as response:
            if response.status != 200:
                raise OSError('hook audit rejected')
        print(json.dumps(decide(event, config['execution_id'], allow_structured=config.get('allow_structured') is True)))
    except Exception:
        print('AgentBridge hook audit unavailable', file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
