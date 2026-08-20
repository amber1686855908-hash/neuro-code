"""Frozen, self-contained Python task corpus for P4.

The task seed is public to the agent once materialized.  Verifier markers and
the deterministic fake-provider actions remain harness data and are never
copied into the task workspace.
"""

from __future__ import annotations

from base64 import b64encode
from hashlib import sha256

from .models import TaskCategory, TaskSpec, VerifierSpec


def _base_files(extra: dict[str, str] | None = None) -> tuple[tuple[str, str], ...]:
    """Return a small package with enough unrelated code to require navigation."""

    catalog = "\n".join(
        [
            '"""Frozen catalog fixture; most entries are intentionally unrelated."""',
            "ITEMS = {",
            *[f'    "item-{index:03d}": {index},' for index in range(520)],
            "}",
            "",
        ]
    )
    files: dict[str, str] = {
        "pyproject.toml": (
            "[project]\nname = 'miniapp'\nversion = '0.1.0'\n"
            "requires-python = '>=3.12'\n\n"
            "[tool.pytest.ini_options]\npythonpath = ['src']\n"
        ),
        "README.md": "# Miniapp\n\nA frozen benchmark repository.\n",
        "src/miniapp/__init__.py": "from .core import add\n\n__all__ = ['add']\n",
        "src/miniapp/core.py": "def add(left: int, right: int) -> int:\n    return left + right\n",
        "src/miniapp/catalog.py": catalog,
        "tests/test_public.py": (
            "from miniapp.core import add\n\n\n"
            "def test_add_is_stable() -> None:\n    assert add(2, 3) == 5\n"
        ),
    }
    if extra:
        files.update(extra)
    return tuple(sorted(files.items()))


def _encoded(value: str) -> str:
    return b64encode(value.encode("utf-8")).decode("ascii")


def _python_command(script: str) -> str:
    """Return a shell command that works under both POSIX shells and cmd.exe."""

    return f'python -c "{script}"'


def _replace(path: str, old: str, new: str) -> str:
    return _python_command(
        "import base64;from pathlib import Path;"
        f"p=Path(base64.b64decode('{_encoded(path)}').decode());"
        "t=p.read_text();"
        f"o=base64.b64decode('{_encoded(old)}').decode();"
        f"n=base64.b64decode('{_encoded(new)}').decode();"
        "assert o in t,'expected source fragment was not found';"
        "p.write_text(t.replace(o,n,1))"
    )


def _replace_all(path: str, old: str, new: str) -> str:
    return _python_command(
        "import base64;from pathlib import Path;"
        f"p=Path(base64.b64decode('{_encoded(path)}').decode());"
        "t=p.read_text();"
        f"o=base64.b64decode('{_encoded(old)}').decode();"
        f"n=base64.b64decode('{_encoded(new)}').decode();"
        "p.write_text(t.replace(o,n))"
    )


def _append(path: str, content: str) -> str:
    return _python_command(
        "import base64;from pathlib import Path;"
        f"p=Path(base64.b64decode('{_encoded(path)}').decode());"
        f"p.write_text(p.read_text()+base64.b64decode('{_encoded(content)}').decode())"
    )


def _write(path: str, content: str) -> str:
    return _python_command(
        "import base64;from pathlib import Path;"
        f"p=Path(base64.b64decode('{_encoded(path)}').decode());"
        "p.parent.mkdir(parents=True,exist_ok=True);"
        f"p.write_text(base64.b64decode('{_encoded(content)}').decode())"
    )


def _reference(url: str, content: str) -> tuple[str, str]:
    return url, sha256(content.encode("utf-8")).hexdigest()


def _task(
    task_id: str,
    category: TaskCategory,
    prompt: str,
    extra: dict[str, str],
    markers: tuple[tuple[str, tuple[str, ...]], ...],
    commands: tuple[str, ...],
    *,
    forbidden: tuple[tuple[str, tuple[str, ...]], ...] = (),
    required_files: tuple[str, ...] = (),
    forbidden_files: tuple[str, ...] = (),
    public_tests: tuple[str, ...] = (),
    web: bool = False,
    external_dependency: str | None = None,
    external_reference_sha256: str | None = None,
) -> TaskSpec:
    return TaskSpec(
        task_id,
        category,
        prompt,
        _base_files(extra),
        VerifierSpec(
            required_markers=markers,
            forbidden_markers=forbidden,
            required_files=required_files,
            forbidden_files=forbidden_files,
        ),
        commands,
        public_tests,
        web,
        external_dependency,
        external_reference_sha256,
        required_files,
        forbidden_files,
    )


_H1_DOC = """# Vendor pagination reference

The stable vendor API uses `limit` and `cursor`. A missing cursor is omitted,
not sent as an empty string. The response field is `next_cursor`.
"""
_H2_DOC = """# Vendor status reference

The endpoint returns HTTP 204 for a successful empty response. A client must
not treat 204 as an error or attempt to decode a JSON body.
"""
_H3_DOC = """# Vendor timestamp reference

Timestamps are RFC 3339 UTC values with a trailing `Z`, for example
`2026-01-02T03:04:05Z`.
"""
_H4_DOC = """# Vendor authentication reference

Requests use the exact `Authorization: Bearer <token>` header. The token is
never placed in a query parameter.
"""
_H5_DOC = """# Vendor rate-limit reference

HTTP 429 responses include `Retry-After` in seconds. The client should expose
that bounded delay to its caller instead of retrying forever.
"""

_H1_URL, _H1_SHA = _reference(
    "https://cloud.google.com/apis/design/design_patterns#list_pagination", _H1_DOC
)
_H2_URL, _H2_SHA = _reference(
    "https://www.rfc-editor.org/rfc/rfc9110.html#name-204-no-content", _H2_DOC
)
_H3_URL, _H3_SHA = _reference("https://www.rfc-editor.org/rfc/rfc3339", _H3_DOC)
_H4_URL, _H4_SHA = _reference(
    "https://www.rfc-editor.org/rfc/rfc9110.html#name-authorization", _H4_DOC
)
_H5_URL, _H5_SHA = _reference(
    "https://www.rfc-editor.org/rfc/rfc9110.html#name-retry-after", _H5_DOC
)


TASKS: tuple[TaskSpec, ...] = (
    # A — Repository navigation
    _task(
        "A01-repository-lookup",
        TaskCategory.REPOSITORY_NAVIGATION,
        "Find the catalog lookup module and implement lookup(items, key) so it returns the item with the matching id or None.",
        {
            "src/miniapp/lookup.py": "def lookup(items: list[dict[str, object]], key: str) -> dict[str, object] | None:\n    return None\n"
        },
        (
            (
                "src/miniapp/lookup.py",
                ('return next((item for item in items if item.get("id") == key), None)',),
            ),
        ),
        (
            _replace(
                "src/miniapp/lookup.py",
                "    return None",
                '    return next((item for item in items if item.get("id") == key), None)',
            ),
        ),
    ),
    _task(
        "A02-repository-export",
        TaskCategory.REPOSITORY_NAVIGATION,
        "Locate the package export surface and make summarize publicly importable without changing parse.",
        {
            "src/miniapp/export_surface.py": 'def parse(value: str) -> str:\n    return value.strip()\n\ndef summarize(value: str) -> str:\n    return value[:10]\n\n__all__ = ["parse"]\n'
        },
        (("src/miniapp/export_surface.py", ('__all__ = ["parse", "summarize"]',)),),
        (
            _replace(
                "src/miniapp/export_surface.py",
                '__all__ = ["parse"]',
                '__all__ = ["parse", "summarize"]',
            ),
        ),
    ),
    _task(
        "A03-repository-path",
        TaskCategory.REPOSITORY_NAVIGATION,
        "Find the path helper and make cache_path return a cache directory Path joined with the supplied name.",
        {
            "src/miniapp/paths.py": "from pathlib import Path\n\ndef cache_path(name: str) -> Path:\n    return Path(name)\n"
        },
        (("src/miniapp/paths.py", ('return Path("cache") / name',)),),
        (
            _replace(
                "src/miniapp/paths.py", "    return Path(name)", '    return Path("cache") / name'
            ),
        ),
    ),
    _task(
        "A04-repository-metadata",
        TaskCategory.REPOSITORY_NAVIGATION,
        "Locate the metadata module and expose the package name constant as miniapp.",
        {"src/miniapp/metadata.py": 'PACKAGE_NAME = "unknown"\n'},
        (("src/miniapp/metadata.py", ('PACKAGE_NAME = "miniapp"',)),),
        (
            _replace(
                "src/miniapp/metadata.py", 'PACKAGE_NAME = "unknown"', 'PACKAGE_NAME = "miniapp"'
            ),
        ),
    ),
    _task(
        "A05-repository-documentation",
        TaskCategory.REPOSITORY_NAVIGATION,
        "Find the project README and add a short verification section telling users to run pytest.",
        {},
        (("README.md", ("## Verification", "pytest -q")),),
        (
            _append(
                "README.md", "\n## Verification\n\nRun `pytest -q` before submitting changes.\n"
            ),
        ),
        public_tests=("README.md",),
    ),
    # B — Localized editing
    _task(
        "B01-localized-clamp",
        TaskCategory.LOCALIZED_EDITING,
        "Fix clamp so values below low become low and values above high become high.",
        {
            "src/miniapp/math_utils.py": "def clamp(value: int, low: int, high: int) -> int:\n    return min(low, max(high, value))\n"
        },
        (("src/miniapp/math_utils.py", ("return max(low, min(high, value))",)),),
        (
            _replace(
                "src/miniapp/math_utils.py",
                "    return min(low, max(high, value))",
                "    return max(low, min(high, value))",
            ),
        ),
    ),
    _task(
        "B02-localized-normalize",
        TaskCategory.LOCALIZED_EDITING,
        "Make name normalization robust for Unicode case conversion while retaining whitespace trimming.",
        {
            "src/miniapp/normalization.py": "def normalize_name(value: str) -> str:\n    return value.strip().lower()\n"
        },
        (("src/miniapp/normalization.py", ("strip().casefold()",)),),
        (
            _replace(
                "src/miniapp/normalization.py", "value.strip().lower()", "value.strip().casefold()"
            ),
        ),
    ),
    _task(
        "B03-localized-pagination",
        TaskCategory.LOCALIZED_EDITING,
        "Correct the page slice so the requested page_size elements are included when available.",
        {
            "src/miniapp/pagination.py": "def page(items: list[int], start: int, page_size: int) -> list[int]:\n    return items[start : start + page_size - 1]\n"
        },
        (("src/miniapp/pagination.py", ("items[start : start + page_size]",)),),
        (
            _replace(
                "src/miniapp/pagination.py",
                "items[start : start + page_size - 1]",
                "items[start : start + page_size]",
            ),
        ),
    ),
    _task(
        "B04-localized-safe-get",
        TaskCategory.LOCALIZED_EDITING,
        "Make safe_get return the provided default for a missing mapping key instead of raising KeyError.",
        {
            "src/miniapp/json_utils.py": "def safe_get(data: dict[str, object], key: str, default: object = None) -> object:\n    return data[key]\n"
        },
        (("src/miniapp/json_utils.py", ("return data.get(key, default)",)),),
        (
            _replace(
                "src/miniapp/json_utils.py",
                "    return data[key]",
                "    return data.get(key, default)",
            ),
        ),
    ),
    _task(
        "B05-localized-format",
        TaskCategory.LOCALIZED_EDITING,
        "Change price formatting to preserve two decimal places.",
        {
            "src/miniapp/formatting.py": 'def format_price(value: float) -> str:\n    return f"{value:.1f}"\n'
        },
        (("src/miniapp/formatting.py", ('f"{value:.2f}"',)),),
        (_replace("src/miniapp/formatting.py", 'f"{value:.1f}"', 'f"{value:.2f}"'),),
    ),
    # C — Multi-file change
    _task(
        "C01-multi-file-timeout",
        TaskCategory.MULTI_FILE_CHANGE,
        "Add a timeout_seconds configuration field with a 10 second default and make request return it.",
        {
            "src/miniapp/config.py": "from dataclasses import dataclass\n\n@dataclass\nclass Config:\n    retries: int = 2\n",
            "src/miniapp/service.py": "from .config import Config\n\ndef request(config: Config) -> int:\n    return config.retries\n",
        },
        (
            ("src/miniapp/config.py", ("timeout_seconds: float = 10.0",)),
            ("src/miniapp/service.py", ("return config.timeout_seconds",)),
        ),
        (
            _replace(
                "src/miniapp/config.py",
                "    retries: int = 2",
                "    retries: int = 2\n    timeout_seconds: float = 10.0",
            ),
            _replace(
                "src/miniapp/service.py",
                "    return config.retries",
                "    return config.timeout_seconds",
            ),
        ),
    ),
    _task(
        "C02-multi-file-repository",
        TaskCategory.MULTI_FILE_CHANGE,
        "Introduce a Repository class with a list_names method and export it from miniapp.",
        {
            "src/miniapp/repository.py": "class Repository:\n    def __init__(self, names: list[str]) -> None:\n        self.names = names\n\n    def list_names(self) -> list[str]:\n        return list(self.names)\n"
        },
        (
            ("src/miniapp/repository.py", ("class Repository:",)),
            ("src/miniapp/__init__.py", ("from .repository import Repository",)),
        ),
        (_append("src/miniapp/__init__.py", "\nfrom .repository import Repository\n"),),
    ),
    _task(
        "C03-multi-file-serializer",
        TaskCategory.MULTI_FILE_CHANGE,
        "Add a JSON serializer and wire the API helper to use it for payloads.",
        {
            "src/miniapp/serializer.py": "def serialize(payload: dict[str, object]) -> str:\n    raise NotImplementedError\n",
            "src/miniapp/api.py": 'def build_payload(name: str) -> dict[str, object]:\n    return {"name": name}\n',
        },
        (
            ("src/miniapp/serializer.py", ("json.dumps(payload, sort_keys=True)",)),
            (
                "src/miniapp/api.py",
                ("from .serializer import serialize", "return serialize(build_payload(name))"),
            ),
        ),
        (
            _replace(
                "src/miniapp/serializer.py",
                "def serialize(payload: dict[str, object]) -> str:\n    raise NotImplementedError",
                "import json\n\ndef serialize(payload: dict[str, object]) -> str:\n    return json.dumps(payload, sort_keys=True)",
            ),
            _append(
                "src/miniapp/api.py",
                "\n\ndef encoded_payload(name: str) -> str:\n    from .serializer import serialize\n    return serialize(build_payload(name))\n",
            ),
        ),
    ),
    _task(
        "C04-multi-file-cli",
        TaskCategory.MULTI_FILE_CHANGE,
        "Add a status command function in cli.py and document its invocation in the README.",
        {"src/miniapp/cli.py": 'def main() -> str:\n    return "ok"\n'},
        (("src/miniapp/cli.py", ("def status_command()",)), ("README.md", ("miniapp status",))),
        (
            _append(
                "src/miniapp/cli.py", '\n\ndef status_command() -> str:\n    return "status:ok"\n'
            ),
            _append("README.md", "\nUse `miniapp status` to inspect the service.\n"),
        ),
    ),
    _task(
        "C05-multi-file-plugin",
        TaskCategory.MULTI_FILE_CHANGE,
        "Create a plugin registry and register the default audit plugin from defaults.py.",
        {
            "src/miniapp/plugins/registry.py": "class Registry:\n    def __init__(self) -> None:\n        self.plugins: list[str] = []\n\n    def register(self, name: str) -> None:\n        self.plugins.append(name)\n",
            "src/miniapp/plugins/defaults.py": "def names() -> list[str]:\n    return []\n",
        },
        (
            ("src/miniapp/plugins/defaults.py", ('registry.register("audit")',)),
            ("src/miniapp/plugins/defaults.py", ("return registry.plugins",)),
        ),
        (
            _replace(
                "src/miniapp/plugins/defaults.py",
                "def names() -> list[str]:\n    return []",
                'def names() -> list[str]:\n    from .registry import Registry\n    registry = Registry()\n    registry.register("audit")\n    return registry.plugins',
            ),
        ),
    ),
    # D — Bug diagnosis
    _task(
        "D01-bug-mutable-default",
        TaskCategory.BUG_DIAGNOSIS,
        "Diagnose why alerts leak between calls and remove the mutable default argument.",
        {
            "src/miniapp/alerts.py": "def collect(message: str, values: list[str] = []) -> list[str]:\n    values.append(message)\n    return values\n"
        },
        (("src/miniapp/alerts.py", ("values: list[str] | None = None", "if values is None:")),),
        (
            _replace(
                "src/miniapp/alerts.py",
                "def collect(message: str, values: list[str] = []) -> list[str]:\n    values.append(message)",
                "def collect(message: str, values: list[str] | None = None) -> list[str]:\n    if values is None:\n        values = []\n    values.append(message)",
            ),
        ),
    ),
    _task(
        "D02-bug-window-boundary",
        TaskCategory.BUG_DIAGNOSIS,
        "Find the boundary error in window_values and include the right endpoint only when it belongs to the window.",
        {
            "src/miniapp/window.py": "def window_values(values: list[int], start: int, end: int) -> list[int]:\n    return [value for value in values if start < value < end]\n"
        },
        (("src/miniapp/window.py", ("start <= value < end",)),),
        (_replace("src/miniapp/window.py", "start < value < end", "start <= value < end"),),
    ),
    _task(
        "D03-bug-loader-errors",
        TaskCategory.BUG_DIAGNOSIS,
        "Stop hiding malformed records in load_records; raise ValueError with the original error chained.",
        {
            "src/miniapp/loader.py": "def load_records(lines: list[str]) -> list[int]:\n    try:\n        return [int(line) for line in lines]\n    except ValueError:\n        return []\n"
        },
        (("src/miniapp/loader.py", ("raise ValueError", "from error")),),
        (
            _replace(
                "src/miniapp/loader.py",
                "    except ValueError:\n        return []",
                '    except ValueError as error:\n        raise ValueError("invalid record") from error',
            ),
        ),
    ),
    _task(
        "D04-bug-clock-zone",
        TaskCategory.BUG_DIAGNOSIS,
        "Fix the clock helper so it returns an aware UTC datetime.",
        {
            "src/miniapp/clock.py": "from datetime import datetime\n\ndef now_utc() -> datetime:\n    return datetime.now()\n"
        },
        (
            (
                "src/miniapp/clock.py",
                ("from datetime import datetime, timezone", "datetime.now(timezone.utc)"),
            ),
        ),
        (
            _replace(
                "src/miniapp/clock.py",
                "from datetime import datetime",
                "from datetime import datetime, timezone",
            ),
            _replace("src/miniapp/clock.py", "datetime.now()", "datetime.now(timezone.utc)"),
        ),
    ),
    _task(
        "D05-bug-cache-key",
        TaskCategory.BUG_DIAGNOSIS,
        "Fix the cache key so requests for different locales cannot collide.",
        {
            "src/miniapp/cache.py": "def cache_key(user: str, locale: str) -> tuple[str, str]:\n    return (user, user)\n"
        },
        (("src/miniapp/cache.py", ("return (user, locale)",)),),
        (_replace("src/miniapp/cache.py", "    return (user, user)", "    return (user, locale)"),),
    ),
    # E — Test-driven repair
    _task(
        "E01-test-discount",
        TaskCategory.TEST_DRIVEN_REPAIR,
        "Use the failing public test to repair discount: a 10 percent discount on 100 is 90.",
        {
            "src/miniapp/discount.py": "def discount(price: float) -> float:\n    return price * 0.1\n",
            "tests/test_discount.py": "from miniapp.discount import discount\n\ndef test_discount() -> None:\n    assert discount(100) == 90\n",
        },
        (("src/miniapp/discount.py", ("return price * 0.9",)),),
        (_replace("src/miniapp/discount.py", "    return price * 0.1", "    return price * 0.9"),),
        public_tests=("tests/test_discount.py",),
    ),
    _task(
        "E02-test-boolean-parser",
        TaskCategory.TEST_DRIVEN_REPAIR,
        "Repair parse_bool so the public tests accept true/yes/1 and reject unknown values.",
        {
            "src/miniapp/bool_parser.py": "def parse_bool(value: str) -> bool:\n    return bool(value)\n",
            "tests/test_bool_parser.py": "import pytest\nfrom miniapp.bool_parser import parse_bool\n\ndef test_values() -> None:\n    assert parse_bool('yes') is True\n    assert parse_bool('0') is False\n    with pytest.raises(ValueError):\n        parse_bool('maybe')\n",
        },
        (
            (
                "src/miniapp/bool_parser.py",
                ('raise ValueError("invalid boolean")', 'normalized in {"true", "yes", "1"}'),
            ),
        ),
        (
            _replace(
                "src/miniapp/bool_parser.py",
                "    return bool(value)",
                '    normalized = value.casefold()\n    if normalized in {"true", "yes", "1"}:\n        return True\n    if normalized in {"false", "no", "0"}:\n        return False\n    raise ValueError("invalid boolean")',
            ),
        ),
        public_tests=("tests/test_bool_parser.py",),
    ),
    _task(
        "E03-test-idempotency",
        TaskCategory.TEST_DRIVEN_REPAIR,
        "Make normalize_idempotent stable after the first normalization, as required by the public test.",
        {
            "src/miniapp/idempotency.py": 'def normalize(value: str) -> str:\n    return value.strip()\n\ndef normalize_idempotent(value: str) -> str:\n    return normalize(value) + "!"\n',
            "tests/test_idempotency.py": "from miniapp.idempotency import normalize_idempotent\n\ndef test_idempotent() -> None:\n    value = normalize_idempotent(' x ')\n    assert normalize_idempotent(value) == value\n",
        },
        (("src/miniapp/idempotency.py", ("return normalize(value)",)),),
        (
            _replace(
                "src/miniapp/idempotency.py",
                '    return normalize(value) + "!"',
                "    return normalize(value)",
            ),
        ),
        public_tests=("tests/test_idempotency.py",),
    ),
    _task(
        "E04-test-unicode-length",
        TaskCategory.TEST_DRIVEN_REPAIR,
        "Fix display_width to count Unicode code points used by the public test, not UTF-8 bytes.",
        {
            "src/miniapp/unicode_utils.py": "def display_width(value: str) -> int:\n    return len(value.encode('utf-8'))\n",
            "tests/test_unicode_utils.py": "from miniapp.unicode_utils import display_width\n\ndef test_unicode() -> None:\n    assert display_width('猫') == 1\n",
        },
        (("src/miniapp/unicode_utils.py", ("return len(value)",)),),
        (
            _replace(
                "src/miniapp/unicode_utils.py",
                "    return len(value.encode('utf-8'))",
                "    return len(value)",
            ),
        ),
        public_tests=("tests/test_unicode_utils.py",),
    ),
    _task(
        "E05-test-retry",
        TaskCategory.TEST_DRIVEN_REPAIR,
        "Use the public test to make retry_count include the initial attempt and never return a negative count.",
        {
            "src/miniapp/retry.py": "def retry_count(attempts: int) -> int:\n    return attempts - 1\n",
            "tests/test_retry.py": "from miniapp.retry import retry_count\n\ndef test_retry() -> None:\n    assert retry_count(0) == 0\n    assert retry_count(3) == 3\n",
        },
        (("src/miniapp/retry.py", ("return max(0, attempts)",)),),
        (
            _replace(
                "src/miniapp/retry.py", "    return attempts - 1", "    return max(0, attempts)"
            ),
        ),
        public_tests=("tests/test_retry.py",),
    ),
    # F — Refactor/API migration
    _task(
        "F01-refactor-total-name",
        TaskCategory.REFACTOR_API_MIGRATION,
        "Rename the old_total API to total consistently across the report module and its caller.",
        {
            "src/miniapp/report.py": "def old_total(values: list[int]) -> int:\n    return sum(values)\n\ndef render(values: list[int]) -> str:\n    return str(old_total(values))\n",
            "src/miniapp/report_client.py": "from .report import old_total\n\ndef send(values: list[int]) -> int:\n    return old_total(values)\n",
        },
        (
            ("src/miniapp/report.py", ("def total(", "return str(total(values))")),
            ("src/miniapp/report_client.py", ("from .report import total", "return total(values)")),
        ),
        (
            _replace_all("src/miniapp/report.py", "old_total", "total"),
            _replace_all("src/miniapp/report_client.py", "old_total", "total"),
        ),
        forbidden=(
            ("src/miniapp/report.py", ("old_total",)),
            ("src/miniapp/report_client.py", ("old_total",)),
        ),
    ),
    _task(
        "F02-refactor-request-api",
        TaskCategory.REFACTOR_API_MIGRATION,
        "Migrate the fetch API to request(url, *, timeout) and update the client call site.",
        {
            "src/miniapp/http_api.py": "def fetch(url: str, timeout: float = 5.0) -> tuple[str, float]:\n    return url, timeout\n",
            "src/miniapp/http_client.py": "from .http_api import fetch\n\ndef call(url: str) -> tuple[str, float]:\n    return fetch(url)\n",
        },
        (
            ("src/miniapp/http_api.py", ("def request(url: str, *, timeout: float = 5.0)",)),
            (
                "src/miniapp/http_client.py",
                ("from .http_api import request", "return request(url, timeout=5.0)"),
            ),
        ),
        (
            _replace_all("src/miniapp/http_api.py", "fetch", "request"),
            _replace(
                "src/miniapp/http_api.py",
                "def request(url: str, timeout: float = 5.0)",
                "def request(url: str, *, timeout: float = 5.0)",
            ),
            _replace(
                "src/miniapp/http_client.py",
                "from .http_api import fetch",
                "from .http_api import request",
            ),
            _replace(
                "src/miniapp/http_client.py",
                "return fetch(url)",
                "return request(url, timeout=5.0)",
            ),
        ),
        forbidden=(("src/miniapp/http_api.py", ("def fetch",)),),
    ),
    _task(
        "F03-refactor-dataclass",
        TaskCategory.REFACTOR_API_MIGRATION,
        "Replace the hand-written User value object with a dataclass while preserving its fields.",
        {
            "src/miniapp/models.py": "class User:\n    def __init__(self, name: str, active: bool) -> None:\n        self.name = name\n        self.active = active\n"
        },
        (
            (
                "src/miniapp/models.py",
                ("from dataclasses import dataclass", "@dataclass", "class User:"),
            ),
        ),
        (
            _replace(
                "src/miniapp/models.py",
                "class User:\n    def __init__(self, name: str, active: bool) -> None:\n        self.name = name\n        self.active = active",
                "from dataclasses import dataclass\n\n@dataclass\nclass User:\n    name: str\n    active: bool",
            ),
        ),
    ),
    _task(
        "F04-refactor-protocol",
        TaskCategory.REFACTOR_API_MIGRATION,
        "Introduce a Reader protocol and type the load function against it.",
        {
            "src/miniapp/reader.py": "class FileReader:\n    def read(self, path: str) -> str:\n        return path\n\ndef load(reader: FileReader, path: str) -> str:\n    return reader.read(path)\n"
        },
        (
            (
                "src/miniapp/reader.py",
                (
                    "from typing import Protocol",
                    "class Reader(Protocol):",
                    "def load(reader: Reader",
                ),
            ),
        ),
        (
            _replace(
                "src/miniapp/reader.py",
                "class FileReader:",
                "from typing import Protocol\n\nclass Reader(Protocol):\n    def read(self, path: str) -> str: ...\n\nclass FileReader:",
            ),
            _replace(
                "src/miniapp/reader.py", "def load(reader: FileReader", "def load(reader: Reader"
            ),
        ),
    ),
    _task(
        "F05-refactor-legacy-adapter",
        TaskCategory.REFACTOR_API_MIGRATION,
        "Keep legacy_name as a compatibility wrapper, but route it through canonical_name.",
        {
            "src/miniapp/names.py": "def canonical_name(value: str) -> str:\n    return value.strip().casefold()\n\ndef legacy_name(value: str) -> str:\n    return value.strip().lower()\n"
        },
        (("src/miniapp/names.py", ("return canonical_name(value)",)),),
        (
            _replace(
                "src/miniapp/names.py",
                "    return value.strip().lower()",
                "    return canonical_name(value)",
            ),
        ),
    ),
    # G — Long-running/tool-control
    _task(
        "G01-tool-control-report",
        TaskCategory.LONG_RUNNING_TOOL_CONTROL,
        "Add a bounded report command that records the catalog size, then run the complete test suite.",
        {"src/miniapp/reporting.py": 'def report() -> str:\n    return "pending"\n'},
        (("src/miniapp/reporting.py", ('return "catalog-items:520"',)),),
        (
            _replace(
                "src/miniapp/reporting.py", '    return "pending"', '    return "catalog-items:520"'
            ),
            "pytest -q",
        ),
    ),
    _task(
        "G02-tool-control-compile",
        TaskCategory.LONG_RUNNING_TOOL_CONTROL,
        "Add a compile_check entry point that compiles the package and returns success; verify with compileall.",
        {"src/miniapp/maintenance.py": "def compile_check() -> bool:\n    return False\n"},
        (("src/miniapp/maintenance.py", ("return True",)),),
        (
            _replace("src/miniapp/maintenance.py", "    return False", "    return True"),
            "python -m compileall -q src",
        ),
    ),
    _task(
        "G03-tool-control-manifest",
        TaskCategory.LONG_RUNNING_TOOL_CONTROL,
        "Create a deterministic manifest writer and its output directory, then check that the generated file is valid.",
        {"src/miniapp/manifest.py": "def manifest() -> dict[str, object]:\n    return {}\n"},
        (
            ("src/miniapp/manifest.py", ('return {"version": 1, "items": 520}',)),
            ("artifacts/manifest.json", ('"items": 520',)),
        ),
        (
            _replace(
                "src/miniapp/manifest.py",
                "    return {}",
                '    return {"version": 1, "items": 520}',
            ),
            _write("artifacts/manifest.json", '{"version": 1, "items": 520}\n'),
        ),
        required_files=("artifacts/manifest.json",),
    ),
    _task(
        "G04-tool-control-timeout",
        TaskCategory.LONG_RUNNING_TOOL_CONTROL,
        "Propagate the caller timeout to the worker instead of discarding it, then run the focused tests.",
        {
            "src/miniapp/worker.py": "def run(job: str, timeout: float | None = None) -> tuple[str, float | None]:\n    return job, None\n"
        },
        (("src/miniapp/worker.py", ("return job, timeout",)),),
        (
            _replace("src/miniapp/worker.py", "    return job, None", "    return job, timeout"),
            "python -m compileall -q src",
        ),
    ),
    _task(
        "G05-tool-control-recovery",
        TaskCategory.LONG_RUNNING_TOOL_CONTROL,
        "Repair the two-file recovery path so failed jobs are recorded and the next run can inspect them.",
        {
            "src/miniapp/recovery.py": "def record_failure(message: str) -> list[str]:\n    return []\n",
            "src/miniapp/recovery_reader.py": "from .recovery import record_failure\n\ndef latest(message: str) -> str:\n    return record_failure(message)[-1]\n",
        },
        (
            ("src/miniapp/recovery.py", ("return [message]",)),
            ("src/miniapp/recovery_reader.py", ("return record_failure(message)[-1]",)),
        ),
        (_replace("src/miniapp/recovery.py", "    return []", "    return [message]"),),
    ),
    # H — External information; the reference is frozen and bundled locally.
    _task(
        "H01-web-pagination-contract",
        TaskCategory.EXTERNAL_INFORMATION,
        "Using the frozen vendor pagination reference in docs/vendor/reference.md, omit an empty cursor and expose next_cursor.",
        {
            "docs/vendor/reference.md": _H1_DOC,
            "src/miniapp/vendor_client.py": 'def query(limit: int, cursor: str | None = None) -> dict[str, object]:\n    return {"limit": limit, "cursor": cursor}\n',
        },
        (
            (
                "src/miniapp/vendor_client.py",
                (
                    'payload = {"limit": limit}',
                    "if cursor is not None:",
                    'return {"next_cursor": None, **payload}',
                ),
            ),
        ),
        (
            _replace(
                "src/miniapp/vendor_client.py",
                '    return {"limit": limit, "cursor": cursor}',
                '    payload = {"limit": limit}\n    if cursor is not None:\n        payload["cursor"] = cursor\n    return {"next_cursor": None, **payload}',
            ),
        ),
        web=True,
        external_dependency=_H1_URL,
        external_reference_sha256=_H1_SHA,
    ),
    _task(
        "H02-web-empty-response",
        TaskCategory.EXTERNAL_INFORMATION,
        "Using the frozen vendor status reference, treat HTTP 204 as a successful empty response.",
        {
            "docs/vendor/reference.md": _H2_DOC,
            "src/miniapp/vendor_status.py": "def ok(status: int) -> bool:\n    return status == 200\n",
        },
        (("src/miniapp/vendor_status.py", ("return status in {200, 204}",)),),
        (
            _replace(
                "src/miniapp/vendor_status.py",
                "    return status == 200",
                "    return status in {200, 204}",
            ),
        ),
        web=True,
        external_dependency=_H2_URL,
        external_reference_sha256=_H2_SHA,
    ),
    _task(
        "H03-web-rfc3339",
        TaskCategory.EXTERNAL_INFORMATION,
        "Using the frozen vendor timestamp reference, format UTC timestamps with a trailing Z.",
        {
            "docs/vendor/reference.md": _H3_DOC,
            "src/miniapp/timestamps.py": "from datetime import datetime\n\ndef format_vendor(value: datetime) -> str:\n    return value.isoformat()\n",
        },
        (("src/miniapp/timestamps.py", ('replace("+00:00", "Z")',)),),
        (
            _replace(
                "src/miniapp/timestamps.py",
                "    return value.isoformat()",
                '    return value.isoformat().replace("+00:00", "Z")',
            ),
        ),
        web=True,
        external_dependency=_H3_URL,
        external_reference_sha256=_H3_SHA,
    ),
    _task(
        "H04-web-auth-header",
        TaskCategory.EXTERNAL_INFORMATION,
        "Using the frozen vendor authentication reference, construct a Bearer Authorization header and never a query token.",
        {
            "docs/vendor/reference.md": _H4_DOC,
            "src/miniapp/auth.py": 'def headers(token: str) -> dict[str, str]:\n    return {"X-Token": token}\n',
        },
        (("src/miniapp/auth.py", ('"Authorization": f"Bearer {token}"',)),),
        (
            _replace(
                "src/miniapp/auth.py",
                '    return {"X-Token": token}',
                '    return {"Authorization": f"Bearer {token}"}',
            ),
        ),
        web=True,
        external_dependency=_H4_URL,
        external_reference_sha256=_H4_SHA,
    ),
    _task(
        "H05-web-rate-limit",
        TaskCategory.EXTERNAL_INFORMATION,
        "Using the frozen vendor rate-limit reference, parse a bounded Retry-After delay from a 429 response.",
        {
            "docs/vendor/reference.md": _H5_DOC,
            "src/miniapp/rate_limit.py": "def retry_after(headers: dict[str, str]) -> int:\n    return 0\n",
        },
        (
            (
                "src/miniapp/rate_limit.py",
                ('min(60, max(0, int(headers.get("Retry-After", "0"))))',),
            ),
        ),
        (
            _replace(
                "src/miniapp/rate_limit.py",
                "    return 0",
                '    return min(60, max(0, int(headers.get("Retry-After", "0"))))',
            ),
        ),
        web=True,
        external_dependency=_H5_URL,
        external_reference_sha256=_H5_SHA,
    ),
)


def get_tasks() -> tuple[TaskSpec, ...]:
    return TASKS


def task_by_id(task_id: str) -> TaskSpec:
    for task in TASKS:
        if task.task_id == task_id:
            return task
    raise KeyError(task_id)


def task_ids() -> tuple[str, ...]:
    return tuple(task.task_id for task in TASKS)


__all__ = ["TASKS", "get_tasks", "task_by_id", "task_ids"]
