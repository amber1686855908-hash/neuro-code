from pathlib import Path

import pytest

from neuro_code.application.ports.workspace_changes import (
    WorkspaceChangeCheckpoint,
    WorkspaceChangeReport,
)
from neuro_code.workspace_changes import (
    FilesystemWorkspaceChangeObserver,
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


def test_filesystem_observer_preserves_the_existing_serialized_report(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    hidden = tmp_path / ".env"
    source.write_text("before\n", encoding="utf-8")
    hidden.write_text("TOKEN=before-secret\n", encoding="utf-8")
    expected_before = capture_workspace_snapshot(tmp_path)
    observer = FilesystemWorkspaceChangeObserver()
    observer_before = observer.capture(tmp_path)

    source.write_text("after\n", encoding="utf-8")
    hidden.write_text("TOKEN=after-secret\n", encoding="utf-8")
    expected_after = capture_workspace_snapshot(tmp_path)
    observer_after = observer.capture(tmp_path)

    expected = compare_workspace_snapshots(
        expected_before,
        expected_after,
        explicit_redactions=("before-secret", "after-secret"),
    )
    report = observer.compare(
        observer_before,
        observer_after,
        explicit_redactions=("before-secret", "after-secret"),
    )

    assert report.to_event_payload() == expected
    assert list(report.to_event_payload()) == ["files", "omitted_files", "scan_limited"]
    source_change = next(change for change in report.files if change.path == "source.txt")
    assert source_change.status == "modified"
    assert source_change.diff is not None
    assert "before-secret" not in source_change.diff
    assert "after-secret" not in source_change.diff


def test_filesystem_observer_rejects_a_checkpoint_from_another_observer(tmp_path: Path) -> None:
    class ForeignCheckpoint(WorkspaceChangeCheckpoint):
        __slots__ = ()

    observer = FilesystemWorkspaceChangeObserver()
    checkpoint = observer.capture(tmp_path)

    with pytest.raises(TypeError, match="different observer"):
        observer.compare(
            ForeignCheckpoint(),
            checkpoint,
            explicit_redactions=(),
        )


def test_workspace_change_report_ignores_omitted_files_without_visible_changes() -> None:
    assert not WorkspaceChangeReport(files=(), omitted_files=1, scan_limited=False).should_emit
    assert WorkspaceChangeReport(files=(), omitted_files=0, scan_limited=True).should_emit
