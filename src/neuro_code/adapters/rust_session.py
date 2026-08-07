"""Compatibility facade for the canonical Rust session persistence importer.

提供 Rust 会话持久化导入器的兼容门面,并转发到规范实现."""

from neuro_code.infrastructure.persistence.rust_session import (
    MAX_CHAT_RECORD_BYTES,
    MAX_CHAT_RECORDS,
    MAX_SUMMARY_BYTES,
    RAW_OUTPUT_BACKEND_TYPES,
    RAW_OUTPUT_NON_CONTEXT_TYPES,
    UNKNOWN_CONTENT_PLACEHOLDER,
    UPSTREAM_IMPORT_PROVIDER,
    RustSessionImport,
    load_rust_session,
)

__all__ = [
    "MAX_CHAT_RECORDS",
    "MAX_CHAT_RECORD_BYTES",
    "MAX_SUMMARY_BYTES",
    "RAW_OUTPUT_BACKEND_TYPES",
    "RAW_OUTPUT_NON_CONTEXT_TYPES",
    "UNKNOWN_CONTENT_PLACEHOLDER",
    "UPSTREAM_IMPORT_PROVIDER",
    "RustSessionImport",
    "load_rust_session",
]
