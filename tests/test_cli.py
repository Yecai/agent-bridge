import json
import os
import subprocess
import sys


def test_invalid_cli_request_is_safe(tmp_path):
    request = tmp_path / 'request.json'
    request.write_text('{"schema_version":99,"password":"do-not-echo"}')
    request.chmod(0o600)
    result = subprocess.run([sys.executable, '-m', 'agentbridge', 'run', '--request-file', str(request)], capture_output=True, text=True)
    assert result.returncode != 0
    assert result.stdout == ''
    assert json.loads(result.stderr)['error_code'] == 'invalid_request'
    assert 'do-not-echo' not in result.stderr


def test_request_must_be_private(spec, tmp_path):
    request = tmp_path / 'request.json'
    request.write_text(spec.model_dump_json())
    request.chmod(0o644)
    result = subprocess.run([sys.executable, '-m', 'agentbridge', 'run', '--request-file', str(request)], capture_output=True, text=True)
    assert result.returncode != 0
    assert json.loads(result.stderr)['error_code'] == 'request_not_private'


def test_local_execution_request_identity_is_immutable(spec,tmp_path,monkeypatch):
    import asyncio
    import pytest
    from agentbridge.cli import ensure_request_identity
    from agentbridge.errors import BridgeError
    from agentbridge import state
    monkeypatch.setenv('AGENTBRIDGE_STATE',str(tmp_path/'state'))
    ensure_request_identity(spec)
    ensure_request_identity(spec)
    with pytest.raises(BridgeError,match='execution_conflict'):
        ensure_request_identity(spec.model_copy(update={'prompt':'changed'}))
