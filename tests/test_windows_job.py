from __future__ import annotations

import unittest
from unittest import mock

from neuro_code.infrastructure.sandbox.windows_job import (
    WindowsJobObject,
    _BasicAccountingInformation,
    _ExtendedLimitInformation,
)


class _FakeWindowsJobApi:
    def __init__(self) -> None:
        self.job_handle: int | None = 101
        self.configure_ok = True
        self.query_ok = True
        self.terminate_ok = True
        self.close_failures: set[int] = set()
        self.error_code = 5
        self.active_process_count = 0
        self.calls: list[tuple[object, ...]] = []
        self.limit_flags: int | None = None

    def create_job_object(self) -> int | None:
        self.calls.append(("create",))
        return self.job_handle

    def set_information_job_object(
        self,
        job_handle: int,
        information_class: int,
        information: _ExtendedLimitInformation,
    ) -> bool:
        self.calls.append(("configure", job_handle, information_class))
        self.limit_flags = int(information.BasicLimitInformation.LimitFlags)
        return self.configure_ok

    def query_information_job_object(
        self,
        job_handle: int,
        information_class: int,
        information: _BasicAccountingInformation,
    ) -> bool:
        self.calls.append(("query", job_handle, information_class))
        information.ActiveProcesses = self.active_process_count
        return self.query_ok

    def terminate_job_object(self, job_handle: int, exit_code: int) -> bool:
        self.calls.append(("terminate", job_handle, exit_code))
        return self.terminate_ok

    def close_handle(self, handle: int) -> bool:
        self.calls.append(("close", handle))
        return handle not in self.close_failures

    def get_last_error(self) -> int:
        return self.error_code


class WindowsJobObjectTests(unittest.TestCase):
    def test_non_windows_default_creation_fails_cleanly(self) -> None:
        with (
            mock.patch("neuro_code.infrastructure.sandbox.windows_job.os.name", "posix"),
            self.assertRaisesRegex(OSError, "only available on Windows"),
        ):
            WindowsJobObject.create()

    def test_create_configures_kill_on_job_close(self) -> None:
        api = _FakeWindowsJobApi()

        job = WindowsJobObject.create(api=api)

        self.assertEqual(api.calls[:2], [("create",), ("configure", 101, 9)])
        self.assertEqual(api.limit_flags, 0x2000)
        job.close()

    def test_create_failure_reports_last_error_without_closing_null_handle(self) -> None:
        api = _FakeWindowsJobApi()
        api.job_handle = None
        api.error_code = 6

        with self.assertRaisesRegex(OSError, "CreateJobObjectW.*Windows error 6"):
            WindowsJobObject.create(api=api)

        self.assertEqual(api.calls, [("create",)])

    def test_configuration_failure_closes_job_and_reports_last_error(self) -> None:
        api = _FakeWindowsJobApi()
        api.configure_ok = False

        with self.assertRaisesRegex(OSError, "SetInformationJobObject.*Windows error 5"):
            WindowsJobObject.create(api=api)

        self.assertEqual(api.calls[-1], ("close", 101))

    def test_configuration_exception_still_closes_job(self) -> None:
        api = _FakeWindowsJobApi()
        with (
            mock.patch.object(api, "set_information_job_object", side_effect=RuntimeError("boom")),
            self.assertRaisesRegex(RuntimeError, "boom"),
        ):
            WindowsJobObject.create(api=api)

        self.assertEqual(api.calls[-1], ("close", 101))

    def test_process_creation_handle_is_borrowed_until_job_close(self) -> None:
        api = _FakeWindowsJobApi()
        job = WindowsJobObject.create(api=api)

        self.assertEqual(job.process_creation_handle, 101)
        self.assertNotIn(("close", 101), api.calls)
        job.close()

        with self.assertRaisesRegex(RuntimeError, "is closed"):
            _ = job.process_creation_handle

    def test_active_processes_queries_basic_accounting_information(self) -> None:
        api = _FakeWindowsJobApi()
        api.active_process_count = 7
        job = WindowsJobObject.create(api=api)

        self.assertEqual(job.active_processes, 7)
        self.assertEqual(api.calls[-1], ("query", 101, 1))
        job.close()

    def test_active_process_query_failure_is_reported(self) -> None:
        api = _FakeWindowsJobApi()
        api.query_ok = False
        job = WindowsJobObject.create(api=api)

        with self.assertRaisesRegex(OSError, "QueryInformationJobObject.*Windows error 5"):
            _ = job.active_processes

        job.close()

    def test_terminate_job_object(self) -> None:
        api = _FakeWindowsJobApi()
        job = WindowsJobObject.create(api=api)

        job.terminate(exit_code=23)

        self.assertEqual(api.calls[-1], ("terminate", 101, 23))
        job.close()

    def test_termination_failure_is_reported(self) -> None:
        api = _FakeWindowsJobApi()
        api.terminate_ok = False
        job = WindowsJobObject.create(api=api)

        with self.assertRaisesRegex(OSError, "TerminateJobObject.*Windows error 5"):
            job.terminate()

        job.close()

    def test_close_is_idempotent(self) -> None:
        api = _FakeWindowsJobApi()
        job = WindowsJobObject.create(api=api)

        job.close()
        job.close()

        self.assertEqual(api.calls.count(("close", 101)), 1)

    def test_close_clears_handle_even_when_close_handle_fails(self) -> None:
        api = _FakeWindowsJobApi()
        api.close_failures.add(101)
        job = WindowsJobObject.create(api=api)

        with self.assertRaisesRegex(OSError, r"CloseHandle\(job\).*Windows error 5"):
            job.close()
        job.close()

        self.assertEqual(api.calls.count(("close", 101)), 1)

    def test_destructor_makes_best_effort_without_raising(self) -> None:
        api = _FakeWindowsJobApi()
        api.close_failures.add(101)
        job = WindowsJobObject.create(api=api)

        job.__del__()
        job.__del__()

        self.assertEqual(api.calls.count(("close", 101)), 1)

    def test_operations_reject_closed_job(self) -> None:
        api = _FakeWindowsJobApi()
        job = WindowsJobObject.create(api=api)
        job.close()

        with self.assertRaisesRegex(RuntimeError, "is closed"):
            _ = job.active_processes

    def test_numeric_arguments_are_validated_before_win32_calls(self) -> None:
        api = _FakeWindowsJobApi()
        job = WindowsJobObject.create(api=api)

        with self.assertRaisesRegex(ValueError, "unsigned 32-bit"):
            job.terminate(-1)

        job.close()


if __name__ == "__main__":
    unittest.main()
