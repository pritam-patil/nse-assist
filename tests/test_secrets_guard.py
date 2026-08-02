"""The secret scanner must catch the leak that actually happened, and its cousins.

    python -m unittest discover -s tests -v

A guard nobody has watched fail is an assumption. These assert it fires on the
exact shape that reached a public repo — a filled-in .env.example — and that it
stays quiet on the things a template legitimately contains, because a scanner that
cries wolf gets bypassed with --no-verify and then protects nothing.

Fixtures below are shaped like credentials but are not any: the token digits are
sequential and the secret half is a repeated letter. This file is in
secrets_guard.SELF_EXEMPT so the scanner does not report its own fixtures.
"""

import unittest

from src import secrets_guard

# Shaped like a Telegram token, deliberately not one.
FAKE_TOKEN = "1234567890:" + "A" * 35


class ExampleFileTestCase(unittest.TestCase):
    """The leak that happened: a template with its values filled in."""

    def test_filled_in_template_is_caught(self):
        findings = secrets_guard.scan_text(".env.example", f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\n")
        self.assertTrue(findings)

    def test_empty_template_is_clean(self):
        text = "TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n"
        self.assertEqual(secrets_guard.scan_text(".env.example", text), [])

    def test_commented_defaults_are_allowed(self):
        """Documenting a default is what a template is for, so a commented value
        must not trip the guard — otherwise the guard gets disabled."""
        text = "# DB_PATH=output/nse.db\n# PRICE_SOURCE=yahoo\nTELEGRAM_BOT_TOKEN=\n"
        self.assertEqual(secrets_guard.scan_text(".env.example", text), [])

    def test_any_value_in_a_template_is_caught_not_just_secrets(self):
        """A template carrying a plain value is still wrong, and 'does it look like
        a secret' is the wrong question to ask of a file that should have none."""
        findings = secrets_guard.scan_text(".env.example", "FUND_SCHEME_CODES=119091\n")
        self.assertTrue(findings)

    def test_other_template_suffixes(self):
        for name in ("config.sample", "settings.template", "app.dist"):
            with self.subTest(file=name):
                self.assertTrue(secrets_guard.scan_text(name, f"KEY={FAKE_TOKEN}\n"))

    def test_a_normal_file_may_contain_assignments(self):
        """The template rule must not fire on ordinary code."""
        self.assertEqual(secrets_guard.scan_text("src/config.py", "DB_PATH = 'output/nse.db'\n"), [])


class PatternTestCase(unittest.TestCase):
    """Shape-based detection, so the next leak is caught in whatever file it lands in."""

    def test_telegram_token_anywhere(self):
        findings = secrets_guard.scan_text("README.md", f"export TOKEN={FAKE_TOKEN}\n")
        self.assertTrue(findings)
        self.assertIn("telegram", findings[0][2])

    def test_private_key_block(self):
        findings = secrets_guard.scan_text("deploy.sh", "-----BEGIN RSA PRIVATE KEY-----\n")
        self.assertTrue(findings)

    def test_aws_key(self):
        self.assertTrue(secrets_guard.scan_text("notes.txt", "AKIAIOSFODNN7EXAMPLE\n"))

    def test_dotenv_may_never_be_committed(self):
        """.gitignore covers it, but `git add -f` does not care about .gitignore."""
        findings = secrets_guard.scan_text(".env", "TELEGRAM_CHAT_ID=5813253073\n")
        self.assertTrue(findings)
        self.assertEqual(findings[0][1], 0)

    def test_ordinary_numbers_are_not_tokens(self):
        """A chat id, a scheme code and a price must not read as credentials."""
        for text in ("chat_id = 5813253073\n", "scheme 119091 nav 5538.28\n", "close = 1307.80\n"):
            with self.subTest(text=text.strip()):
                self.assertEqual(secrets_guard.scan_text("src/notes.py", text), [])


class ReportingTestCase(unittest.TestCase):
    def test_the_value_is_never_printed(self):
        """A scanner that echoes the secret to prove it found one has just copied it
        into your scrollback and your CI logs."""
        findings = secrets_guard.scan_text(".env.example", f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\n")
        rendered = secrets_guard.describe(findings)
        self.assertNotIn(FAKE_TOKEN, rendered)
        self.assertNotIn("A" * 35, rendered)

    def test_findings_carry_file_and_line(self):
        text = "CLEAN=\n\nDIRTY=value\n"
        findings = secrets_guard.scan_text(".env.example", text)
        self.assertEqual(findings[0][0], ".env.example")
        self.assertEqual(findings[0][1], 3)

    def test_scanner_does_not_report_itself(self):
        """src/secrets_guard.py necessarily contains the patterns it hunts for."""
        self.assertEqual(secrets_guard.scan_text("src/secrets_guard.py", FAKE_TOKEN), [])

    def test_repository_is_currently_clean(self):
        """The live assertion: this repo has no credentials in tracked files."""
        findings = secrets_guard.tracked_findings()
        self.assertEqual(findings, [], secrets_guard.describe(findings))


if __name__ == "__main__":
    unittest.main()
