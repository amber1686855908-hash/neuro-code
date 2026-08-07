from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from neuro_code.infrastructure.persistence.output_artifacts import FileToolOutputArtifactStore


class ToolOutputArtifactStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_redacts_before_bounded_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tool-output"
            store = FileToolOutputArtifactStore(
                root,
                redaction_values=("provider-secret",),
                max_bytes=64,
            )

            artifact = await store.save(
                tool_name="bash",
                content=b"token=provider-secret\n" + b"x" * 200,
                content_truncated=True,
            )

            path = root / Path(artifact.relative_path).name
            self.assertTrue(path.is_file())
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("provider-secret", content)
            self.assertLessEqual(path.stat().st_size, 64)
            self.assertTrue(artifact.truncated)
            self.assertEqual(artifact.byte_count, path.stat().st_size)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(root.stat().st_mode & 0o777, 0o700)

    async def test_artifact_path_is_relative_and_does_not_include_tool_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FileToolOutputArtifactStore(Path(directory))
            artifact = await store.save(tool_name="bash", content=b"safe")

            self.assertFalse(Path(artifact.relative_path).is_absolute())
            self.assertNotIn("bash", artifact.relative_path)
            self.assertNotIn("safe", artifact.relative_path)

    async def test_prune_keeps_referenced_and_recent_or_malformed_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tool-output"
            store = FileToolOutputArtifactStore(root)
            kept = await store.save(tool_name="bash", content=b"kept")
            orphan = await store.save(tool_name="bash", content=b"orphan")
            orphan_path = root / Path(orphan.relative_path).name
            old_timestamp = orphan_path.stat().st_mtime - 7200
            os.utime(orphan_path, (old_timestamp, old_timestamp))
            malformed = root / "not-an-artifact.log"
            malformed.write_text("preserve", encoding="utf-8")

            result = await store.prune_unreferenced(
                (kept.artifact_id,),
                min_age_seconds=3600,
            )

            self.assertEqual(result.deleted_count, 1)
            self.assertGreaterEqual(result.preserved_count, 2)
            self.assertTrue((root / Path(kept.relative_path).name).is_file())
            self.assertFalse(orphan_path.exists())
            self.assertTrue(malformed.is_file())

    def test_invalid_limits_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            FileToolOutputArtifactStore(Path(directory), max_bytes=0)


if __name__ == "__main__":
    unittest.main()
