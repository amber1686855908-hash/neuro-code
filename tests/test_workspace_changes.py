from pathlib import Path

from neuro_code.workspace_changes import (
    capture_workspace_snapshot,
    compare_workspace_snapshots,
)


def test_change_report_is_bounded_to_workspace_files_and_redacts_secrets(tmp_path: Path) -> None:
    source = tmp_path / "src.py"
    removed = tmp_path / "removed.txt"
    sensitive = tmp_path / ".env"
    ignored = tmp_path / ".venv" / "ignored.py"
    source.write_text('API_KEY = "old-secret-value"\nprint("old")\n', encoding="utf-8")
    removed.write_text("remove me\n", encoding="utf-8")
    sensitive.write_text("TOKEN=old-sensitive-value\n", encoding="utf-8")
    ignored.parent.mkdir()
    ignored.write_text("old\n", encoding="utf-8")
    before = capture_workspace_snapshot(tmp_path)

    source.write_text('API_KEY = "new-secret-value"\nprint("new")\n', encoding="utf-8")
    removed.unlink()
    sensitive.write_text("TOKEN=new-sensitive-value\n", encoding="utf-8")
    (tmp_path / "created.txt").write_text("created\n", encoding="utf-8")
    ignored.write_text("new\n", encoding="utf-8")
    after = capture_workspace_snapshot(tmp_path)

    report = compare_workspace_snapshots(
        before,
        after,
        explicit_redactions=("old-secret-value", "new-secret-value"),
    )
    changes = {change["path"]: change for change in report["files"]}

    assert set(changes) == {".env", "created.txt", "removed.txt", "src.py"}
    assert changes["created.txt"]["status"] == "created"
    assert changes["removed.txt"]["status"] == "deleted"
    assert changes[".env"]["hidden_reason"] == "sensitive"
    assert changes["src.py"]["hidden_reason"] == "redacted"
    serialized = repr(report)
    assert "old-secret-value" not in serialized
    assert "new-secret-value" not in serialized
    assert "old-sensitive-value" not in serialized
    assert "new-sensitive-value" not in serialized
    assert ".venv/ignored.py" not in serialized
