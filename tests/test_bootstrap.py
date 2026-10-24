import hashlib
import json
import os
import subprocess
import sys
import zipfile

import pytest
from agentbridge.bootstrap import BOOTSTRAP


def test_bootstrap_rejects_bad_digest_before_install(tmp_path):
    root = tmp_path / 'private'
    root.mkdir()
    wheel = root / 'agentbridge-0.1.0-py3-none-any.whl'
    wheel.write_bytes(b'not a wheel')
    result = subprocess.run([sys.executable, '-c', BOOTSTRAP], input=json.dumps({'wheel': str(wheel), 'sha256': 'a' * 64, 'home': str(root)}), text=True, capture_output=True)
    assert result.returncode != 0
    assert 'digest_mismatch' in result.stderr
    assert not list(root.glob('runtime/*/ready.json'))


def test_bootstrap_creates_executable_venv(tmp_path):
    from agentbridge.bootstrap import wheel_path
    wheel = wheel_path()
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    result = subprocess.run([sys.executable, '-c', BOOTSTRAP], input=json.dumps({'wheel': str(wheel), 'sha256': digest, 'home': str(tmp_path)}), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    installed = json.loads(result.stdout)
    check = subprocess.run([installed['python'], '-m', 'agentbridge', '--version'], capture_output=True, text=True)
    assert check.returncode == 0
    assert check.stdout.strip() == '0.1.0'

