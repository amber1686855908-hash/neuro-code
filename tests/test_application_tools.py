from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from neuro_code.application.ports.tools import (
    MAX_TOOL_OUTPUT_ARTIFACT_BYTES,
    ToolOutputArtifact,
    ToolOutputArtifactPruneResult,
)
from neuro_code.application.sessions import (
    ListSessionsPageRequest,
    SessionApplicationService,
)
from neuro_code.application.sessions.event_queries import (
    LoadSessionEventsRequest,
    SessionEventQueryService,
)
from neuro_code.application.sessions.summary import (
    GetSessionSummaryRequest,
    SessionSummaryQueryService,
)
from neuro_code.application.tools import (
    ListSessionToolOutputArtifactsRequest,
    ReadSessionToolOutputArtifactRequest,
    ReadToolOutputArtifactRequest,
    SessionToolOutputArtifactApplicationService,
    ToolOutputArtifactApplicationService,
)
from neuro_code.domain.sessions import SessionSummary
from neuro_code.infrastructure.persistence.output_artifacts import FileToolOutputArtifactStore
from neuro_code.shared.errors import SessionError


class EventStore:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events

    async def get_session(self, session_id: str) -> SessionSummary:
        return SessionSummary(
            session_id,
            "/workspace",
            "provider",
            "model",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 1, tzinfo=UTC),
        )

    async def load_events(self, session_id: str) -> list[dict[str, Any]]:
        return self.events


class PrunableEventStore(EventStore):
    def __init__(
        self, summaries: list[SessionSummary], events_by_session: dict[str, list[dict[str, Any]]]
    ) -> None:
        super().__init__([])
        self.summaries = summaries
        self.events_by_session = events_by_session

    async def list_sessions_page(self, **kwargs: object) -> list[SessionSummary]:
        if kwargs.get("before_id") is not None:
            return []
        return self.summaries

    async def load_events(self, session_id: str) -> list[dict[str, Any]]:
        return self.events_by_session.get(session_id, [])


class RecordingGarbageCollector:
    def __init__(self) -> None:
        self.keep_ids: frozenset[str] | None = None

    async def prune_unreferenced(
        self,
        keep_artifact_ids: set[str] | frozenset[str],
        *,
        min_age_seconds: float = 3600,
    ) -> ToolOutputArtifactPruneResult:
        del min_age_seconds
        self.keep_ids = frozenset(keep_artifact_ids)
        return ToolOutputArtifactPruneResult(0, 0)


class ToolOutputArtifactApplicationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_only_a_bounded_redacted_handle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FileToolOutputArtifactStore(
                Path(directory),
                redaction_values=("configured-secret",),
            )
            artifact = await store.save(
                tool_name="bash",
                content=b"configured-secret\n" + b"x" * 200,
                content_truncated=True,
            )
            result = await ToolOutputArtifactApplicationService(store).read(
                ReadToolOutputArtifactRequest(artifact, max_bytes=32),
            )

            self.assertIs(result.artifact, artifact)
            self.assertNotIn("configured-secret", result.content)
            self.assertLessEqual(len(result.content.encode("utf-8")), 32)
            self.assertTrue(result.read_truncated)

    async def test_rejects_forged_relative_path_and_missing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FileToolOutputArtifactStore(Path(directory))
            artifact = await store.save(tool_name="bash", content=b"safe")
            service = ToolOutputArtifactApplicationService(store)

            forged = replace(artifact, relative_path="other.log")
            with self.assertRaisesRegex(ValueError, "path does not match"):
                await service.read(ReadToolOutputArtifactRequest(forged))

            missing = replace(
                artifact, artifact_id="0" * 32, relative_path="tool-output/" + "0" * 32 + ".log"
            )
            with self.assertRaises(FileNotFoundError):
                await service.read(ReadToolOutputArtifactRequest(missing))

    def test_request_bounds_are_explicit_and_typed(self) -> None:
        artifact_id = "0" * 32
        artifact = ToolOutputArtifact(
            artifact_id,
            f"tool-output/{artifact_id}.log",
            0,
            False,
        )
        self.assertEqual(
            ReadToolOutputArtifactRequest(artifact, MAX_TOOL_OUTPUT_ARTIFACT_BYTES).max_bytes,
            MAX_TOOL_OUTPUT_ARTIFACT_BYTES,
        )
        with self.assertRaises(ValueError):
            ReadToolOutputArtifactRequest(artifact, MAX_TOOL_OUTPUT_ARTIFACT_BYTES + 1)
        with self.assertRaises(ValueError):
            ReadToolOutputArtifactRequest(artifact, 0)

    async def test_lists_only_bounded_artifacts_from_persisted_session_events(self) -> None:
        artifact_id = "a" * 32
        second_id = "b" * 32
        events = [
            {
                "sequence": 1,
                "kind": "tool_completed",
                "data": {
                    "metadata": {
                        "output_artifact_id": artifact_id,
                        "output_artifact_path": f"tool-output/{artifact_id}.log",
                        "output_artifact_bytes": 12,
                        "output_artifact_truncated": True,
                    }
                },
            },
            {
                "sequence": 2,
                "kind": "text_delta",
                "data": {
                    "metadata": {
                        "output_artifact_id": "c" * 32,
                        "output_artifact_path": f"tool-output/{'c' * 32}.log",
                        "output_artifact_bytes": 8,
                        "output_artifact_truncated": False,
                    }
                },
            },
            {
                "sequence": 3,
                "kind": "tool_completed",
                "data": {
                    "metadata": {
                        "output_artifact_id": second_id,
                        "output_artifact_path": f"tool-output/{second_id}.log",
                        "output_artifact_bytes": 4,
                        "output_artifact_truncated": False,
                    }
                },
            },
            {
                "sequence": 4,
                "kind": "tool_completed",
                "data": {
                    "metadata": {
                        "output_artifact_id": artifact_id,
                        "output_artifact_path": f"tool-output/{artifact_id}.log",
                        "output_artifact_bytes": 999,
                        "output_artifact_truncated": False,
                    }
                },
            },
        ]
        service = SessionToolOutputArtifactApplicationService(
            EventStore(events),
            reader=FileToolOutputArtifactStore(Path(tempfile.mkdtemp())),
        )

        references = await service.list(ListSessionToolOutputArtifactsRequest("session", limit=1))

        self.assertEqual(len(references), 1)
        self.assertEqual(references[0].event_sequence, 3)
        self.assertEqual(references[0].artifact.artifact_id, second_id)

    async def test_artifact_session_lookup_uses_application_summary_seam(self) -> None:
        artifact_id = "a" * 32
        captured: list[GetSessionSummaryRequest] = []
        captured_events: list[LoadSessionEventsRequest] = []
        original = SessionSummaryQueryService.get_session_summary
        original_events = SessionEventQueryService.load_session_events

        async def capture(
            service: SessionSummaryQueryService,
            request: GetSessionSummaryRequest,
        ) -> object:
            captured.append(request)
            return await original(service, request)

        async def capture_events(
            service: SessionEventQueryService,
            request: LoadSessionEventsRequest,
        ) -> object:
            captured_events.append(request)
            return await original_events(service, request)

        event_store = EventStore(
            [
                {
                    "sequence": 1,
                    "kind": "tool_completed",
                    "data": {
                        "metadata": {
                            "output_artifact_id": artifact_id,
                            "output_artifact_path": f"tool-output/{artifact_id}.log",
                            "output_artifact_bytes": 1,
                            "output_artifact_truncated": False,
                        }
                    },
                }
            ]
        )
        service = SessionToolOutputArtifactApplicationService(
            event_store,
            reader=FileToolOutputArtifactStore(Path(tempfile.mkdtemp())),
        )

        with (
            patch.object(SessionSummaryQueryService, "get_session_summary", new=capture),
            patch.object(SessionEventQueryService, "load_session_events", new=capture_events),
        ):
            await service.list(ListSessionToolOutputArtifactsRequest("session-1"))

        self.assertEqual(captured, [GetSessionSummaryRequest("session-1")])
        self.assertEqual(captured_events, [LoadSessionEventsRequest("session-1")])

    async def test_session_read_requires_persisted_association(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FileToolOutputArtifactStore(Path(directory), redaction_values=("secret",))
            artifact = await store.save(
                tool_name="bash",
                content=b"secret\nvisible output",
                content_truncated=True,
            )
            session_store = EventStore(
                [
                    {
                        "sequence": 9,
                        "kind": "tool_completed",
                        "data": {
                            "metadata": {
                                "output_artifact_id": artifact.artifact_id,
                                "output_artifact_path": artifact.relative_path,
                                "output_artifact_bytes": artifact.byte_count,
                                "output_artifact_truncated": artifact.truncated,
                            }
                        },
                    }
                ]
            )
            service = SessionToolOutputArtifactApplicationService(session_store, store)

            result = await service.read(
                ReadSessionToolOutputArtifactRequest("session", artifact.artifact_id, 64)
            )
            self.assertNotIn("secret", result.content)
            self.assertIn("visible output", result.content)

            with self.assertRaisesRegex(SessionError, "not associated"):
                await service.read(ReadSessionToolOutputArtifactRequest("session", "f" * 32, 64))

    async def test_prune_scans_all_sessions_before_calling_collector(self) -> None:
        first_id = "a" * 32
        second_id = "b" * 32
        summaries = [
            SessionSummary(
                "first",
                "/workspace",
                "provider",
                "model",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 2, tzinfo=UTC),
            ),
            SessionSummary(
                "second",
                "/workspace",
                "provider",
                "model",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ]
        events_by_session = {
            "first": [
                {
                    "sequence": 1,
                    "kind": "tool_completed",
                    "data": {
                        "metadata": {
                            "output_artifact_id": first_id,
                            "output_artifact_path": f"tool-output/{first_id}.log",
                            "output_artifact_bytes": 1,
                            "output_artifact_truncated": True,
                        }
                    },
                }
            ],
            "second": [
                {
                    "sequence": 1,
                    "kind": "text_delta",
                    "data": {
                        "metadata": {
                            "output_artifact_id": second_id,
                            "output_artifact_path": f"tool-output/{second_id}.log",
                            "output_artifact_bytes": 1,
                            "output_artifact_truncated": False,
                        }
                    },
                }
            ],
        }
        collector = RecordingGarbageCollector()
        service = SessionToolOutputArtifactApplicationService(
            PrunableEventStore(summaries, events_by_session),
            reader=FileToolOutputArtifactStore(Path(tempfile.mkdtemp())),
            garbage_collector=collector,
        )

        captured: list[ListSessionsPageRequest] = []
        original = SessionApplicationService.list_sessions_page

        async def capture_page(
            session_service: SessionApplicationService,
            request: ListSessionsPageRequest,
        ) -> tuple[SessionSummary, ...]:
            captured.append(request)
            return await original(session_service, request)

        with patch.object(SessionApplicationService, "list_sessions_page", new=capture_page):
            await service.prune_unreferenced()

        self.assertEqual(collector.keep_ids, frozenset({first_id}))
        self.assertEqual(captured, [ListSessionsPageRequest(1000)])


if __name__ == "__main__":
    unittest.main()
