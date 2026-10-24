import subprocess
import sys

import pytest


def test_password_is_private(tmp_path):
    from agentbridge.protocol import SshTarget
    target = SshTarget(host="fixture.invalid", user="tester", password="fixture-secret", known_hosts=tmp_path / "kh")
    assert "fixture-secret" not in repr(target)
    assert target.password.get_secret_value() == "fixture-secret"


def test_invalid_port_and_unknown_fields_rejected(tmp_path):
    from pydantic import ValidationError
    from agentbridge.protocol import SshTarget
    for values in ({"port": 0}, {"port": True}, {"unexpected": "value"}):
        with pytest.raises(ValidationError):
            SshTarget(host="fixture.invalid", user="tester", known_hosts=tmp_path / "kh", **values)


def test_cli_version():
    result = subprocess.run([sys.executable, "-m", "agentbridge", "--version"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.1.0"
