"""Detects credentials that are about to be committed, or already have been.

    python -m src.secrets_guard --staged     # what the pre-commit hook runs
    python -m src.secrets_guard --tracked    # what --stage doctor runs

One scanner, two callers. The hook and the doctor check must agree about what
counts as a secret: if the hook were stricter, commits would fail for things doctor
calls fine; if doctor were stricter, it would flag what the hook just waved
through. Either way you learn to ignore one of them.

WHY THIS EXISTS

A real bot token reached a public repository through .env.example. That file is a
template living next to the real .env, one letter apart, and filling it in "just to
test" is a thirty-second mistake that survives forever in git history. Revoking the
token closed that exposure; this stops the next one.

The hook reads STAGED content, not the working tree. Those differ whenever
something is edited after `git add`, and it is the staged version that gets
committed — checking the file on disk would pass a commit whose contents are
different from what was inspected.
"""

import argparse
import re
import subprocess
import sys

# A template's whole purpose is to carry keys without values. Anything after the
# `=` on an uncommented line is either a secret or a default that belongs in code —
# both worth stopping. Commented lines are how defaults get documented, so they are
# left alone.
EXAMPLE_SUFFIXES = (".example", ".sample", ".template", ".dist")

# Files that must never be committed whatever their contents. .gitignore already
# covers these, but `git add -f` does not care about .gitignore.
FORBIDDEN_NAMES = (".env", ".env.local", ".env.production")

# Shape-based detection, so a secret is caught in any file rather than only in the
# one that leaked last time.
PATTERNS = (
    ("telegram bot token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")),
    ("aws access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("generic bearer token", re.compile(r"\b(?:ghp|gho|github_pat|sk-|xoxb-)[A-Za-z0-9_\-]{16,}")),
)

# This file necessarily contains the patterns it looks for, and the tests contain
# fixtures shaped like secrets. Scanning them would mean the guard reports itself.
SELF_EXEMPT = ("src/secrets_guard.py", "tests/test_secrets_guard.py")


def _is_example(path):
    return path.endswith(EXAMPLE_SUFFIXES)


def scan_text(path, text):
    """Findings for one file's contents, as (path, line number, reason) tuples.

    Never includes the offending value. A scanner that prints the secret to prove
    it found one has copied it into your terminal scrollback and your CI logs.
    """
    findings = []
    normalised = path.replace("\\", "/")
    if normalised in SELF_EXEMPT:
        return findings

    if normalised.split("/")[-1] in FORBIDDEN_NAMES:
        findings.append((path, 0, "this file holds real credentials and must never be committed"))
        return findings

    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if _is_example(normalised) and "=" in stripped:
            _, _, value = stripped.partition("=")
            if value.strip():
                findings.append((
                    path, number,
                    f"template carries a value for {stripped.partition('=')[0].strip()} "
                    f"— an example file should list keys, not fill them in",
                ))

        for label, pattern in PATTERNS:
            if pattern.search(line):
                findings.append((path, number, f"looks like a {label}"))
    return findings


def _run(*args):
    return subprocess.run(args, capture_output=True, text=True, check=False)


def staged_findings():
    """Scan what is staged, which is what would actually be committed."""
    listing = _run("git", "diff", "--cached", "--name-only", "--diff-filter=ACM")
    findings = []
    for path in [p for p in listing.stdout.splitlines() if p.strip()]:
        blob = _run("git", "show", f":{path}")
        if blob.returncode != 0:
            continue
        findings.extend(scan_text(path, blob.stdout))
    return findings


def tracked_findings():
    """Scan the committed working tree — catches what is already in the repo."""
    listing = _run("git", "ls-files")
    findings = []
    for path in [p for p in listing.stdout.splitlines() if p.strip()]:
        try:
            with open(path, encoding="utf-8") as handle:
                findings.extend(scan_text(path, handle.read()))
        except (OSError, UnicodeDecodeError):
            continue  # binaries and unreadable files carry no reviewable secrets
    return findings


def describe(findings):
    return "\n".join(f"  {path}:{line}  {reason}" for path, line, reason in findings)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Detect credentials in this repository.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true", help="scan staged content (pre-commit)")
    group.add_argument("--tracked", action="store_true", help="scan tracked files (doctor)")
    args = parser.parse_args(argv)

    findings = tracked_findings() if args.tracked else staged_findings()
    if not findings:
        print("secrets-guard: clean")
        return 0

    scope = "tracked files" if args.tracked else "staged changes"
    print(f"secrets-guard: {len(findings)} problem(s) in {scope}\n", file=sys.stderr)
    print(describe(findings), file=sys.stderr)
    print(
        "\nNothing above prints the value itself. Fix the file, then re-stage.\n"
        "If a finding is a false positive, `git commit --no-verify` bypasses this —\n"
        "but a token that reaches a public repo is compromised the moment it lands,\n"
        "and revoking it is the only fix after that.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
