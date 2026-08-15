from __future__ import annotations

import unittest

from neuro_code.infrastructure.sandbox.windows_sandbox_accounts import (
    BUILTIN_ADMINISTRATORS_SID,
    BUILTIN_USERS_SID,
    SANDBOX_OFFLINE_USERNAME,
    SANDBOX_ONLINE_USERNAME,
    InMemoryWindowsSandboxAccountApi,
    WindowsAccountSid,
    WindowsSandboxAccountError,
    generate_windows_account_password,
)


class WindowsSandboxAccountContractTests(unittest.TestCase):
    def test_account_sid_is_distinct_from_synthetic_restricting_sid(self) -> None:
        self.assertEqual(
            str(WindowsAccountSid("S-1-5-21-100-200-300-2000")), "S-1-5-21-100-200-300-2000"
        )
        with self.assertRaises(ValueError):
            WindowsAccountSid("S-1-5-21-nope")

    def test_password_has_required_length_and_classes(self) -> None:
        password = generate_windows_account_password()
        self.assertGreaterEqual(len(password), 20)
        self.assertTrue(any(character.isupper() for character in password))
        self.assertTrue(any(character.islower() for character in password))
        self.assertTrue(any(character.isdigit() for character in password))
        self.assertTrue(any(not character.isalnum() for character in password))

    def test_local_group_facts_use_locale_independent_builtin_sids(self) -> None:
        accounts = InMemoryWindowsSandboxAccountApi()
        facts = accounts.ensure_user(SANDBOX_OFFLINE_USERNAME, "offline-password")
        qualified = type(facts)(
            facts.username,
            facts.sid,
            ("BUILTIN\\Users",),
            facts.enabled,
            facts.user_privilege,
            facts.created_by_installation,
            (BUILTIN_USERS_SID,),
        )
        qualified.validate(expected_username=SANDBOX_OFFLINE_USERNAME)
        localized = type(facts)(
            facts.username,
            facts.sid,
            ("Benutzer",),
            facts.enabled,
            facts.user_privilege,
            facts.created_by_installation,
            (BUILTIN_USERS_SID,),
        )
        localized.validate(expected_username=SANDBOX_OFFLINE_USERNAME)
        privileged = type(facts)(
            facts.username,
            facts.sid,
            ("Administratoren",),
            facts.enabled,
            facts.user_privilege,
            facts.created_by_installation,
            (BUILTIN_USERS_SID, BUILTIN_ADMINISTRATORS_SID),
        )
        with self.assertRaises(WindowsSandboxAccountError):
            privileged.validate(expected_username=SANDBOX_OFFLINE_USERNAME)

    def test_two_users_are_real_account_subjects_and_stable(self) -> None:
        accounts = InMemoryWindowsSandboxAccountApi()
        offline = accounts.ensure_user(SANDBOX_OFFLINE_USERNAME, "offline-password")
        online = accounts.ensure_user(SANDBOX_ONLINE_USERNAME, "online-password")
        self.assertNotEqual(offline.sid, online.sid)
        self.assertTrue(offline.created_by_installation)
        self.assertEqual(
            accounts.validate_user(
                SANDBOX_OFFLINE_USERNAME,
                "offline-password",
                expected_sid=offline.sid,
            ).sid,
            offline.sid,
        )
        self.assertEqual(
            accounts.lookup_user(
                SANDBOX_OFFLINE_USERNAME,
                expected_sid=offline.sid,
            ).sid,
            offline.sid,
        )
        with self.assertRaises(WindowsSandboxAccountError):
            accounts.validate_user(
                SANDBOX_OFFLINE_USERNAME,
                "wrong-password",
                expected_sid=offline.sid,
            )

    def test_sid_mismatch_fails_closed(self) -> None:
        accounts = InMemoryWindowsSandboxAccountApi()
        offline = accounts.ensure_user(SANDBOX_OFFLINE_USERNAME, "offline-password")
        with self.assertRaises(WindowsSandboxAccountError):
            accounts.ensure_user(
                SANDBOX_OFFLINE_USERNAME,
                "new-password",
                expected_sid=WindowsAccountSid("S-1-5-21-100-200-300-9999"),
            )
        accounts.remove_user(offline)
        self.assertFalse(accounts.user_exists(SANDBOX_OFFLINE_USERNAME))

    def test_existing_user_is_reused_without_losing_sid(self) -> None:
        accounts = InMemoryWindowsSandboxAccountApi()
        first = accounts.ensure_user(SANDBOX_OFFLINE_USERNAME, "old-password")
        second = accounts.ensure_user(
            SANDBOX_OFFLINE_USERNAME,
            "new-password",
            expected_sid=first.sid,
        )
        self.assertEqual(second.sid, first.sid)
        self.assertFalse(second.created_by_installation)
        self.assertEqual(
            accounts.validate_user(
                SANDBOX_OFFLINE_USERNAME,
                "new-password",
                expected_sid=first.sid,
            ).sid,
            first.sid,
        )

    def test_account_input_and_lookup_fail_closed(self) -> None:
        accounts = InMemoryWindowsSandboxAccountApi()
        for username, password in (
            ("", "pw"),
            ("bad\\name", "pw"),
            ("bad\x00name", "pw"),
            ("user", ""),
        ):
            with self.assertRaises(WindowsSandboxAccountError):
                accounts.ensure_user(username, password)
        offline = accounts.ensure_user(SANDBOX_OFFLINE_USERNAME, "offline-password")
        with self.assertRaises(WindowsSandboxAccountError):
            accounts.validate_user(
                SANDBOX_OFFLINE_USERNAME,
                "offline-password",
                expected_sid=WindowsAccountSid("S-1-5-21-100-200-300-9999"),
            )
        with self.assertRaises(WindowsSandboxAccountError):
            accounts.lookup_user(
                SANDBOX_ONLINE_USERNAME,
                expected_sid=offline.sid,
            )
        with self.assertRaises(ValueError):
            generate_windows_account_password(19)


if __name__ == "__main__":
    unittest.main()
