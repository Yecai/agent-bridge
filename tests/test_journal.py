import pytest


def test_duplicate_and_result_survive_reopen(tmp_path):
    from agentbridge.journal import Journal
    first = Journal(tmp_path / "ops.sqlite3")
    assert first.begin("e1", "o1", "digest")["created"]
    assert not first.begin("e1", "o1", "digest")["created"]
    first.finish("e1", "o1", {"status": "ok", "data": {"value": 7}})
    second = Journal(tmp_path / "ops.sqlite3")
    assert second.begin("e1", "o1", "digest")["result"]["data"]["value"] == 7


def test_operation_conflict_and_unknown(tmp_path):
    from agentbridge.journal import Journal
    from agentbridge.errors import BridgeError
    journal = Journal(tmp_path / "ops.sqlite3")
    journal.begin("e1", "o1", "a")
    with pytest.raises(BridgeError, match="operation_conflict"):
        journal.begin("e1", "o1", "b")
    journal.reconcile("e1")
    assert journal.lookup("e1", "o1")["state"] == "outcome_unknown"
