import hashlib

import pytest


def test_symlink_and_readonly_boundary(tmp_path):
    from agentbridge.workspace import Workspace
    from agentbridge.errors import BridgeError
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    workspace = Workspace(root, [root], [])
    with pytest.raises(BridgeError, match="path_not_allowed"):
        workspace.resolve(root / "escape" / "file", write=True)
    with pytest.raises(BridgeError, match="path_not_allowed"):
        workspace.resolve(root / "file", write=True)


def test_atomic_create_patch_and_version_conflict(spec):
    from agentbridge.workspace import Workspace
    from agentbridge.files import FileTools
    from agentbridge.errors import BridgeError
    files = FileTools(Workspace(spec.cwd, spec.read_roots, spec.write_roots))
    written = files.write_file("中文 'file.txt", "hello", create=True)
    assert written["version"] == hashlib.sha256(b"hello").hexdigest()
    assert files.read_file("中文 'file.txt")["text"] == "hello"
    patched = files.apply_patch("中文 'file.txt", written["version"], "hello", "world")
    assert patched["version"] != written["version"]
    with pytest.raises(BridgeError, match="file_conflict"):
        files.write_file("中文 'file.txt", "lost", expected_version=written["version"])
    assert files.read_file("中文 'file.txt")["text"] == "world"


def test_search_pagination_and_context(spec):
    from agentbridge.workspace import Workspace
    from agentbridge.files import FileTools
    from agentbridge.search import SearchTools
    (spec.cwd / "a.txt").write_text("needle\n")
    (spec.cwd / "b.txt").write_text("needle again\n")
    workspace = Workspace(spec.cwd, spec.read_roots, spec.write_roots)
    search = SearchTools(workspace)
    first = search.grep("needle", limit=1)
    assert len(first["matches"]) == 1
    assert first["next_cursor"]
    second = search.grep("needle", cursor=first["next_cursor"], limit=1)
    assert first["matches"][0]["path"] != second["matches"][0]["path"]
    assert len(search.glob("*.txt")["paths"]) == 2
    assert FileTools(workspace).workspace_context()["cwd"] == str(spec.cwd.resolve())
