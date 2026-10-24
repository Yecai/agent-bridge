from uuid import uuid4

import pytest

from agentbridge.protocol import RunSpec, SshTarget


def make_spec(tmp_path, access="full"):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    return RunSpec(trace_id=str(uuid4()), execution_id=str(uuid4()), node_id="node-1", control_revision=0,
                   machine_id="machine-1", client_id="client-1", workspace_id="workspace-1", cwd=root,
                   read_roots=[root], write_roots=[root] if access == "full" else [], log_dir=tmp_path / "logs",
                   access=access, mode="print", target=SshTarget(host="fixture.invalid", user="tester",
                   password="fixture-secret", known_hosts=tmp_path / "known_hosts"))


@pytest.fixture
def spec(tmp_path):
    return make_spec(tmp_path)
