import asyncio
import json

from agentbridge import doctor as module
from agentbridge.runtime import Runtime


class ProbeRunner:
    seen = []

    def __init__(self, spec):
        self.spec = spec
        self.runtime = Runtime(spec)
        self.cancelled = asyncio.Event()
        self.result = None
        self.fatal_error = None
        self.seen.append(spec)

    async def run(self):
        nonce = (self.spec.cwd / 'probe.txt').read_text()
        for name in ['workspace_context', 'read_file']:
            self.runtime.audit.append('tool.end', 'ok', {'tool': name})
        if self.spec.mode == 'print':
            for name in ['write_file', 'exec', 'process_output', 'process_stop']:
                self.runtime.audit.append('tool.end', 'ok', {'tool': name})
            (self.spec.cwd / 'file-proof').write_text(nonce)
            (self.spec.cwd / 'command-proof').write_text(nonce)
            self.result = {'is_error': False, 'structured_output': {'nonce': nonce, 'cwd': str(self.spec.cwd)}}
        else:
            self.runtime.audit.append('claude.transcript', 'received', data={'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': nonce}]}})
            self.runtime.audit.append('claude.hook', 'received', {'hook': 'Stop'})
            await self.cancelled.wait()
        return {'state': 'exited', 'stop_confirmed': True, 'exit_code': 0}


async def test_explicit_probe_uses_isolated_workspace_and_requires_actual_evidence(spec, monkeypatch):
    monkeypatch.setattr(module, 'Runner', ProbeRunner)
    ProbeRunner.seen = []
    (spec.cwd / 'user-project').write_text('unchanged')
    result = await module.probe_tools(spec)
    assert all(result['checks'][key]['status'] == 'ok' for key in ['claude_auth', 'mcp_roundtrip', 'print_schema', 'interactive_audit', 'stop_confirmation'])
    assert len(ProbeRunner.seen) == 2
    for probe in ProbeRunner.seen:
        assert probe.cwd != spec.cwd
        assert probe.read_roots == [probe.cwd]
        assert not probe.callbacks and not probe.resume
    assert (spec.cwd / 'user-project').read_text() == 'unchanged'
    assert (spec.log_dir / 'doctor-result.json').is_file()
    assert 'fixture-secret' not in json.dumps(result)


async def test_probe_does_not_pass_without_tool_or_transcript_evidence(spec, monkeypatch):
    class EmptyRunner(ProbeRunner):
        async def run(self):
            self.result = {'is_error': False, 'structured_output': {}}
            return {'state': 'exited', 'stop_confirmed': True, 'exit_code': 0}
    monkeypatch.setattr(module, 'Runner', EmptyRunner)
    result = await module.probe_tools(spec)
    assert result['checks']['mcp_roundtrip']['status'] == 'failed'
    assert result['checks']['print_schema']['status'] == 'failed'
    assert result['checks']['interactive_audit']['status'] == 'failed'
    assert result['checks']['stop_confirmation']['status'] == 'ok'


async def test_python_probe_failure_keeps_successful_ssh_identity(spec,monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    class Connection:
        def get_server_host_key(self):return SimpleNamespace(get_fingerprint=lambda:'fixture-fingerprint')
        async def create_process(self,command):
            class Process:
                exit_status=127
                async def communicate(self,data):return '','python unavailable'
            return Process()
    @asynccontextmanager
    async def connect(target):yield Connection()
    monkeypatch.setattr(module,'connect_target',connect)
    result=await module.doctor(spec)
    assert result['checks']['ssh']['status']=='ok'
    assert result['checks']['host_identity']['status']=='ok'
    assert result['checks']['python']['status']=='failed'
    assert result['error_code']=='python_unavailable'
