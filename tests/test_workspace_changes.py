from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from neuro_code.application.ports.workspace_changes import (
    WorkspaceChangeCheckpoint,
    WorkspaceChangeReport,
    WorkspaceFileChange,
)
from neuro_code.infrastructure.workspace.changes import (
    FilesystemWorkspaceChangeObserver,
    MultiRootWorkspaceChangeObserver,
    capture_workspace_snapshot,
    compare_workspace_snapshots,
)


def test_snapshot_and_diff_limits_preserve_safe_metadata(tmp_path: Path) -> None:
    import neuro_code.infrastructure.workspace.changes as changes_module

    text = tmp_path / "text.txt"
    large = tmp_path / "large.txt"
    binary = tmp_path / "binary.bin"
    secret = tmp_path / ".env"
    budget = tmp_path / "z-budget.txt"
    # Use bytes so the fixture has the same size on POSIX and Windows (where
    # text-mode writes otherwise translate ``\n`` to ``\r\n``).
    text.write_bytes(b"b\n")
    large.write_text("l" * 21, encoding="utf-8")
    binary.write_bytes(b"\xff")
    secret.write_text("TOKEN=secret\n", encoding="utf-8")
    budget.write_text("b" * 10, encoding="utf-8")

    with (
        patch.object(changes_module, "_MAX_TEXT_FILE_BYTES", 20),
        patch.object(changes_module, "_MAX_CAPTURED_BYTES", 8),
        patch.object(changes_module, "_MAX_DIFF_LINES", 1),
    ):
        before = capture_workspace_snapshot(tmp_path)
        text.write_bytes(b"a\nb\nc\n")
        after = capture_workspace_snapshot(tmp_path)

        assert before.files["large.txt"].hidden_reason == "large"
        assert before.files["binary.bin"].hidden_reason == "binary"
        assert before.files[".env"].hidden_reason == "sensitive"
        assert before.files["z-budget.txt"].hidden_reason == "budget"
        assert before.scan_limited
        report = compare_workspace_snapshots(before, after)
        files = cast(list[dict[str, object]], report["files"])
        text_change = next(item for item in files if item["path"] == "text.txt")
        assert text_change["diff_truncated"] is True
        assert report["scan_limited"] is True


def test_observer_rejects_malformed_serialized_change_details(tmp_path: Path) -> None:
    import neuro_code.infrastructure.workspace.changes as changes_module

    observer = FilesystemWorkspaceChangeObserver()
    before = observer.capture(tmp_path)
    after = observer.capture(tmp_path)
    common = {"omitted_files": 0, "scan_limited": False}
    malformed = (
        {**common, "files": "not-a-list"},
        {**common, "files": [None]},
        {
            **common,
            "files": [
                {
                    "path": "x",
                    "status": "unknown",
                    "additions": 0,
                    "deletions": 0,
                    "diff": "",
                    "diff_truncated": False,
                }
            ],
        },
        {
            **common,
            "files": [
                {
                    "path": "x",
                    "status": "modified",
                    "additions": True,
                    "deletions": 0,
                    "diff": "",
                    "diff_truncated": False,
                }
            ],
        },
        {
            **common,
            "files": [
                {
                    "path": "x",
                    "status": "modified",
                    "additions": 0,
                    "deletions": 0,
                    "diff": None,
                    "diff_truncated": False,
                }
            ],
        },
        {
            **common,
            "files": [
                {
                    "path": "x",
                    "status": "modified",
                    "additions": 0,
                    "deletions": 0,
                    "hidden_reason": "sensitive",
                    "diff": "leak",
                    "diff_truncated": False,
                }
            ],
        },
        {
            **common,
            "files": [
                {
                    "path": "x",
                    "status": "modified",
                    "additions": 0,
                    "deletions": 0,
                    "hidden_reason": "not-a-reason",
                }
            ],
        },
    )
    for comparison in malformed:
        with (
            patch.object(changes_module, "compare_workspace_snapshots", return_value=comparison),
            pytest.raises(TypeError),
        ):
            observer.compare(before, after, explicit_redactions=())


def test_multi_root_observer_rejects_mismatch_and_bounds_report(tmp_path: Path) -> None:
    import neuro_code.infrastructure.workspace.changes as changes_module
    from neuro_code.infrastructure.workspace.changes import _MultiRootWorkspaceChangeCheckpoint

    primary = tmp_path / "primary"
    extra = tmp_path / "extra"
    primary.mkdir()
    extra.mkdir()
    observer = MultiRootWorkspaceChangeObserver(FilesystemWorkspaceChangeObserver(), (extra,))
    before = observer.capture(primary)
    with pytest.raises(TypeError, match="checkpoints do not match"):
        observer.compare(
            before,
            _MultiRootWorkspaceChangeCheckpoint(
                (FilesystemWorkspaceChangeObserver().capture(primary),)
            ),
            explicit_redactions=(),
        )

    changed = extra / "changed.txt"
    changed.write_text("after\n", encoding="utf-8")
    with patch.object(changes_module, "_MAX_CHANGED_FILES", 0):
        report = observer.compare(before, observer.capture(primary), explicit_redactions=())
    assert report.omitted_files == 1
    assert report.files == ()


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
    changes = {change["path"]: change for change in cast(list[dict[str, object]], report["files"])}

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


def test_visible_change_requires_diff_details_and_preserves_redaction_marker() -> None:
    with pytest.raises(ValueError, match="diff details"):
        WorkspaceFileChange("x.txt", "modified", 0, 0).to_event_payload()
    redacted = WorkspaceFileChange(
        "x.txt", "modified", 1, 0, "diff", False, hidden_reason="redacted"
    )
    assert redacted.to_event_payload()["hidden_reason"] == "redacted"


def test_multi_root_change_observer_labels_explicit_additional_directory(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    additional = tmp_path / "additional"
    additional.mkdir()
    observer = MultiRootWorkspaceChangeObserver(
        FilesystemWorkspaceChangeObserver(),
        (additional,),
    )

    before = observer.capture(primary)
    changed = additional / "shared.txt"
    changed.write_text("new\n", encoding="utf-8")
    after = observer.capture(primary)

    report = observer.compare(before, after, explicit_redactions=())
    assert [change.path for change in report.files] == [str(changed)]
