"""DPAPI-backed persistence for the Windows sandbox installation record.

The file envelope contains only a format marker and an encrypted blob.  The
plaintext installation record is never written to disk.  DPAPI uses the local
machine scope so an administrator can perform setup once and an ordinary
session can later read the installation record without requiring elevation;
the file's ACL is still managed by the setup authority.

Windows 沙箱 installation record 的 DPAPI 持久化.

文件 envelope 只包含格式标记和加密 blob,不会把明文 installation record 写入磁盘.
DPAPI 使用本机 scope,使管理员可以完成一次 setup,普通 session 随后无需提权即可读取;
文件本身的 ACL 仍由 setup authority 管理.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import json
import os
import tempfile
from pathlib import Path
from typing import Protocol, cast

from neuro_code.shared.errors import SandboxError

_DPAPI_UI_FORBIDDEN = 0x00000001
_DPAPI_LOCAL_MACHINE = 0x00000004
_ENVELOPE_FORMAT = "neuro-code-windows-sandbox-dpapi-v1"


class WindowsDpapiError(SandboxError):
    """A DPAPI or encrypted-record persistence failure."""


class WindowsDpapiApi(Protocol):
    """Small injectable DPAPI surface used by the credential store."""

    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, protected: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class _CFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(self, *args: object) -> object: ...


def _load_function(
    library: object,
    name: str,
    argtypes: list[object],
    restype: object,
) -> _CFunction:
    function = cast(_CFunction, getattr(library, name))
    function.argtypes = argtypes
    function.restype = restype
    return function


class _NativeWindowsDpapiApi:  # pragma: no cover - exercised by Windows native CI
    """Lazy ctypes facade over CryptProtectData/CryptUnprotectData."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsDpapiError("DPAPI is available only on Windows")
        loader = getattr(ctypes, "WinDLL", None)
        get_last_error = getattr(ctypes, "get_last_error", None)
        if loader is None or get_last_error is None:  # pragma: no cover - defensive on Windows
            raise WindowsDpapiError("this Python runtime does not expose the Win32 ctypes API")
        crypt32 = cast(object, loader("crypt32.dll", use_last_error=True))
        kernel32 = cast(object, loader("kernel32.dll", use_last_error=True))
        self._get_last_error = cast(_CFunction, get_last_error)
        self._local_free = _load_function(
            kernel32,
            "LocalFree",
            [ctypes.c_void_p],
            ctypes.c_void_p,
        )
        blob_pointer = ctypes.POINTER(_DataBlob)
        self._protect = _load_function(
            crypt32,
            "CryptProtectData",
            [
                blob_pointer,
                ctypes.c_wchar_p,
                blob_pointer,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                blob_pointer,
            ],
            ctypes.c_int32,
        )
        self._unprotect = _load_function(
            crypt32,
            "CryptUnprotectData",
            [
                blob_pointer,
                ctypes.POINTER(ctypes.c_wchar_p),
                blob_pointer,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                blob_pointer,
            ],
            ctypes.c_int32,
        )

    def _error(self, operation: str) -> WindowsDpapiError:
        return WindowsDpapiError(
            f"{operation} failed with Windows error {cast(int, self._get_last_error())}"
        )

    @staticmethod
    def _input_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
        buffer = ctypes.create_string_buffer(data, max(1, len(data)))
        pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        return _DataBlob(len(data), pointer), buffer

    def _run(self, operation: str, function: _CFunction, data: bytes) -> bytes:
        input_blob, input_buffer = self._input_blob(data)
        _ = input_buffer  # Keep the backing buffer alive through the call.
        output_blob = _DataBlob()
        if operation == "CryptUnprotectData":
            description = ctypes.c_wchar_p()
            succeeded = function(
                ctypes.byref(input_blob),
                ctypes.byref(description),
                None,
                None,
                None,
                _DPAPI_UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
        else:
            succeeded = function(
                ctypes.byref(input_blob),
                None,
                None,
                None,
                None,
                _DPAPI_UI_FORBIDDEN | _DPAPI_LOCAL_MACHINE,
                ctypes.byref(output_blob),
            )
        if not succeeded or not output_blob.pbData:
            raise self._error(operation)
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            if self._local_free(output_blob.pbData):
                raise self._error("LocalFree(DPAPI blob)")

    def protect(self, plaintext: bytes) -> bytes:
        return self._run("CryptProtectData", self._protect, plaintext)

    def unprotect(self, protected: bytes) -> bytes:
        return self._run("CryptUnprotectData", self._unprotect, protected)


class WindowsDpapiCredentialStore:
    """Atomically persist one opaque DPAPI-protected installation payload."""

    def __init__(self, path: Path, *, api: WindowsDpapiApi | None = None) -> None:
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValueError("DPAPI credential-store path must be absolute")
        self._path = path
        self._api = _NativeWindowsDpapiApi() if api is None else api

    @property
    def path(self) -> Path:
        return self._path

    def save(self, plaintext: bytes) -> None:
        if not isinstance(plaintext, bytes) or not plaintext:
            raise ValueError("DPAPI credential-store payload must be non-empty bytes")
        protected = self._api.protect(plaintext)
        envelope = json.dumps(
            {
                "format": _ENVELOPE_FORMAT,
                "blob": base64.b64encode(protected).decode("ascii"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        parent = self._path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self._path.name}.",
                dir=parent,
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    chmod = getattr(os, "fchmod", None)
                    if chmod is not None:
                        chmod(stream.fileno(), 0o600)
                    stream.write(envelope)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, self._path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temporary_name)
                raise
        except OSError as error:
            raise WindowsDpapiError(
                "unable to atomically persist DPAPI credential record"
            ) from error

    def load(self) -> bytes | None:
        try:
            envelope = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise WindowsDpapiError("DPAPI credential record is unreadable") from error
        if not isinstance(envelope, dict) or envelope.get("format") != _ENVELOPE_FORMAT:
            raise WindowsDpapiError("DPAPI credential record format is unsupported")
        encoded = envelope.get("blob")
        if not isinstance(encoded, str):
            raise WindowsDpapiError("DPAPI credential record blob is invalid")
        try:
            protected = base64.b64decode(encoded.encode("ascii"), validate=True)
            return self._api.unprotect(protected)
        except (ValueError, UnicodeError, WindowsDpapiError) as error:
            if isinstance(error, WindowsDpapiError):
                raise
            raise WindowsDpapiError("DPAPI credential record blob is invalid") from error

    def clear(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            return
        except OSError as error:
            raise WindowsDpapiError("unable to remove DPAPI credential record") from error


__all__ = ["WindowsDpapiApi", "WindowsDpapiCredentialStore", "WindowsDpapiError"]
