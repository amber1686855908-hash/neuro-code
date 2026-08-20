from __future__ import annotations

import ast
import dataclasses
import importlib
import importlib.util
import inspect
import subprocess
import sys
import tomllib
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_ROOT = _PROJECT_ROOT / "src" / "neuro_code"

_EXPECTED_IMPORTS = (
    ("neuro_code", "__version__"),
    ("neuro_code.acp", "NeuroCodeAcpAgent"),
    ("neuro_code.acp", "serve_acp"),
    ("neuro_code.infrastructure.persistence.sqlite_session", "SqliteSessionStore"),
    ("neuro_code.infrastructure.persistence", "RustSessionImport"),
    ("neuro_code.infrastructure.persistence", "load_rust_session"),
    ("neuro_code.infrastructure.persistence", "UPSTREAM_IMPORT_PROVIDER"),
    (
        "neuro_code.infrastructure.persistence.rust_session",
        "RustSessionImport",
    ),
    (
        "neuro_code.infrastructure.persistence.rust_session",
        "load_rust_session",
    ),
    ("neuro_code.infrastructure.persistence", "JsonUiPreferencesStore"),
    (
        "neuro_code.infrastructure.persistence.ui_preferences",
        "JsonUiPreferencesStore",
    ),
    ("neuro_code.infrastructure.background_tasks", "LocalBackgroundTaskManager"),
    (
        "neuro_code.infrastructure.workspace.instructions",
        "FilesystemInstructionDiscovery",
    ),
    (
        "neuro_code.infrastructure.workspace.skills",
        "FilesystemSkillDiscovery",
    ),
    ("neuro_code.application", "ApplicationSettings"),
    (
        "neuro_code.configuration.managed_provider_settings",
        "load_managed_provider_settings",
    ),
    ("neuro_code.application.runtime.agent", "AgentRunResult"),
    ("neuro_code.application.runtime.agent", "AgentRuntime"),
    (
        "neuro_code.application.sessions.binding",
        "ConversationBinding",
    ),
    (
        "neuro_code.application.sessions.binding",
        "ConversationRunner",
    ),
    (
        "neuro_code.application.sessions.contracts",
        "InteractionModeSelectionResult",
    ),
    (
        "neuro_code.application.sessions.contracts",
        "ReasoningEffortSelectionResult",
    ),
    (
        "neuro_code.application.sessions.contracts",
        "SessionOption",
    ),
    (
        "neuro_code.application.sessions.contracts",
        "SessionSelectionResult",
    ),
    (
        "neuro_code.application.sessions.selection",
        "SessionSelectionController",
    ),
    (
        "neuro_code.application.sessions.selection",
        "SessionSelectionService",
    ),
    (
        "neuro_code.application.sessions.lifecycle",
        "DeleteSessionRequest",
    ),
    (
        "neuro_code.application.sessions.lifecycle",
        "ForkSessionRequest",
    ),
    (
        "neuro_code.application.sessions.lifecycle",
        "ImportSessionRequest",
    ),
    (
        "neuro_code.application.sessions.lifecycle",
        "RenameSessionRequest",
    ),
    (
        "neuro_code.application.sessions.lifecycle",
        "SessionLifecycleService",
    ),
    (
        "neuro_code.application.sessions.lifecycle",
        "SessionLifecycleController",
    ),
    (
        "neuro_code.application.sessions.lifecycle",
        "StartSessionRequest",
    ),
    (
        "neuro_code.application.sessions.profile_conversation",
        "ConversationBinding",
    ),
    (
        "neuro_code.application.sessions.profile_conversation",
        "ProfileConversationController",
    ),
    ("neuro_code.application.sessions.conversation", "AgentConversation"),
    (
        "neuro_code.application.sessions.terminal_sessions",
        "LocalInteractiveTerminalManager",
    ),
    (
        "neuro_code.application.sessions.terminal_sessions",
        "LocalInteractiveTerminalSession",
    ),
    ("neuro_code.application.sessions", "SessionApplicationService"),
    ("neuro_code.application.sessions", "SessionLifecycleController"),
    ("neuro_code.application.sessions", "SessionLifecycleService"),
    ("neuro_code.application.sessions", "SessionSelectionController"),
    ("neuro_code.application.sessions", "SessionSelectionService"),
    ("neuro_code.application.sessions.catalog", "SessionCatalogApplicationService"),
    ("neuro_code.application.sessions.catalog", "SessionInspection"),
    ("neuro_code.application.sessions.catalog", "ListSessionsRequest"),
    ("neuro_code.application.sessions.catalog", "ListSessionsPageRequest"),
    ("neuro_code.application.sessions.catalog", "SearchSessionsRequest"),
    ("neuro_code.application.sessions.catalog", "SessionSearchInspectionPage"),
    ("neuro_code.application.sessions", "ExportSessionRequest"),
    ("neuro_code.application.sessions", "SessionExport"),
    ("neuro_code.application.sessions", "SessionInspection"),
    ("neuro_code.application.sessions", "LoadSessionItemsRequest"),
    ("neuro_code.application.sessions", "SessionItemQueryController"),
    ("neuro_code.application.sessions", "SessionItemQueryService"),
    ("neuro_code.application.sessions.item_queries", "LoadSessionItemsRequest"),
    ("neuro_code.application.sessions.item_queries", "SessionItemQueryController"),
    ("neuro_code.application.sessions.item_queries", "SessionItemQueryService"),
    ("neuro_code.application.sessions", "LoadSessionEventsRequest"),
    ("neuro_code.application.sessions", "SessionEventQueryController"),
    ("neuro_code.application.sessions", "SessionEventQueryService"),
    ("neuro_code.application.sessions.event_queries", "LoadSessionEventsRequest"),
    ("neuro_code.application.sessions.event_queries", "SessionEventQueryController"),
    ("neuro_code.application.sessions.event_queries", "SessionEventQueryService"),
    ("neuro_code.application.sessions", "LoadExecutionRecordRequest"),
    ("neuro_code.application.sessions", "LoadExecutionRecordsRequest"),
    ("neuro_code.application.sessions", "SessionExecutionQueryController"),
    ("neuro_code.application.sessions", "SessionExecutionQueryService"),
    (
        "neuro_code.application.sessions.execution_queries",
        "LoadExecutionRecordRequest",
    ),
    (
        "neuro_code.application.sessions.execution_queries",
        "LoadExecutionRecordsRequest",
    ),
    (
        "neuro_code.application.sessions.execution_queries",
        "SessionExecutionQueryController",
    ),
    (
        "neuro_code.application.sessions.execution_queries",
        "SessionExecutionQueryService",
    ),
    ("neuro_code.application.sessions", "LoadSessionPlanRequest"),
    ("neuro_code.application.sessions", "ListPlanCommentsRequest"),
    ("neuro_code.application.sessions", "ListSessionTasksRequest"),
    ("neuro_code.application.sessions", "GetSessionTaskRequest"),
    ("neuro_code.application.sessions", "SessionTaskQueryController"),
    ("neuro_code.application.sessions", "SessionTaskQueryService"),
    ("neuro_code.application.sessions", "SessionSummaryQueryController"),
    ("neuro_code.application.sessions", "SessionSummaryQueryService"),
    ("neuro_code.application.sessions.summary", "GetSessionSummaryRequest"),
    ("neuro_code.application.sessions.summary", "SessionSummaryQueryController"),
    ("neuro_code.application.sessions.summary", "SessionSummaryQueryService"),
    (
        "neuro_code.application.sessions",
        "GetSubagentRelationshipRequest",
    ),
    (
        "neuro_code.application.sessions",
        "ListSubagentRelationshipsRequest",
    ),
    (
        "neuro_code.application.sessions",
        "MAX_SUBAGENT_RELATIONSHIP_LIMIT",
    ),
    (
        "neuro_code.application.sessions",
        "SubagentRelationshipAction",
    ),
    (
        "neuro_code.application.sessions",
        "SubagentRelationshipProjection",
    ),
    (
        "neuro_code.application.sessions",
        "SubagentRelationshipQueryController",
    ),
    (
        "neuro_code.application.sessions",
        "SubagentRelationshipQueryService",
    ),
    (
        "neuro_code.application.sessions.subagent_queries",
        "GetSubagentRelationshipRequest",
    ),
    (
        "neuro_code.application.sessions.subagent_queries",
        "ListSubagentRelationshipsRequest",
    ),
    (
        "neuro_code.application.sessions.subagent_queries",
        "MAX_SUBAGENT_RELATIONSHIP_LIMIT",
    ),
    (
        "neuro_code.application.sessions.subagent_queries",
        "SubagentRelationshipAction",
    ),
    (
        "neuro_code.application.sessions.subagent_queries",
        "SubagentRelationshipProjection",
    ),
    (
        "neuro_code.application.sessions.subagent_queries",
        "SubagentRelationshipQueryController",
    ),
    (
        "neuro_code.application.sessions.subagent_queries",
        "SubagentRelationshipQueryService",
    ),
    (
        "neuro_code.application.sessions.task_queries",
        "GetSessionTaskRequest",
    ),
    (
        "neuro_code.application.sessions.task_queries",
        "ListSessionTasksRequest",
    ),
    (
        "neuro_code.application.sessions.task_queries",
        "SessionTaskQueryController",
    ),
    (
        "neuro_code.application.sessions.task_queries",
        "SessionTaskQueryService",
    ),
    ("neuro_code.application.sessions", "SessionTurnService"),
    ("neuro_code.application.sessions.turns", "RunTurnRequest"),
    ("neuro_code.application.sessions.turns", "SessionTurnRunner"),
    ("neuro_code.application.sessions.turns", "SessionTurnService"),
    ("neuro_code.application.sessions", "DeleteSessionRequest"),
    ("neuro_code.application.sessions", "ForkSessionRequest"),
    ("neuro_code.application.sessions", "GetSessionSummaryRequest"),
    ("neuro_code.application.sessions", "ImportSessionRequest"),
    ("neuro_code.application.sessions", "ListSessionsPageRequest"),
    ("neuro_code.application.sessions", "ResumeSessionRequest"),
    ("neuro_code.application.sessions", "RunTurnRequest"),
    ("neuro_code.application.sessions", "SessionWorkspaceMatcher"),
    ("neuro_code.application.sessions", "StartSessionRequest"),
    ("neuro_code.application.providers", "ChangeProviderRequest"),
    ("neuro_code.application.providers.contracts", "ProviderOption"),
    ("neuro_code.application.providers.contracts", "ProviderSelectionResult"),
    ("neuro_code.application.providers", "ProviderChangeService"),
    ("neuro_code.application.providers", "ProviderProfileController"),
    (
        "neuro_code.application.memory.instruction_tracker",
        "InstructionTracker",
    ),
    ("neuro_code.application.memory.skill_tracker", "SkillTracker"),
    (
        "neuro_code.application.memory.compaction_service",
        "ContextCompactionApplicationService",
    ),
    (
        "neuro_code.application.memory.compaction_service",
        "ContextCompactionPersistenceResult",
    ),
    (
        "neuro_code.application.memory.compaction_service",
        "PersistContextCompactionRequest",
    ),
    (
        "neuro_code.application.memory.compaction_trigger",
        "ContextCompactionTriggerAssessment",
    ),
    (
        "neuro_code.application.memory.compaction_trigger",
        "ContextCompactionTriggerMode",
    ),
    (
        "neuro_code.application.memory.compaction_trigger",
        "ContextCompactionTriggerRequest",
    ),
    (
        "neuro_code.application.memory.compaction_trigger",
        "ContextCompactionTriggerResult",
    ),
    (
        "neuro_code.application.memory.compaction_trigger",
        "ContextCompactionTriggerService",
    ),
    (
        "neuro_code.application.memory.compaction_runtime",
        "ContextCompactionBoundaryDecision",
    ),
    (
        "neuro_code.application.memory.compaction_runtime",
        "ContextCompactionExecutionRecordPolicy",
    ),
    (
        "neuro_code.application.memory.compaction_runtime",
        "ContextCompactionRuntimeAssessment",
    ),
    (
        "neuro_code.application.memory.compaction_runtime",
        "ContextCompactionRuntimeBoundary",
    ),
    (
        "neuro_code.application.memory.compaction_runtime",
        "ContextCompactionRuntimeBudget",
    ),
    (
        "neuro_code.application.memory.compaction_runtime",
        "ContextCompactionRuntimeFailureHandling",
    ),
    (
        "neuro_code.application.memory.compaction_runtime",
        "ContextCompactionRuntimeFailureKind",
    ),
    (
        "neuro_code.application.memory.compaction_runtime",
        "ContextCompactionRuntimeFailureProjection",
    ),
    (
        "neuro_code.application.memory.compaction_runtime",
        "ContextCompactionRuntimeGate",
    ),
    (
        "neuro_code.application.memory.compaction_runtime",
        "ContextCompactionRuntimeRequest",
    ),
    (
        "neuro_code.application.memory.compaction_runtime",
        "ContextCompactionRuntimeResult",
    ),
    (
        "neuro_code.application.memory.compaction_runtime",
        "ContextCompactionSafePoint",
    ),
    (
        "neuro_code.application.memory.compaction_runtime",
        "ContextCompactionTimeoutError",
    ),
    (
        "neuro_code.application.memory.compaction_runtime",
        "classify_context_compaction_failure",
    ),
    (
        "neuro_code.application.memory.compaction",
        "CompactionContextUsage",
    ),
    (
        "neuro_code.application.memory.compaction",
        "ContextCompactionDecision",
    ),
    (
        "neuro_code.application.memory.compaction",
        "ContextCompactionPlan",
    ),
    (
        "neuro_code.application.memory.compaction",
        "ContextCompactionPlanner",
    ),
    (
        "neuro_code.application.memory.compaction",
        "ContextCompactionPolicy",
    ),
    (
        "neuro_code.application.memory.compaction",
        "ContextSummaryGenerationResult",
    ),
    (
        "neuro_code.application.memory.compaction",
        "ContextSummaryRequest",
    ),
    (
        "neuro_code.application.memory.compaction",
        "ContextSummaryInput",
    ),
    (
        "neuro_code.application.memory.compaction",
        "ContextSummaryInputBuilder",
    ),
    (
        "neuro_code.application.memory.compaction",
        "ContextSummaryItem",
    ),
    (
        "neuro_code.application.memory.compaction",
        "ContextSummarySourceKind",
    ),
    (
        "neuro_code.application.memory.compaction",
        "ProviderContextSummaryGenerator",
    ),
    (
        "neuro_code.application.memory.compaction",
        "ProviderContextWindow",
    ),
    (
        "neuro_code.application.memory.compaction",
        "CompactionResumeRebuilder",
    ),
    (
        "neuro_code.application.memory.compaction",
        "CompactionResumeResult",
    ),
    (
        "neuro_code.application.memory.compaction",
        "DurableCompactionItem",
    ),
    (
        "neuro_code.application.memory.compaction",
        "build_durable_compaction_item",
    ),
    ("neuro_code.application.workflows", "ExecutePlanRequest"),
    ("neuro_code.application.workflows", "PlanExecutionController"),
    ("neuro_code.application.workflows", "PlanExecutionService"),
    ("neuro_code.application.workflows", "PlanSchedulingController"),
    ("neuro_code.application.workflows", "PlanSchedulingService"),
    ("neuro_code.application.workflows", "QueuedPlanExecutionController"),
    ("neuro_code.application.workflows", "QueuedPlanExecutionService"),
    ("neuro_code.application.workflows", "RunSessionTaskRequest"),
    ("neuro_code.application.workflows", "RunSubagentRequest"),
    ("neuro_code.application.workflows", "ReadOnlySubagentApplicationService"),
    ("neuro_code.application.workflows", "MAX_SUBAGENT_RESULT_BYTES"),
    ("neuro_code.application.workflows", "SchedulePlanRequest"),
    ("neuro_code.application.workflows", "SubagentExecutionController"),
    ("neuro_code.application.workflows", "SubagentResultProjection"),
    ("neuro_code.application.workflows", "SubagentExecutionService"),
    ("neuro_code.application.workflows", "SubagentExecutor"),
    ("neuro_code.application.workflows", "SubagentExecutorFactory"),
    ("neuro_code.application.workflows", "SubagentRunResult"),
    ("neuro_code.application.workflows.subagent", "RunSubagentRequest"),
    ("neuro_code.application.workflows.subagent", "ReadOnlySubagentApplicationService"),
    ("neuro_code.application.workflows.subagent", "SubagentExecutionController"),
    ("neuro_code.application.workflows.subagent", "MAX_SUBAGENT_RESULT_BYTES"),
    ("neuro_code.application.workflows.subagent", "SubagentResultProjection"),
    ("neuro_code.application.workflows.subagent", "SubagentExecutionService"),
    ("neuro_code.application.workflows.subagent", "SubagentExecutor"),
    ("neuro_code.application.workflows.subagent", "SubagentExecutorFactory"),
    ("neuro_code.application.workflows.subagent", "SubagentRunResult"),
    ("neuro_code.application.permissions.broker", "ApprovalHandler"),
    ("neuro_code.application.permissions.broker", "SessionApprovalBroker"),
    ("neuro_code.application.permissions.service", "ApproveToolRequest"),
    ("neuro_code.application.permissions.service", "ToolApprovalService"),
    ("neuro_code.application.permissions.policy", "PermissionEffect"),
    ("neuro_code.application.permissions.policy", "PermissionMode"),
    ("neuro_code.application.permissions.policy", "PermissionRule"),
    ("neuro_code.application.permissions.policy", "PermissionDecision"),
    ("neuro_code.application.permissions.policy", "PermissionManager"),
    ("neuro_code.domain.permissions.bash_commands", "BashCommandAnalysis"),
    ("neuro_code.domain.permissions.bash_commands", "BashCommandSegment"),
    ("neuro_code.domain.permissions.bash_commands", "analyze_bash_command"),
    ("neuro_code.application.runtime.approval", "ApprovalHandler"),
    ("neuro_code.application.runtime.approval", "SessionApprovalBroker"),
    ("neuro_code.application.runtime.conversation", "AgentConversation"),
    (
        "neuro_code.application.runtime.instruction_tracker",
        "InstructionTracker",
    ),
    ("neuro_code.application.runtime.skill_tracker", "SkillTracker"),
    (
        "neuro_code.application.runtime.profile_conversation",
        "ConversationBinding",
    ),
    (
        "neuro_code.application.runtime.profile_conversation",
        "ProfileConversationController",
    ),
    (
        "neuro_code.application.runtime.terminal_sessions",
        "LocalInteractiveTerminalManager",
    ),
    (
        "neuro_code.application.runtime.terminal_sessions",
        "LocalInteractiveTerminalSession",
    ),
    (
        "neuro_code.application.runtime.background_task_reminders",
        "format_background_task_completion_reminder",
    ),
    ("neuro_code.application.settings", "ApplicationSettings"),
    ("neuro_code.domain.sessions.search", "SessionSearchHit"),
    ("neuro_code.domain.sessions.search", "SessionSearchPage"),
    ("neuro_code.domain.sessions.search", "fallback_session_title"),
    ("neuro_code.domain.sessions.search", "searchable_session_text"),
    (
        "neuro_code.domain.conversation.compaction",
        "COMPACTION_SOURCE_FINGERPRINT_BYTES",
    ),
    (
        "neuro_code.domain.conversation.compaction",
        "DurableCompactionItem",
    ),
    (
        "neuro_code.domain.conversation.compaction",
        "compute_compaction_source_fingerprint",
    ),
    ("neuro_code.domain.conversation.interaction_mode", "InteractionMode"),
    (
        "neuro_code.domain.conversation.interaction_mode",
        "interaction_mode_guidance",
    ),
    ("neuro_code.domain.conversation.reasoning", "ReasoningEffort"),
    ("neuro_code.domain.conversation.reasoning", "reasoning_guidance"),
    ("neuro_code.domain.workspace", "InstructionDiscoveryResult"),
    ("neuro_code.domain.workspace.instructions", "InstructionDiscoveryResult"),
    (
        "neuro_code.domain.background_tasks.models",
        "DEFAULT_BACKGROUND_WAKE_COOLDOWN_SECONDS",
    ),
    (
        "neuro_code.domain.background_tasks.models",
        "DEFAULT_BACKGROUND_WAKE_MAX_PER_SESSION",
    ),
    ("neuro_code.domain.background_tasks.models", "MAX_BACKGROUND_TASK_WAIT_IDS"),
    ("neuro_code.domain.background_tasks.models", "MAX_BACKGROUND_WAKE_COUNT"),
    ("neuro_code.domain.background_tasks.models", "MAX_BACKGROUND_WAKE_TASK_IDS"),
    ("neuro_code.domain.background_tasks.models", "BackgroundTaskKillOutcome"),
    ("neuro_code.domain.background_tasks.models", "BackgroundTaskKillResult"),
    ("neuro_code.domain.background_tasks.models", "BackgroundTaskSnapshot"),
    ("neuro_code.domain.background_tasks.models", "BackgroundTaskStatus"),
    ("neuro_code.domain.background_tasks.models", "BackgroundTaskWaitMode"),
    ("neuro_code.domain.background_tasks.models", "BackgroundTaskWaitResult"),
    ("neuro_code.domain.background_tasks.models", "BackgroundTaskWakePolicy"),
    ("neuro_code.domain.background_tasks.models", "BackgroundWakeDecision"),
    ("neuro_code.domain.background_tasks.models", "BackgroundWakeLimits"),
    ("neuro_code.domain.background_tasks.models", "BackgroundWakeState"),
    ("neuro_code.domain.sandbox.models", "SandboxProfile"),
    ("neuro_code.domain.terminal.models", "MAX_TERMINAL_DIMENSION"),
    ("neuro_code.domain.terminal.models", "MAX_TERMINAL_OUTPUT_BYTES"),
    ("neuro_code.domain.terminal.models", "MAX_TERMINAL_READ_BYTES"),
    ("neuro_code.domain.terminal.models", "MAX_TERMINAL_WRITE_BYTES"),
    ("neuro_code.domain.terminal.models", "TerminalOutputChunk"),
    ("neuro_code.domain.terminal.models", "TerminalSignal"),
    ("neuro_code.domain.terminal.models", "TerminalSize"),
    ("neuro_code.domain.workspace", "SkillInfo"),
    ("neuro_code.domain.workspace.skills", "SkillInfo"),
    ("neuro_code.configuration.app", "AppConfig"),
    ("neuro_code.configuration.app", "ProviderProfile"),
    ("neuro_code.configuration.app", "load_config"),
    ("neuro_code.configuration.app", "override_provider"),
    ("neuro_code.configuration.app", "override_sandbox"),
    ("neuro_code.configuration.app", "pin_resumed_sandbox"),
    ("neuro_code.configuration.app", "resolve_http_client_policy"),
    ("neuro_code.shared.async_utils", "run_blocking"),
    ("neuro_code.shared.errors", "ConfigurationError"),
    ("neuro_code.shared.redaction", "redact_sensitive_text"),
    ("neuro_code.shared.ui_language", "UiLanguage"),
    ("neuro_code.application.ports", "ToolCollection"),
    ("neuro_code.application.ports", "ProviderCatalogError"),
    ("neuro_code.application.ports", "ProviderCatalogResult"),
    ("neuro_code.application.ports", "ProviderConnectionSpec"),
    ("neuro_code.application.ports", "ManagedProviderProfile"),
    ("neuro_code.application.ports", "ManagedProviderSettings"),
    ("neuro_code.application.ports", "ManagedProxyPolicy"),
    ("neuro_code.application.ports", "ProviderSettingsStore"),
    ("neuro_code.application.ports.provider_catalog", "ProviderCatalog"),
    ("neuro_code.application.ports.provider_catalog", "ProviderCatalogError"),
    ("neuro_code.application.ports.provider_catalog", "ProviderCatalogResult"),
    ("neuro_code.application.ports.provider_catalog", "ProviderConnectionSpec"),
    ("neuro_code.application.ports.provider_settings", "ManagedProviderProfile"),
    ("neuro_code.application.ports.provider_settings", "ManagedProviderSettings"),
    ("neuro_code.application.ports.provider_settings", "ManagedProxyPolicy"),
    ("neuro_code.application.ports.provider_settings", "ProviderSettingsStore"),
    ("neuro_code.application.ports", "WorkspaceChangeObserver"),
    ("neuro_code.application.ports", "WorkspaceIdentity"),
    ("neuro_code.application.ports", "WorkspacePathResolver"),
    ("neuro_code.application.ports.tools", "ToolCollection"),
    ("neuro_code.application.ports.workspace", "WorkspaceIdentity"),
    ("neuro_code.application.ports.workspace", "WorkspacePathResolver"),
    ("neuro_code.application.ports.workspace_changes", "WorkspaceChangeCheckpoint"),
    ("neuro_code.application.ports.workspace_changes", "WorkspaceChangeReport"),
    ("neuro_code.bootstrap", "ApplicationComposition"),
    ("neuro_code.bootstrap.composition", "ApplicationComposition"),
    ("neuro_code.bootstrap.composition", "WorkspaceChangeObserverFactory"),
    ("neuro_code.bootstrap.entrypoints", "main"),
    ("neuro_code.cli", "build_parser"),
    ("neuro_code.infrastructure.providers", "create_provider"),
    ("neuro_code.infrastructure.providers", "create_routed_provider"),
    (
        "neuro_code.infrastructure.providers.openai_responses",
        "OpenAIResponsesProvider",
    ),
    ("neuro_code.tui", "NeuroCodeApp"),
)

_COMPATIBILITY_IDENTITY_EXPORTS = (
    (
        "neuro_code.application",
        "neuro_code.application.settings",
        ("ApplicationSettings",),
    ),
    (
        "neuro_code.application.ports",
        "neuro_code.application.ports.tools",
        ("ToolCollection",),
    ),
    (
        "neuro_code.application.ports",
        "neuro_code.application.ports.provider_settings",
        (
            "ManagedProviderProfile",
            "ManagedProviderSettings",
            "ManagedProxyPolicy",
            "ProviderSettingsStore",
        ),
    ),
    (
        "neuro_code.application.runtime.instruction_tracker",
        "neuro_code.application.memory.instruction_tracker",
        ("InstructionTracker",),
    ),
    (
        "neuro_code.application.runtime.skill_tracker",
        "neuro_code.application.memory.skill_tracker",
        ("SkillTracker",),
    ),
    (
        "neuro_code.application.runtime.conversation",
        "neuro_code.application.sessions.conversation",
        ("AgentConversation", "PLAN_EXECUTION_PROMPT"),
    ),
    (
        "neuro_code.application.runtime.profile_conversation",
        "neuro_code.application.sessions.binding",
        ("ConversationBinding", "ConversationRunner"),
    ),
    (
        "neuro_code.application.runtime.profile_conversation",
        "neuro_code.application.sessions.contracts",
        (
            "InteractionModeSelectionResult",
            "ReasoningEffortSelectionResult",
            "SessionOption",
            "SessionSelectionResult",
        ),
    ),
    (
        "neuro_code.application.runtime.profile_conversation",
        "neuro_code.application.sessions.profile_conversation",
        (
            "InteractionModeSelectionResult",
            "ProfileConversationController",
            "ProviderOption",
            "ProviderSelectionResult",
            "ReasoningEffortSelectionResult",
            "SessionOption",
            "SessionSelectionResult",
        ),
    ),
    (
        "neuro_code.application.runtime.terminal_sessions",
        "neuro_code.application.sessions.terminal_sessions",
        ("LocalInteractiveTerminalManager", "LocalInteractiveTerminalSession"),
    ),
)

# These are the remaining runtime compatibility facades. The quarantine is
# explicit so a future production import cannot silently make a facade an
# implementation dependency.
_LEGACY_FACADE_MODULES = frozenset(
    {
        "neuro_code.application.runtime.approval",
        "neuro_code.application.runtime.conversation",
        "neuro_code.application.runtime.instruction_tracker",
        "neuro_code.application.runtime.profile_conversation",
        "neuro_code.application.runtime.skill_tracker",
        "neuro_code.application.runtime.terminal_sessions",
    }
)


def test_key_compatibility_imports_remain_available() -> None:
    missing: list[str] = []
    for module_name, attribute_name in _EXPECTED_IMPORTS:
        module = importlib.import_module(module_name)
        if not hasattr(module, attribute_name):
            missing.append(f"{module_name}:{attribute_name}")
    assert not missing, "missing compatibility imports:\n" + "\n".join(
        f"  - {entry}" for entry in missing
    )


def test_removed_root_compatibility_modules_are_absent() -> None:
    removed = (
        "neuro_code.bash_commands",
        "neuro_code.config",
        "neuro_code.permissions",
        "neuro_code.workspace",
        "neuro_code.workspace_changes",
    )
    for module_name in removed:
        assert importlib.util.find_spec(module_name) is None, module_name


def test_removed_adapter_tool_and_flat_domain_facades_are_absent() -> None:
    for package_name in ("adapters", "tools"):
        package_root = _PACKAGE_ROOT / package_name
        assert not any(package_root.glob("*.py")), package_name

    domain_root = _PACKAGE_ROOT / "domain"
    removed_flat_files = (
        "background_tasks.py",
        "context_usage.py",
        "events.py",
        "instructions.py",
        "interaction_mode.py",
        "messages.py",
        "model_context.py",
        "model_events.py",
        "provider_catalog.py",
        "provider_settings.py",
        "reasoning.py",
        "sandbox.py",
        "session_search.py",
        "skills.py",
        "terminal.py",
        "ui_preferences.py",
    )
    for filename in removed_flat_files:
        assert not (domain_root / filename).exists(), filename

    for module_name in (
        "neuro_code.domain.context_usage",
        "neuro_code.domain.events",
        "neuro_code.domain.instructions",
        "neuro_code.domain.interaction_mode",
        "neuro_code.domain.messages",
        "neuro_code.domain.model_context",
        "neuro_code.domain.model_events",
        "neuro_code.domain.provider_catalog",
        "neuro_code.domain.provider_settings",
        "neuro_code.domain.reasoning",
        "neuro_code.domain.session_search",
        "neuro_code.domain.skills",
        "neuro_code.domain.ui_preferences",
    ):
        assert importlib.util.find_spec(module_name) is None, module_name

    # These names remain importable as canonical package aggregates; only their
    # conflicting flat .py facades were removed.
    for module_name in (
        "neuro_code.domain.background_tasks",
        "neuro_code.domain.sandbox",
        "neuro_code.domain.terminal",
    ):
        assert importlib.util.find_spec(module_name) is not None, module_name


def test_compatibility_exports_preserve_object_identity() -> None:
    mismatches: list[str] = []
    for old_module_name, canonical_module_name, export_names in _COMPATIBILITY_IDENTITY_EXPORTS:
        old_module = importlib.import_module(old_module_name)
        canonical_module = importlib.import_module(canonical_module_name)
        for export_name in export_names:
            if getattr(old_module, export_name) is not getattr(canonical_module, export_name):
                mismatches.append(f"{old_module_name}:{export_name}")
    assert not mismatches, "compatibility exports changed object identity:\n" + "\n".join(
        f"  - {entry}" for entry in mismatches
    )


def test_production_sources_use_canonical_conversation_imports() -> None:
    legacy_modules = frozenset(
        {
            "neuro_code.domain.context_usage",
            "neuro_code.domain.events",
            "neuro_code.domain.messages",
            "neuro_code.domain.model_context",
            "neuro_code.domain.model_events",
            "neuro_code.domain.interaction_mode",
            "neuro_code.domain.reasoning",
        }
    )
    source_root = _PROJECT_ROOT / "src" / "neuro_code"
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imported_module = node.module if isinstance(node, ast.ImportFrom) else None
            if isinstance(node, ast.Import):
                imported_modules = tuple(alias.name for alias in node.names)
            elif imported_module is not None:
                imported_modules = (imported_module,)
            else:
                continue
            assert not legacy_modules.intersection(imported_modules), path


def test_importing_canonical_session_profile_does_not_load_runtime_facade() -> None:
    script = """
import importlib
import sys

importlib.import_module("neuro_code.application.sessions.profile_conversation")
assert "neuro_code.application.runtime.profile_conversation" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_importing_canonical_session_terminal_does_not_load_runtime_facade() -> None:
    script = """
import importlib
import sys

importlib.import_module("neuro_code.application.sessions.terminal_sessions")
assert "neuro_code.application.runtime.terminal_sessions" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_importing_canonical_session_conversation_does_not_load_runtime_facade() -> None:
    script = """
import importlib
import sys

importlib.import_module("neuro_code.application.sessions.conversation")
assert "neuro_code.application.runtime.conversation" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_production_sources_use_canonical_session_profile_imports() -> None:
    legacy_module = "neuro_code.application.runtime.profile_conversation"
    source_root = _PROJECT_ROOT / "src" / "neuro_code"
    for path in source_root.rglob("*.py"):
        if path == source_root / "application" / "runtime" / "profile_conversation.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imported_module = node.module if isinstance(node, ast.ImportFrom) else None
            if isinstance(node, ast.Import):
                imported_modules = tuple(alias.name for alias in node.names)
            elif imported_module is not None:
                imported_modules = (imported_module,)
            else:
                continue
            assert legacy_module not in imported_modules, path


def test_production_sources_use_canonical_session_terminal_imports() -> None:
    legacy_module = "neuro_code.application.runtime.terminal_sessions"
    source_root = _PROJECT_ROOT / "src" / "neuro_code"
    for path in source_root.rglob("*.py"):
        if path == source_root / "application" / "runtime" / "terminal_sessions.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imported_module = node.module if isinstance(node, ast.ImportFrom) else None
            if isinstance(node, ast.Import):
                imported_modules = tuple(alias.name for alias in node.names)
            elif imported_module is not None:
                imported_modules = (imported_module,)
            else:
                continue
            assert legacy_module not in imported_modules, path


def test_production_sources_use_canonical_session_conversation_imports() -> None:
    legacy_module = "neuro_code.application.runtime.conversation"
    source_root = _PROJECT_ROOT / "src" / "neuro_code"
    for path in source_root.rglob("*.py"):
        if path == source_root / "application" / "runtime" / "conversation.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imported_module = node.module if isinstance(node, ast.ImportFrom) else None
            if isinstance(node, ast.Import):
                imported_modules = tuple(alias.name for alias in node.names)
            elif imported_module is not None:
                imported_modules = (imported_module,)
            else:
                continue
            assert legacy_module not in imported_modules, path


def test_importing_canonical_instruction_tracker_does_not_load_runtime_facade() -> None:
    script = """
import importlib
import sys

importlib.import_module("neuro_code.application.memory.instruction_tracker")
assert "neuro_code.application.runtime.instruction_tracker" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_importing_canonical_skill_tracker_does_not_load_runtime_facade() -> None:
    script = """
import importlib
import sys

importlib.import_module("neuro_code.application.memory.skill_tracker")
assert "neuro_code.application.runtime.skill_tracker" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_execution_package_preserves_public_identity_across_value_object_modules() -> None:
    aggregate = importlib.import_module("neuro_code.domain.execution")
    outcomes = importlib.import_module("neuro_code.domain.execution.outcomes")
    tasks = importlib.import_module("neuro_code.domain.execution.tasks")
    checkpoints = importlib.import_module("neuro_code.domain.execution.checkpoints")

    for name in (
        "AgentExecutionOutcome",
        "AgentExecutionStatus",
        "ProgressKind",
        "SupervisorDecision",
        "SupervisorDecisionKind",
        "SupervisorReasonCode",
    ):
        assert getattr(aggregate, name) is getattr(outcomes, name)
    for name in (
        "ExecutionBudget",
        "ExecutionCounters",
        "SupervisionThresholds",
        "ToolCallBudget",
        "ToolCallCount",
        "ToolInteractionFingerprint",
        "TurnCancellationPolicy",
        "TurnSource",
    ):
        assert getattr(aggregate, name) is getattr(tasks, name)
    for name in ("ExecutionSnapshot", "SessionExecutionRecord"):
        assert getattr(aggregate, name) is getattr(checkpoints, name)

    assert aggregate.AgentExecutionOutcome.__module__ == outcomes.__name__
    assert aggregate.ExecutionBudget.__module__ == tasks.__name__
    assert aggregate.SessionExecutionRecord.__module__ == checkpoints.__name__


def test_plans_package_preserves_public_identity_from_models_module() -> None:
    aggregate = importlib.import_module("neuro_code.domain.plans")
    models = importlib.import_module("neuro_code.domain.plans.models")

    for name in (
        "PlanComment",
        "PlanStep",
        "PlanStepStatus",
        "SessionPlan",
        "plan_from_update_arguments",
    ):
        assert getattr(aggregate, name) is getattr(models, name)
    for name in (
        "MAX_PLAN_COMMENTS",
        "MAX_PLAN_COMMENT_BYTES",
        "MAX_PLAN_COMMENT_ID_BYTES",
        "MAX_PLAN_EXPLANATION_BYTES",
        "MAX_PLAN_STEPS",
        "MAX_PLAN_STEP_BYTES",
    ):
        assert getattr(aggregate, name) == getattr(models, name)
    assert aggregate.SessionPlan.__module__ == models.__name__


def test_sessions_package_preserves_public_identity_from_models_module() -> None:
    aggregate = importlib.import_module("neuro_code.domain.sessions")
    models = importlib.import_module("neuro_code.domain.sessions.models")
    search = importlib.import_module("neuro_code.domain.sessions.search")

    for name in ("SessionSnapshot", "SessionSummary", "normalize_session_title"):
        assert getattr(aggregate, name) is getattr(models, name)
    assert aggregate.MAX_SESSION_TITLE_CHARS == models.MAX_SESSION_TITLE_CHARS
    assert aggregate.SessionSummary.__module__ == models.__name__
    assert aggregate.SessionSnapshot.__module__ == models.__name__
    for name in (
        "SessionSearchHit",
        "SessionSearchPage",
        "fallback_session_title",
        "searchable_session_text",
    ):
        assert getattr(aggregate, name) is getattr(search, name)
    assert aggregate.SessionSearchHit.__module__ == search.__name__


def test_tools_package_preserves_public_identity_from_models_module() -> None:
    aggregate = importlib.import_module("neuro_code.domain.tools")
    models = importlib.import_module("neuro_code.domain.tools.models")

    for name in ("ToolDefinition", "ToolResult"):
        assert getattr(aggregate, name) is getattr(models, name)
        assert getattr(aggregate, name).__module__ == models.__name__


def test_session_tasks_package_preserves_public_identity_from_models_module() -> None:
    aggregate = importlib.import_module("neuro_code.domain.session_tasks")
    models = importlib.import_module("neuro_code.domain.session_tasks.models")

    for name in ("SessionTask", "SessionTaskKind", "SessionTaskStatus"):
        assert getattr(aggregate, name) is getattr(models, name)
        assert getattr(aggregate, name).__module__ == models.__name__
    for name in ("MAX_QUEUED_SESSION_TASKS", "MAX_SESSION_TASK_ID_BYTES"):
        assert getattr(aggregate, name) == getattr(models, name)


def test_background_tasks_package_preserves_public_identity_from_models_module() -> None:
    aggregate = importlib.import_module("neuro_code.domain.background_tasks")
    models = importlib.import_module("neuro_code.domain.background_tasks.models")

    for name in (
        "BackgroundTaskKillOutcome",
        "BackgroundTaskKillResult",
        "BackgroundTaskSnapshot",
        "BackgroundTaskStatus",
        "BackgroundTaskWaitMode",
        "BackgroundTaskWaitResult",
        "BackgroundTaskWakePolicy",
        "BackgroundWakeDecision",
        "BackgroundWakeLimits",
        "BackgroundWakeState",
    ):
        assert getattr(aggregate, name) is getattr(models, name)
        assert getattr(aggregate, name).__module__ == models.__name__
    for name in (
        "DEFAULT_BACKGROUND_WAKE_COOLDOWN_SECONDS",
        "DEFAULT_BACKGROUND_WAKE_MAX_PER_SESSION",
        "MAX_BACKGROUND_TASK_WAIT_IDS",
        "MAX_BACKGROUND_WAKE_COUNT",
        "MAX_BACKGROUND_WAKE_TASK_IDS",
    ):
        assert getattr(aggregate, name) == getattr(models, name)


def test_terminal_package_preserves_public_identity_from_models_module() -> None:
    aggregate = importlib.import_module("neuro_code.domain.terminal")
    models = importlib.import_module("neuro_code.domain.terminal.models")

    for name in ("TerminalOutputChunk", "TerminalSignal", "TerminalSize"):
        assert getattr(aggregate, name) is getattr(models, name)
        assert getattr(aggregate, name).__module__ == models.__name__
    for name in (
        "MAX_TERMINAL_DIMENSION",
        "MAX_TERMINAL_OUTPUT_BYTES",
        "MAX_TERMINAL_READ_BYTES",
        "MAX_TERMINAL_WRITE_BYTES",
    ):
        assert getattr(aggregate, name) == getattr(models, name)


def test_sandbox_package_preserves_public_identity_from_models_module() -> None:
    aggregate = importlib.import_module("neuro_code.domain.sandbox")
    models = importlib.import_module("neuro_code.domain.sandbox.models")

    assert aggregate.SandboxProfile is models.SandboxProfile
    assert aggregate.SandboxProfile.__module__ == models.__name__


def test_production_code_uses_canonical_sandbox_owner() -> None:
    legacy_module = "neuro_code.domain.sandbox"
    source_root = _PROJECT_ROOT / "src" / "neuro_code"
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name for alias in node.names}
                assert legacy_module not in imported, path
            elif isinstance(node, ast.ImportFrom):
                assert node.module != legacy_module, path


def test_production_code_uses_canonical_terminal_owner() -> None:
    """Stage 5AH keeps terminal value-object consumers on the canonical owner.

    验证阶段 5AH 让终端值对象消费者统一依赖规范所有者.
    """

    legacy_module = "neuro_code.domain.terminal"
    source_root = _PROJECT_ROOT / "src" / "neuro_code"
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name for alias in node.names}
                assert legacy_module not in imported, path
            elif isinstance(node, ast.ImportFrom):
                assert node.module != legacy_module, path


def test_production_code_uses_canonical_background_tasks_owner() -> None:
    """Stage 5AJ keeps background-task value consumers on the models owner.

    验证阶段 5AJ 让后台任务值对象消费者统一依赖 models owner.
    """

    legacy_module = "neuro_code.domain.background_tasks"
    source_root = _PROJECT_ROOT / "src" / "neuro_code"
    facade = source_root / "domain" / "background_tasks.py"
    for path in source_root.rglob("*.py"):
        if path == facade:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name for alias in node.names}
                assert legacy_module not in imported, path
            elif isinstance(node, ast.ImportFrom):
                assert node.module != legacy_module, path


def test_production_code_uses_canonical_ui_language_owner() -> None:
    """Stage 5AK keeps the domain aggregate off the UI-language facade.

    验证阶段 5AK 让 domain aggregate 直接依赖 shared UI language owner.
    """

    legacy_module = "neuro_code.domain.ui_preferences"
    source_root = _PROJECT_ROOT / "src" / "neuro_code"
    facade = source_root / "domain" / "ui_preferences.py"
    for path in source_root.rglob("*.py"):
        if path == facade:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name for alias in node.names}
                assert legacy_module not in imported, path
            elif isinstance(node, ast.ImportFrom):
                assert node.module != legacy_module, path


def test_production_code_does_not_import_legacy_compatibility_facades() -> None:
    """Keep explicitly migrated facade paths out of production consumers.

    防止已明确迁移的兼容 facade 路径重新成为生产消费者依赖.
    """

    source_root = _PROJECT_ROOT / "src" / "neuro_code"
    facade_paths: set[Path] = set()
    for module_name in _LEGACY_FACADE_MODULES:
        relative = Path(*module_name.removeprefix("neuro_code.").split("."))
        facade_paths.add(source_root / relative.with_suffix(".py"))
        facade_paths.add(source_root / relative / "__init__.py")

    for path in source_root.rglob("*.py"):
        if path in facade_paths:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules = (node.module,)
            else:
                continue
            assert not _LEGACY_FACADE_MODULES.intersection(imported_modules), path


def test_importing_canonical_provider_modules_does_not_load_legacy_facades() -> None:
    script = """
import importlib
import sys

importlib.import_module("neuro_code.infrastructure.providers.anthropic")
importlib.import_module("neuro_code.infrastructure.providers.gemini")
importlib.import_module("neuro_code.infrastructure.providers.failover")
importlib.import_module("neuro_code.infrastructure.providers.openai_compatible")
importlib.import_module("neuro_code.infrastructure.providers.openai_responses")
importlib.import_module("neuro_code.infrastructure.providers.image_references")

assert "neuro_code.providers" not in sys.modules
assert not any(name.startswith("neuro_code.providers.") for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_importing_canonical_workspace_modules_does_not_load_legacy_facades() -> None:
    script = """
import importlib
import sys

importlib.import_module("neuro_code.infrastructure.workspace.paths")
importlib.import_module("neuro_code.infrastructure.workspace.changes")
importlib.import_module("neuro_code.infrastructure.workspace.instructions")
importlib.import_module("neuro_code.infrastructure.workspace.skills")

assert "neuro_code.workspace" not in sys.modules
assert "neuro_code.workspace_changes" not in sys.modules
assert "neuro_code.adapters.instruction_discovery" not in sys.modules
assert "neuro_code.adapters.skill_discovery" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_provider_infrastructure_aggregate_exports_are_lazy_and_identity_preserving() -> None:
    script = """
import importlib
import sys

package = importlib.import_module("neuro_code.infrastructure.providers")
assert "neuro_code.infrastructure.providers.provider_catalog" not in sys.modules
assert "neuro_code.infrastructure.providers.provider_settings" not in sys.modules
catalog = package.HttpProviderCatalog
assert catalog is importlib.import_module(
    "neuro_code.infrastructure.providers.provider_catalog"
).HttpProviderCatalog
assert "neuro_code.infrastructure.providers.provider_settings" not in sys.modules
settings = package.JsonProviderSettingsStore
assert settings is importlib.import_module(
    "neuro_code.infrastructure.providers.provider_settings"
).JsonProviderSettingsStore
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_filesystem_canonical_owner_includes_the_write_tool() -> None:
    """Stage 5G makes the filesystem tool module the single owner.

    验证阶段 5G 使文件系统工具模块成为唯一所有者."""
    canonical = importlib.import_module("neuro_code.infrastructure.tools.filesystem")

    assert canonical.SearchReplaceTool.__module__ == canonical.__name__


def test_importing_canonical_filesystem_tools_does_not_load_legacy_facade() -> None:
    script = """
import importlib
import sys

importlib.import_module("neuro_code.infrastructure.tools.filesystem")

assert "neuro_code.tools.filesystem" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_tool_registry_import_does_not_eagerly_load_tool_implementations() -> None:
    """Stage 2W registry seam stays side-effect-free.

    Importing the canonical registry must not load any concrete tool
    implementation; ``default_tool_registry`` imports them lazily when
    called.

    验证阶段 2W 的注册表接缝保持无副作用. 导入规范注册表不会提前加载具体工具实现.
    """
    script = """
import importlib
import sys

importlib.import_module("neuro_code.infrastructure.tools.registry")
for module_name in (
    "neuro_code.tools.bash",
    "neuro_code.infrastructure.tools.bash",
    "neuro_code.tools.background_tasks",
    "neuro_code.tools.client_terminal",
    "neuro_code.tools.filesystem",
    "neuro_code.infrastructure.tools.filesystem",
    "neuro_code.infrastructure.tools.plans",
    "neuro_code.infrastructure.tools.skills",
):
    assert module_name not in sys.modules, module_name
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_background_tasks_canonical_owner_includes_the_kill_tool() -> None:
    """Stage 5J makes the background task tools share one canonical owner.

    验证阶段 5J 使后台任务工具共享一个规范所有者."""
    canonical = importlib.import_module("neuro_code.infrastructure.tools.background_tasks")

    for name in ("KillTaskTool", "TaskOutputTool", "WaitTasksTool"):
        canonical_tool = getattr(canonical, name)
        assert canonical_tool.__module__ == canonical.__name__


def test_background_tools_import_does_not_load_other_tool_implementations() -> None:
    """Stage 5J keeps canonical background tool imports bounded.

    Importing the canonical read-only background task tools must not load bash,
    client terminal, the legacy write tool, registry, filesystem, plan, or
    skill implementations.

    验证阶段 5J 保持规范后台工具的导入范围有界,不会加载其他工具实现.
    """
    script = """
import importlib
import sys

importlib.import_module("neuro_code.infrastructure.tools.background_tasks")
for module_name in (
    "neuro_code.tools.bash",
    "neuro_code.tools.background_tasks",
    "neuro_code.tools.client_terminal",
    "neuro_code.infrastructure.tools.filesystem",
    "neuro_code.infrastructure.tools.plans",
    "neuro_code.infrastructure.tools.registry",
    "neuro_code.infrastructure.tools.skills",
):
    assert module_name not in sys.modules, module_name
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_client_terminal_canonical_owner_includes_session_tools() -> None:
    """Stage 5I makes the client-terminal module the single tool owner.

    验证阶段 5I 使客户端终端模块成为唯一工具所有者."""
    canonical = importlib.import_module("neuro_code.infrastructure.tools.client_terminal")

    for name in (
        "ClientTerminalKillTool",
        "ClientTerminalOutputTool",
        "ClientTerminalStartTool",
        "ClientTerminalTool",
        "ClientTerminalWaitTool",
    ):
        canonical_tool = getattr(canonical, name)
        assert canonical_tool.__module__ == canonical.__name__


def test_client_terminal_tools_import_does_not_load_other_tool_implementations() -> None:
    """Stage 5I keeps canonical client-terminal imports bounded.

    Importing canonical client terminal tools must not load bash, background
    tasks, filesystem, plan, skill, registry, or the legacy session tools.

    验证阶段 5I 保持规范客户端终端工具的导入范围有界,不会加载其他工具实现.
    """
    script = """
import importlib
import sys

importlib.import_module("neuro_code.infrastructure.tools.client_terminal")
for module_name in (
    "neuro_code.tools.bash",
    "neuro_code.tools.background_tasks",
    "neuro_code.tools.client_terminal",
    "neuro_code.infrastructure.tools.background_tasks",
    "neuro_code.infrastructure.tools.filesystem",
    "neuro_code.infrastructure.tools.plans",
    "neuro_code.infrastructure.tools.registry",
    "neuro_code.infrastructure.tools.skills",
):
    assert module_name not in sys.modules, module_name
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_sandbox_package_keeps_platform_modules_lazy() -> None:
    script = """
import importlib
import sys

importlib.import_module("neuro_code.infrastructure.sandbox.windows_process")
assert "neuro_code.infrastructure.sandbox.process_tree" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_sandbox_package_process_tree_export_is_lazy_and_identity_preserving() -> None:
    script = """
import importlib
import sys

package = importlib.import_module("neuro_code.infrastructure.sandbox")
assert "neuro_code.infrastructure.sandbox.process_tree" not in sys.modules
canonical = importlib.import_module("neuro_code.infrastructure.sandbox.process_tree")
assert package.ProcessTree is canonical.ProcessTree
assert package.ProcessTree.__module__ == canonical.__name__
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_sandbox_platform_modules_do_not_eagerly_load_process_tree() -> None:
    platform_modules = (
        "neuro_code.infrastructure.sandbox.posix_pty",
        "neuro_code.infrastructure.sandbox.windows_pty",
        "neuro_code.infrastructure.sandbox.sandbox",
        "neuro_code.infrastructure.sandbox.windows_process",
        "neuro_code.infrastructure.sandbox.windows_job",
        "neuro_code.infrastructure.sandbox.windows_job_process",
        "neuro_code.infrastructure.sandbox.windows_conpty",
    )
    for module_name in platform_modules:
        script = f"""
import importlib
import sys

importlib.import_module({module_name!r})
assert "neuro_code.infrastructure.sandbox.process_tree" not in sys.modules
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{module_name}: {result.stderr or result.stdout}"


def test_removed_configuration_compatibility_exports_are_not_public() -> None:
    canonical_app = importlib.import_module("neuro_code.configuration.app")
    canonical = importlib.import_module("neuro_code.configuration.managed_provider_settings")

    assert canonical.__all__ == ["load_managed_provider_settings"]
    assert canonical.load_managed_provider_settings.__module__ == canonical.__name__
    assert importlib.util.find_spec("neuro_code.config") is None
    assert canonical_app.AppConfig.__module__ == canonical_app.__name__
    assert canonical_app.ProviderProfile.__module__ == canonical_app.__name__


def test_importing_canonical_configuration_does_not_load_legacy_facade() -> None:
    script = """
import importlib
import sys

importlib.import_module("neuro_code.configuration.app")
assert "neuro_code.config" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_production_code_uses_canonical_configuration_owner() -> None:
    """Stage 5AI keeps production configuration consumers off the facade.

    验证阶段 5AI 让生产配置消费者脱离兼容 facade 并直接依赖规范 owner.
    """

    legacy_module = "neuro_code.config"
    source_root = _PROJECT_ROOT / "src" / "neuro_code"
    facade = source_root / "config.py"
    for path in source_root.rglob("*.py"):
        if path == facade:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name for alias in node.names}
                assert legacy_module not in imported, path
            elif isinstance(node, ast.ImportFrom):
                assert node.module != legacy_module, path


def test_permissions_expose_policy_without_approval_contract_reexports() -> None:
    policy = importlib.import_module("neuro_code.application.permissions.policy")
    contracts = importlib.import_module("neuro_code.application.permissions.contracts")
    approval_port = importlib.import_module("neuro_code.application.ports.approval")
    policy_names = (
        "PermissionDecision",
        "PermissionEffect",
        "PermissionManager",
        "PermissionMode",
        "PermissionRule",
        "PermissionRuleStore",
    )
    contract_names = (
        "PermissionApproval",
        "PermissionApprovalKind",
        "PermissionRequest",
        "build_permission_request",
    )

    assert policy.__all__ == list(policy_names)
    assert all(getattr(policy, name).__module__ == policy.__name__ for name in policy_names)
    assert contracts.__all__ == list(contract_names)
    assert importlib.util.find_spec("neuro_code.permissions") is None
    assert approval_port.PermissionApprover.__module__ == "neuro_code.application.ports.approval"

    assert contract_names


def test_source_and_tests_do_not_statically_import_removed_root_approval_contracts() -> None:
    contract_names = frozenset(
        {
            "PermissionApproval",
            "PermissionApprovalKind",
            "PermissionRequest",
            "build_permission_request",
        }
    )
    for root in (_PROJECT_ROOT / "src" / "neuro_code", _PROJECT_ROOT / "tests"):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.module != "neuro_code.permissions":
                    continue
                imported_names = {alias.name for alias in node.names}
                assert not imported_names & contract_names, path

    policy_path = _PROJECT_ROOT / "src" / "neuro_code" / "application" / "permissions" / "policy.py"
    policy_tree = ast.parse(policy_path.read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "neuro_code.application.permissions.contracts"
        for node in ast.walk(policy_tree)
    )


def test_importing_canonical_permission_policy_does_not_load_legacy_facade() -> None:
    script = """
import importlib
import sys

importlib.import_module("neuro_code.application.permissions.policy")
assert "neuro_code.permissions" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_importing_canonical_bash_command_analysis_does_not_load_legacy_facade() -> None:
    script = """
import importlib
import sys

importlib.import_module("neuro_code.domain.permissions.bash_commands")
assert "neuro_code.bash_commands" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_application_package_retains_settings_without_composition_facade() -> None:
    application = importlib.import_module("neuro_code.application")
    settings = application.ApplicationSettings
    canonical_settings = importlib.import_module("neuro_code.application.settings")
    assert settings is canonical_settings.ApplicationSettings
    assert settings.__module__ == "neuro_code.application.settings"
    assert application.__all__ == ["ApplicationSettings"]
    assert not any(
        hasattr(application, attribute)
        for attribute in (
            "ApplicationComposition",
            "BackgroundSupervisorFactory",
            "InstructionDiscoveryFactory",
            "ProcessSandboxEnforcer",
            "ProviderFactory",
            "SessionStoreFactory",
            "ShellSandboxFactory",
            "SkillDiscoveryFactory",
        )
    )
    assert Path(application.__file__).name == "__init__.py"
    assert not (_PROJECT_ROOT / "src" / "neuro_code" / "application.py").exists()
    assert importlib.import_module("neuro_code.application.ports")
    assert importlib.import_module("neuro_code.application.permissions")


def test_permissions_can_initialize_before_the_tui_module() -> None:
    script = """
import neuro_code.application.permissions
import neuro_code.tui
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_importing_application_ports_does_not_load_composition_or_infrastructure() -> None:
    script = """
import sys

import neuro_code.application.ports

disallowed = [
    name
    for name in sys.modules
    if name == "neuro_code.bootstrap"
    or name.startswith("neuro_code.bootstrap.")
    or name == "neuro_code.adapters"
    or name.startswith("neuro_code.adapters.")
    or name == "neuro_code.providers"
    or name.startswith("neuro_code.providers.")
]
assert not disallowed, disallowed
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_importing_application_does_not_load_bootstrap() -> None:
    script = """
import sys

import neuro_code.application

assert not [
    name
    for name in sys.modules
    if name == "neuro_code.bootstrap" or name.startswith("neuro_code.bootstrap.")
]
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_canonical_runtime_modules_are_independently_importable() -> None:
    canonical_modules = (
        "neuro_code.application.runtime.background_task_reminders",
        "neuro_code.application.runtime.agent",
        "neuro_code.application.runtime.agent_loop",
        "neuro_code.application.runtime.context_builder",
        "neuro_code.application.runtime.event_recorder",
        "neuro_code.application.runtime.model_step",
        "neuro_code.application.runtime.tool_pipeline",
        "neuro_code.application.memory.instruction_tracker",
        "neuro_code.application.memory.skill_tracker",
        "neuro_code.application.memory.compaction",
        "neuro_code.application.memory.compaction_service",
        "neuro_code.application.memory.compaction_runtime",
        "neuro_code.application.memory.compaction_trigger",
        "neuro_code.application.sessions.terminal_sessions",
        "neuro_code.application.sessions.conversation",
        "neuro_code.application.permissions.broker",
    )
    for module in canonical_modules:
        script = f"""
import importlib
import sys

importlib.import_module({module!r})
assert not [
    name
    for name in sys.modules
    if name == "neuro_code.runtime" or name.startswith("neuro_code.runtime.")
]
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr or result.stdout


def test_removed_runtime_package_cannot_be_imported() -> None:
    script = """
import importlib
import importlib.util
from pathlib import Path

assert importlib.util.find_spec("neuro_code.runtime") is None
try:
    importlib.import_module("neuro_code.runtime")
except ModuleNotFoundError as error:
    assert error.name == "neuro_code.runtime"
else:
    raise AssertionError("removed runtime package was importable")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_canonical_port_modules_are_independently_importable() -> None:
    canonical_modules = (
        "neuro_code.application.ports",
        "neuro_code.application.ports.approval",
        "neuro_code.application.ports.background_tasks",
        "neuro_code.application.ports.http",
        "neuro_code.application.ports.instructions",
        "neuro_code.application.ports.model",
        "neuro_code.application.ports.provider_catalog",
        "neuro_code.application.ports.provider_settings",
        "neuro_code.application.ports.sandbox",
        "neuro_code.application.ports.skills",
        "neuro_code.application.ports.storage",
        "neuro_code.application.ports.terminal",
        "neuro_code.application.ports.tools",
        "neuro_code.application.ports.ui_preferences",
        "neuro_code.application.ports.workspace",
        "neuro_code.application.ports.workspace_changes",
    )
    for module in canonical_modules:
        script = f"""
import importlib
import sys

importlib.import_module({module!r})
assert not [
    name
    for name in sys.modules
    if name == "neuro_code.ports" or name.startswith("neuro_code.ports.")
]
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr or result.stdout


def test_canonical_provider_catalog_port_does_not_load_domain_facade() -> None:
    script = """
import importlib
import sys

canonical = importlib.import_module("neuro_code.application.ports.provider_catalog")
assert "neuro_code.domain.provider_catalog" not in sys.modules
for name in (
    "ProviderCatalog",
    "ProviderCatalogError",
    "ProviderCatalogResult",
    "ProviderConnectionSpec",
):
    assert getattr(canonical, name).__module__ == canonical.__name__
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_canonical_provider_settings_port_does_not_load_domain_facade() -> None:
    script = """
import importlib
import sys

canonical = importlib.import_module("neuro_code.application.ports.provider_settings")
assert "neuro_code.domain.provider_settings" not in sys.modules
for name in (
    "ManagedProviderProfile",
    "ManagedProviderSettings",
    "ManagedProxyPolicy",
    "ProviderSettingsStore",
):
    assert getattr(canonical, name).__module__ == canonical.__name__
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_removed_ports_package_cannot_be_imported() -> None:
    script = """
import importlib
import importlib.util

assert importlib.util.find_spec("neuro_code.ports") is None
try:
    importlib.import_module("neuro_code.ports")
except ModuleNotFoundError as error:
    assert error.name == "neuro_code.ports"
else:
    raise AssertionError("removed ports package was importable")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_canonical_shared_modules_are_independently_importable() -> None:
    script = """
import importlib
import sys

errors = importlib.import_module("neuro_code.shared.errors")
async_utils = importlib.import_module("neuro_code.shared.async_utils")
redaction = importlib.import_module("neuro_code.shared.redaction")
ui_language = importlib.import_module("neuro_code.shared.ui_language")
assert all(
    error.__module__ == "neuro_code.shared.errors"
    for error in (
        errors.ConfigurationError,
        errors.NeuroCodeError,
        errors.PermissionDenied,
        errors.ProviderError,
        errors.SandboxError,
        errors.SessionError,
        errors.TerminalError,
        errors.ToolError,
    )
)
assert async_utils.run_blocking.__module__ == "neuro_code.shared.async_utils"
assert redaction.redact_sensitive_text.__module__ == "neuro_code.shared.redaction"
assert ui_language.UiLanguage.__module__ == "neuro_code.shared.ui_language"
assert "neuro_code.domain.ui_preferences" not in sys.modules
assert not [
    name
    for name in sys.modules
    if name in {"neuro_code.async_utils", "neuro_code.errors", "neuro_code.redaction"}
]
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_removed_shared_modules_cannot_be_imported() -> None:
    script = """
import importlib
import importlib.util

for module in ("neuro_code.async_utils", "neuro_code.errors", "neuro_code.redaction"):
    assert importlib.util.find_spec(module) is None
    try:
        importlib.import_module(module)
    except ModuleNotFoundError as error:
        assert error.name == module
    else:
        raise AssertionError(f"removed shared module was importable: {module}")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_responses_provider_uses_the_canonical_module_without_xai_wrapper() -> None:
    assert not (_PROJECT_ROOT / "src" / "neuro_code" / "providers" / "xai_responses.py").exists()
    script = """
import importlib
import importlib.util
from pathlib import Path

canonical = importlib.import_module("neuro_code.infrastructure.providers.openai_responses")

assert canonical.__all__ == ["OpenAIResponsesProvider"]
assert canonical.OpenAIResponsesProvider.__module__ == canonical.__name__
providers_root = Path.cwd() / "src" / "neuro_code" / "providers"
assert not any(providers_root.glob("*.py"))
assert not (providers_root / "xai_responses.py").exists()
try:
    importlib.import_module("neuro_code.providers.xai_responses")
except ModuleNotFoundError as error:
    assert error.name in {
        "neuro_code.providers",
        "neuro_code.providers.xai_responses",
    }
else:
    raise AssertionError("removed xAI Responses provider module was importable")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_source_and_tests_do_not_statically_import_removed_xai_responses_module() -> None:
    legacy_module = "neuro_code.providers.xai_responses"
    for root in (_PROJECT_ROOT / "src" / "neuro_code", _PROJECT_ROOT / "tests"):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                module = node.module if isinstance(node, ast.ImportFrom) else None
                if isinstance(node, ast.Import):
                    names = tuple(alias.name for alias in node.names)
                elif module is not None:
                    names = (module,)
                else:
                    continue
                if isinstance(node, ast.ImportFrom) and node.module == "neuro_code.providers":
                    names += tuple(
                        f"neuro_code.providers.{alias.name}"
                        for alias in node.names
                        if alias.name == "xai_responses"
                    )
                assert legacy_module not in names, path


def test_tests_do_not_statically_import_the_removed_runtime_package() -> None:
    for path in (_PROJECT_ROOT / "tests").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif module is not None:
                names = (module,)
            else:
                continue
            assert not any(
                name == "neuro_code.runtime" or name.startswith("neuro_code.runtime.")
                for name in names
            ), path


def test_tests_do_not_statically_import_the_removed_ports_package() -> None:
    for path in (_PROJECT_ROOT / "tests").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif module is not None:
                names = (module,)
            else:
                continue
            if isinstance(node, ast.ImportFrom) and node.module == "neuro_code":
                names += tuple("neuro_code.ports" for alias in node.names if alias.name == "ports")
        assert not any(
            name == "neuro_code.ports" or name.startswith("neuro_code.ports.") for name in names
        ), path


def test_tests_do_not_statically_import_the_removed_shared_modules() -> None:
    legacy_modules = frozenset(
        {
            "neuro_code.async_utils",
            "neuro_code.errors",
            "neuro_code.redaction",
        }
    )
    for path in (_PROJECT_ROOT / "tests").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            if isinstance(node, ast.Import):
                names = tuple(alias.name for alias in node.names)
            elif module is not None:
                names = (module,)
            else:
                continue
            if isinstance(node, ast.ImportFrom) and node.module == "neuro_code":
                names += tuple(
                    f"neuro_code.{alias.name}"
                    for alias in node.names
                    if f"neuro_code.{alias.name}" in legacy_modules
                )
            assert not set(names) & legacy_modules, path


def test_application_runtime_has_no_aggregate_api() -> None:
    application_runtime = importlib.import_module("neuro_code.application.runtime")
    aggregate_names = {
        "AgentConversation",
        "AgentRunResult",
        "AgentRuntime",
        "AgentLoopRunner",
        "ApprovalHandler",
        "ContextBuilder",
        "ConversationBinding",
        "TurnEventRecorder",
        "InstructionTracker",
        "LocalInteractiveTerminalManager",
        "LocalInteractiveTerminalSession",
        "ModelStepProcessor",
        "ModelStepResult",
        "ProfileConversationController",
        "SessionApprovalBroker",
        "SkillTracker",
        "ToolExecutor",
        "ToolObservationBuilder",
    }
    assert not aggregate_names & vars(application_runtime).keys()


def test_canonical_runtime_public_types_keep_module_paths_and_metadata() -> None:
    runtime_modules = {
        "neuro_code.application.runtime.agent": ("AgentRuntime",),
        "neuro_code.application.runtime.agent_loop": ("AgentLoopRunner", "AgentRunResult"),
        "neuro_code.application.runtime.context_builder": ("ContextBuilder",),
        "neuro_code.application.runtime.event_recorder": ("TurnEventRecorder",),
        "neuro_code.application.runtime.model_step": (
            "ModelStepProcessor",
            "ModelStepResult",
        ),
        "neuro_code.application.runtime.tool_pipeline": (
            "ToolExecutor",
            "ToolObservationBuilder",
        ),
        "neuro_code.application.sessions.conversation": ("AgentConversation",),
    }
    for module_name, type_names in runtime_modules.items():
        module = importlib.import_module(module_name)
        for type_name in type_names:
            assert getattr(module, type_name).__module__ == module_name

    permission_broker = importlib.import_module("neuro_code.application.permissions.broker")
    legacy_permission_broker = importlib.import_module("neuro_code.application.runtime.approval")
    assert permission_broker.__all__ == ["ApprovalHandler", "SessionApprovalBroker"]
    for name in permission_broker.__all__:
        assert getattr(legacy_permission_broker, name) is getattr(permission_broker, name)
    assert permission_broker.SessionApprovalBroker.__module__ == permission_broker.__name__

    memory = importlib.import_module("neuro_code.application.memory.instruction_tracker")
    assert memory.InstructionTracker.__module__ == memory.__name__
    memory_skills = importlib.import_module("neuro_code.application.memory.skill_tracker")
    assert memory_skills.SkillTracker.__module__ == memory_skills.__name__
    compaction = importlib.import_module("neuro_code.application.memory.compaction")
    for name in (
        "CompactionContextUsage",
        "ContextCompactionDecision",
        "ContextCompactionPlan",
        "ContextCompactionPlanner",
        "ContextCompactionPolicy",
        "ContextSummaryGenerationResult",
        "ContextSummaryInput",
        "ContextSummaryInputBuilder",
        "ContextSummaryItem",
        "ContextSummaryRequest",
        "ContextSummarySourceKind",
        "ProviderContextSummaryGenerator",
        "ProviderContextWindow",
    ):
        assert getattr(compaction, name).__module__ == compaction.__name__
    assert compaction.MAX_CONTEXT_SUMMARY_TOKENS == 4_096
    compaction_service = importlib.import_module("neuro_code.application.memory.compaction_service")
    for name in (
        "ContextCompactionApplicationService",
        "ContextCompactionPersistenceResult",
        "PersistContextCompactionRequest",
    ):
        assert getattr(compaction_service, name).__module__ == compaction_service.__name__

    compaction_trigger = importlib.import_module("neuro_code.application.memory.compaction_trigger")
    for name in (
        "ContextCompactionTriggerAssessment",
        "ContextCompactionTriggerMode",
        "ContextCompactionTriggerRequest",
        "ContextCompactionTriggerResult",
        "ContextCompactionTriggerService",
    ):
        assert getattr(compaction_trigger, name).__module__ == compaction_trigger.__name__

    compaction_runtime = importlib.import_module("neuro_code.application.memory.compaction_runtime")
    for name in (
        "ContextCompactionBoundaryDecision",
        "ContextCompactionExecutionRecordPolicy",
        "ContextCompactionRuntimeAssessment",
        "ContextCompactionRuntimeBoundary",
        "ContextCompactionRuntimeBudget",
        "ContextCompactionRuntimeFailureHandling",
        "ContextCompactionRuntimeFailureKind",
        "ContextCompactionRuntimeFailureProjection",
        "ContextCompactionRuntimeGate",
        "ContextCompactionRuntimeRequest",
        "ContextCompactionRuntimeResult",
        "ContextCompactionSafePoint",
        "ContextCompactionTimeoutError",
        "classify_context_compaction_failure",
    ):
        assert getattr(compaction_runtime, name).__module__ == compaction_runtime.__name__

    profile = importlib.import_module("neuro_code.application.sessions.profile_conversation")
    legacy_profile = importlib.import_module("neuro_code.application.runtime.profile_conversation")
    binding = importlib.import_module("neuro_code.application.sessions.binding")
    session_contracts = importlib.import_module("neuro_code.application.sessions.contracts")
    session_selection = importlib.import_module("neuro_code.application.sessions.selection")
    session_lifecycle = importlib.import_module("neuro_code.application.sessions.lifecycle")
    session_summary = importlib.import_module("neuro_code.application.sessions.summary")
    session_execution = importlib.import_module("neuro_code.application.sessions.execution_queries")
    session_events = importlib.import_module("neuro_code.application.sessions.event_queries")
    session_items = importlib.import_module("neuro_code.application.sessions.item_queries")
    session_task_queries = importlib.import_module("neuro_code.application.sessions.task_queries")
    provider_contracts = importlib.import_module("neuro_code.application.providers.contracts")
    providers = importlib.import_module("neuro_code.application.providers")
    assert profile.__all__ == [
        "ConversationBinding",
        "InteractionModeSelectionResult",
        "ProfileConversationController",
        "ProviderOption",
        "ProviderSelectionResult",
        "ReasoningEffortSelectionResult",
        "SessionOption",
        "SessionSelectionResult",
    ]
    assert binding.__all__ == ["ConversationBinding", "ConversationRunner"]
    for name in binding.__all__:
        assert getattr(binding, name).__module__ == binding.__name__
        assert getattr(legacy_profile, name) is getattr(binding, name)
    assert profile.ConversationBinding is binding.ConversationBinding
    assert profile.ConversationRunner is binding.ConversationRunner
    assert session_contracts.__all__ == [
        "InteractionModeSelectionResult",
        "ReasoningEffortSelectionResult",
        "SessionOption",
        "SessionSelectionResult",
    ]
    for name in session_contracts.__all__:
        assert getattr(session_contracts, name).__module__ == session_contracts.__name__
        assert getattr(profile, name) is getattr(session_contracts, name)
        assert getattr(legacy_profile, name) is getattr(session_contracts, name)
    assert session_selection.__all__ == [
        "SessionSelectionController",
        "SessionSelectionService",
    ]
    session_package = importlib.import_module("neuro_code.application.sessions")
    for name in session_selection.__all__:
        assert getattr(session_selection, name).__module__ == session_selection.__name__
        assert getattr(session_package, name) is getattr(session_selection, name)
    assert session_lifecycle.__all__ == [
        "DeleteSessionRequest",
        "ForkSessionRequest",
        "ImportSessionRequest",
        "RenameSessionRequest",
        "SessionLifecycleController",
        "SessionLifecycleService",
        "StartSessionRequest",
    ]
    legacy_service = importlib.import_module("neuro_code.application.sessions.service")
    for name in session_lifecycle.__all__:
        assert getattr(session_lifecycle, name).__module__ == session_lifecycle.__name__
        assert getattr(session_package, name) is getattr(session_lifecycle, name)
        assert getattr(legacy_service, name) is getattr(session_lifecycle, name)
    assert session_summary.__all__ == [
        "GetSessionSummaryRequest",
        "SessionSummaryQueryController",
        "SessionSummaryQueryService",
    ]
    for name in session_summary.__all__:
        assert getattr(session_summary, name).__module__ == session_summary.__name__
        assert getattr(session_package, name) is getattr(session_summary, name)
        assert getattr(legacy_service, name) is getattr(session_summary, name)
    assert session_execution.__all__ == [
        "LoadExecutionRecordRequest",
        "LoadExecutionRecordsRequest",
        "SessionExecutionQueryController",
        "SessionExecutionQueryService",
    ]
    for name in session_execution.__all__:
        assert getattr(session_execution, name).__module__ == session_execution.__name__
        assert getattr(session_package, name) is getattr(session_execution, name)
        assert getattr(legacy_service, name) is getattr(session_execution, name)
    assert session_events.__all__ == [
        "LoadSessionEventsRequest",
        "SessionEventQueryController",
        "SessionEventQueryService",
    ]
    for name in session_events.__all__:
        assert getattr(session_events, name).__module__ == session_events.__name__
        assert getattr(session_package, name) is getattr(session_events, name)
        assert getattr(legacy_service, name) is getattr(session_events, name)
    assert session_items.__all__ == [
        "LoadSessionItemsRequest",
        "SessionItemQueryController",
        "SessionItemQueryService",
    ]
    for name in session_items.__all__:
        assert getattr(session_items, name).__module__ == session_items.__name__
        assert getattr(session_package, name) is getattr(session_items, name)
        assert getattr(legacy_service, name) is getattr(session_items, name)
    assert session_task_queries.__all__ == [
        "GetSessionTaskRequest",
        "ListSessionTasksRequest",
        "SessionTaskQueryController",
        "SessionTaskQueryService",
    ]
    for name in session_task_queries.__all__:
        assert getattr(session_task_queries, name).__module__ == session_task_queries.__name__
        assert getattr(session_package, name) is getattr(session_task_queries, name)
        assert getattr(legacy_service, name) is getattr(session_task_queries, name)
    assert provider_contracts.__all__ == ["ProviderOption", "ProviderSelectionResult"]
    for name in provider_contracts.__all__:
        assert getattr(provider_contracts, name).__module__ == provider_contracts.__name__
        assert getattr(providers, name) is getattr(provider_contracts, name)
        assert getattr(profile, name) is getattr(provider_contracts, name)
        assert getattr(legacy_profile, name) is getattr(provider_contracts, name)
    binding_fields = dataclasses.fields(profile.ConversationBinding)
    assert tuple(field.name for field in binding_fields) == (
        "runner",
        "provider",
        "background_tasks",
    )
    assert profile.ConversationBinding.__dataclass_params__.frozen
    assert profile.ConversationBinding.__slots__ == (
        "runner",
        "provider",
        "background_tasks",
    )
    assert profile.ConversationBinding.__match_args__ == (
        "runner",
        "provider",
        "background_tasks",
    )
    assert not profile.ConversationRunner._is_runtime_protocol
    for name in profile.__all__:
        assert getattr(legacy_profile, name) is getattr(profile, name)
        if name not in {
            "ConversationBinding",
            "ProviderOption",
            "ProviderSelectionResult",
            "InteractionModeSelectionResult",
            "ReasoningEffortSelectionResult",
            "SessionOption",
            "SessionSelectionResult",
        }:
            assert getattr(profile, name).__module__ == profile.__name__

    terminal = importlib.import_module("neuro_code.application.sessions.terminal_sessions")
    legacy_terminal = importlib.import_module("neuro_code.application.runtime.terminal_sessions")
    assert terminal.__all__ == [
        "LocalInteractiveTerminalManager",
        "LocalInteractiveTerminalSession",
    ]
    for name in terminal.__all__:
        assert getattr(legacy_terminal, name) is getattr(terminal, name)
        assert getattr(terminal, name).__module__ == terminal.__name__
    signature = inspect.signature(terminal.LocalInteractiveTerminalManager)
    assert signature.parameters["workspace_path_resolver"].default is inspect.Parameter.empty
    assert signature.parameters["local_process_sandbox"].default is inspect.Parameter.empty

    conversation = importlib.import_module("neuro_code.application.sessions.conversation")
    legacy_conversation = importlib.import_module("neuro_code.application.runtime.conversation")
    assert conversation.__all__ == ["PLAN_EXECUTION_PROMPT", "AgentConversation"]
    for name in conversation.__all__:
        assert getattr(legacy_conversation, name) is getattr(conversation, name)
    assert conversation.AgentConversation.__module__ == conversation.__name__

    turns = importlib.import_module("neuro_code.application.sessions.turns")
    assert turns.__all__ == ["RunTurnRequest", "SessionTurnRunner", "SessionTurnService"]
    assert turns.RunTurnRequest.__module__ == turns.__name__
    assert turns.SessionTurnRunner.__module__ == turns.__name__
    assert turns.SessionTurnService.__module__ == turns.__name__
    session_package = importlib.import_module("neuro_code.application.sessions")
    for name in turns.__all__:
        assert getattr(session_package, name) is getattr(turns, name)
        assert getattr(legacy_service, name) is getattr(turns, name)


def test_importing_workspace_change_port_does_not_load_filesystem_implementation() -> None:
    script = """
import sys

import neuro_code.application.ports.workspace_changes

assert "neuro_code.workspace_changes" not in sys.modules
assert "neuro_code.infrastructure.workspace.changes" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_importing_workspace_identity_port_does_not_load_filesystem_implementation() -> None:
    script = """
import sys

import neuro_code.application.ports.workspace

assert "neuro_code.workspace" not in sys.modules
assert "neuro_code.infrastructure.workspace.paths" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_importing_canonical_terminal_manager_does_not_load_concrete_implementations() -> None:
    script = """
import sys

import neuro_code.application.sessions.terminal_sessions

disallowed = [
    name
    for name in sys.modules
    if name == "neuro_code.workspace"
    or name == "neuro_code.adapters.posix_pty"
    or name == "neuro_code.adapters.windows_pty"
]
assert not disallowed, disallowed
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_console_script_targets_remain_unchanged() -> None:
    pyproject = tomllib.loads((_PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["scripts"] == {
        "neuro": "neuro_code.bootstrap.entrypoints:main",
        "neuro-code": "neuro_code.bootstrap.entrypoints:main",
    }


def test_python_module_entrypoint_uses_the_canonical_bootstrap_launcher() -> None:
    path = _PROJECT_ROOT / "src" / "neuro_code" / "__main__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports_bootstrap_main = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "neuro_code.bootstrap.entrypoints"
        and any(alias.name == "main" for alias in node.names)
        for node in ast.walk(tree)
    )
    assert imports_bootstrap_main


def test_importing_cli_does_not_load_bootstrap_or_concrete_infrastructure() -> None:
    script = """
import sys

import neuro_code.cli as cli

assert not hasattr(cli, "main")

disallowed = [
    name
    for name in sys.modules
    if name == "neuro_code.bootstrap"
    or name.startswith("neuro_code.bootstrap.")
    or name == "neuro_code.adapters"
    or name.startswith("neuro_code.adapters.")
    or name == "neuro_code.providers"
    or name.startswith("neuro_code.providers.")
]
assert not disallowed, disallowed
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_importing_acp_does_not_load_bootstrap_or_selected_concrete_dependencies() -> None:
    script = """
import sys

import neuro_code.acp

disallowed = [
    name
    for name in sys.modules
    if name == "neuro_code.bootstrap"
    or name.startswith("neuro_code.bootstrap.")
    or name == "neuro_code.adapters.mcp_stdio"
    or name == "neuro_code.adapters.sqlite_session"
    or name == "neuro_code.providers"
    or name.startswith("neuro_code.providers.")
]
assert not disallowed, disallowed
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_acp_uses_only_its_narrow_application_service() -> None:
    path = _PROJECT_ROOT / "src" / "neuro_code" / "acp.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    serve_functions = [
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "serve_acp"
    ]
    assert len(serve_functions) == 1
    serve_function = serve_functions[0]
    assert len(serve_function.args.args) == 1
    assert ast.unparse(serve_function.args.args[0].annotation) == "AcpApplicationService"
    assert "neuro_code.bootstrap.composition" not in imports
    assert "neuro_code.bootstrap.entrypoints" not in imports
    assert "neuro_code.adapters.mcp_stdio" not in imports
    assert "neuro_code.workspace" not in imports
    assert "self._application" not in source
    assert "self._service.config" not in source
    assert "self._service.store" not in source
