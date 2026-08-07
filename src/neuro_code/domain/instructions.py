"""Compatibility facade for the canonical workspace instruction model.

提供工作区指令模型的兼容门面,并重新导出规范实现."""

from neuro_code.domain.workspace.instructions import (
    INSTRUCTION_FILENAME,
    MAX_DIRECTORY_DEPTH,
    MAX_INSTRUCTION_FILES,
    MAX_SINGLE_FILE_BYTES,
    MAX_TOTAL_BYTES,
    InstructionDiscoveryResult,
    InstructionFile,
    InstructionRejection,
    InstructionRejectionReason,
    compute_instruction_fingerprint,
    normalize_relative_path,
)

__all__ = [
    "INSTRUCTION_FILENAME",
    "MAX_DIRECTORY_DEPTH",
    "MAX_INSTRUCTION_FILES",
    "MAX_SINGLE_FILE_BYTES",
    "MAX_TOTAL_BYTES",
    "InstructionDiscoveryResult",
    "InstructionFile",
    "InstructionRejection",
    "InstructionRejectionReason",
    "compute_instruction_fingerprint",
    "normalize_relative_path",
]
