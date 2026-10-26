import pytest
from pydantic import ValidationError


def test_tools_spec_needs_no_ssh_and_preserves_read_access(spec):
    from agentbridge.protocol import ToolsSpec
    values = spec.model_dump(mode='json')
    for key in ('target', 'claude_exe', 'model', 'effort', 'prompt', 'session_id', 'resume', 'result_schema', 'callback_token'):
        values.pop(key, None)
    values.update(mode='tools', callbacks={})
    tools = ToolsSpec.model_validate(values)
    assert tools.secrets() == []
    assert 'target' not in tools.model_dump()
    with pytest.raises(ValidationError):
        ToolsSpec.model_validate({**values, 'access': 'read'})
    with pytest.raises(ValidationError):
        ToolsSpec.model_validate({**values, 'callbacks': {'dev_finish': 'http://localhost'}})


def test_catalog_is_readonly_and_matches_mcp():
    from agentbridge.tool_catalog import describe
    read = describe('read')
    full = describe('full')
    assert {t['name'] for t in read} == {'workspace_context', 'read_file', 'list_directory', 'glob', 'grep'}
    assert {t['name'] for t in full} == {t['name'] for t in read} | {'write_file', 'apply_patch', 'exec', 'process_output', 'process_stop'}
    assert all(t['input_schema']['type'] == 'object' for t in full)
    assert next(t for t in full if t['name'] == 'exec')['input_schema']['properties']['command']
