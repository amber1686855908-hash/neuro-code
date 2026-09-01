"""Application-owned LSP failure taxonomy and service helpers."""

from neuro_code.application.ports.lsp import LspError, LspFailureKind, LspFailurePhase

__all__ = ["LspError", "LspFailureKind", "LspFailurePhase"]
