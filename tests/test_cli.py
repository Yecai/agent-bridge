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
