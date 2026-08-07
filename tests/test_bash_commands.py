from __future__ import annotations

import unittest

from neuro_code.domain.permissions.bash_commands import analyze_bash_command


class BashCommandAnalysisTests(unittest.TestCase):
    def test_splits_chained_commands_without_splitting_quoted_operators(self) -> None:
        analysis = analyze_bash_command("git status && echo 'a && b' | sed 's/a/b/'")

        self.assertTrue(analysis.complete)
        self.assertEqual(
            [segment.forms for segment in analysis.segments],
            [("git status",), ("echo a && b",), ("sed s/a/b/",)],
        )

    def test_strips_assignments_and_recursively_checks_wrappers_and_shell_c(self) -> None:
        analysis = analyze_bash_command(
            "FOO=value timeout 30 env BAR=x bash -c 'git status && rm -rf generated'"
        )

        self.assertTrue(analysis.complete)
        self.assertEqual(
            analysis.segments[0].forms,
            (
                "timeout 30 env BAR=x bash -c git status && rm -rf generated",
                "bash -c git status && rm -rf generated",
            ),
        )
        self.assertEqual(analysis.segments[1].forms, ("git status",))
        self.assertEqual(analysis.segments[2].forms, ("rm -rf generated",))

    def test_rejects_constructs_that_cannot_be_safely_classified(self) -> None:
        commands = (
            "echo $(dangerous-command)",
            "echo `dangerous-command`",
            "echo $EXPANSION",
            "echo hi > output.txt",
            "(echo subshell)",
            "echo first\necho second",
            "echo 'unterminated",
            "PATH=/untrusted git status",
            "env LD_PRELOAD=payload.so git status",
            "git status &&",
            "&& git status",
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(analyze_bash_command(command).complete)

    def test_accepts_comments_escapes_and_trailing_semicolon(self) -> None:
        analysis = analyze_bash_command(r"echo one\ two; git status; # ignored")

        self.assertTrue(analysis.complete)
        self.assertEqual(
            [segment.forms for segment in analysis.segments],
            [("echo one two",), ("git status",)],
        )

    def test_supported_wrapper_options_expose_the_inner_command(self) -> None:
        cases = (
            ("/usr/bin/nice --adjustment=5 git status", "git status"),
            ("ionice --class 2 --classdata 7 git status", "git status"),
            ("chrt --fifo 10 git status", "git status"),
            ("stdbuf -o L -eL git status", "git status"),
            ("env --chdir=/tmp -u FOO BAR=x git status", "git status"),
            ("env -- BAR=x git status", "git status"),
        )
        for command, inner in cases:
            with self.subTest(command=command):
                analysis = analyze_bash_command(command)
                self.assertTrue(analysis.complete)
                self.assertEqual(analysis.segments[0].forms[-1], inner)

    def test_quote_and_escape_lexing_preserves_literal_shell_characters(self) -> None:
        commands = {
            r'''echo "a\"b"''': 'echo a"b',
            "echo '$HOME && literal'": "echo $HOME && literal",
            r"echo \$HOME": "echo $HOME",
            "echo foo#bar": "echo foo#bar",
            "true&&false||true": "true",
        }
        for command, first_form in commands.items():
            with self.subTest(command=command):
                analysis = analyze_bash_command(command)
                self.assertTrue(analysis.complete)
                self.assertEqual(analysis.segments[0].forms[0], first_form)

    def test_invalid_wrapper_shapes_and_background_execution_fail_closed(self) -> None:
        for command in (
            "timeout",
            "timeout 5",
            "env",
            "env -- PATH=/untrusted git status",
            "nice -n",
            "chrt --fifo",
            "sleep 1 &",
            "echo trailing\\",
        ):
            with self.subTest(command=command):
                self.assertFalse(analyze_bash_command(command).complete)


if __name__ == "__main__":
    unittest.main()
