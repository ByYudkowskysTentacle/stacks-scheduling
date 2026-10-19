#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Block commits that look like they contain real personal data (CLAUDE.md §2.1, §6).

Secrets are gitleaks' job. This catches what gitleaks doesn't know about: student
records. It flags:

- spreadsheet, database and dump files anywhere;
- data files (CSV, TSV, ICS, vCard) outside the synthetic-fixture folders;
- `.env` files other than `.env.example`;
- email addresses outside reserved example domains;
- strings shaped like US Social Security numbers or phone numbers.

Usage:
    python tools/check_sensitive.py --staged   # pre-commit hook: staged files only
    python tools/check_sensitive.py --all      # CI: every tracked file

False positive? Add `sensitive-check: allow` on the line, or a path glob to
`.sensitive-allowlist`, with a comment saying why. Never use either for real data.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import PurePosixPath

BLOCKED_SUFFIXES = {
    ".xlsx",
    ".xlsm",
    ".xls",
    ".ods",
    ".numbers",  # spreadsheets: fixtures must be CSV
    ".sqlite",
    ".sqlite3",
    ".db",
    ".dump",
    ".pgdump",
    ".bak",  # databases and dumps
}
DATA_SUFFIXES = {".csv", ".tsv", ".ics", ".vcf"}
DATA_DIRS = ("fixtures/", "tests/", "spikes/")  # synthetic data only, per CLAUDE.md §2.1

ALLOWED_EMAIL_DOMAINS = re.compile(
    r"(^|\.)(example\.(com|org|net|edu)|example|test|invalid|localhost)$"
    r"|^users\.noreply\.github\.com$|^anthropic\.com$",
    re.IGNORECASE,
)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)\b")
SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
PHONE = re.compile(r"(?<![\d-])\(?\b\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b(?![\d-])")
# Add the campus ID format here once known, for example re.compile(r"\bA\d{8}\b").
EXTRA_PATTERNS: list[tuple[str, re.Pattern]] = []

ALLOW_MARKER = "sensitive-check: allow"
ALLOWLIST_FILE = ".sensitive-allowlist"


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def list_files(staged: bool) -> list[str]:
    if staged:
        out = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    else:
        out = git("ls-files", "-z")
    return [p for p in out.decode("utf-8").split("\0") if p]


def read(path: str, staged: bool) -> bytes:
    if staged:
        return git("show", f":{path}")
    with open(path, "rb") as f:
        return f.read()


def load_allowlist() -> list[str]:
    try:
        with open(ALLOWLIST_FILE, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    except FileNotFoundError:
        return []


def check_path(path: str) -> list[str]:
    p = PurePosixPath(path)
    suffix = p.suffix.lower()
    name = p.name.lower()
    problems = []
    if suffix in BLOCKED_SUFFIXES:
        problems.append(f"{path}: {suffix} files can't be committed (fixtures must be synthetic CSV)")
    if suffix in DATA_SUFFIXES and not path.startswith(DATA_DIRS):
        problems.append(f"{path}: data files belong under {', '.join(DATA_DIRS)} and must be synthetic")
    if name.startswith(".env") and name != ".env.example":
        problems.append(f"{path}: environment files hold secrets; commit .env.example instead")
    return problems


def check_content(path: str, data: bytes) -> list[str]:
    if b"\0" in data[:8192]:
        return []  # binary
    problems = []
    for lineno, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
        if ALLOW_MARKER in line:
            continue
        for m in EMAIL.finditer(line):
            if not ALLOWED_EMAIL_DOMAINS.search(m.group(1)):
                problems.append(f"{path}:{lineno}: email address outside example domains")
        if SSN.search(line):
            problems.append(f"{path}:{lineno}: looks like a Social Security number")
        if PHONE.search(line):
            problems.append(f"{path}:{lineno}: looks like a phone number")
        for label, pattern in EXTRA_PATTERNS:
            if pattern.search(line):
                problems.append(f"{path}:{lineno}: looks like a {label}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="check staged files (pre-commit)")
    mode.add_argument("--all", action="store_true", help="check every tracked file (default)")
    args = ap.parse_args()

    allow = load_allowlist()
    problems: list[str] = []
    for path in list_files(args.staged):
        if path == ALLOWLIST_FILE or any(fnmatch.fnmatch(path, g) for g in allow):
            continue
        problems += check_path(path)
        problems += check_content(path, read(path, args.staged))

    if problems:
        # Report locations only, never the matched text, so the check itself can't leak data.
        print("Possible personal data or secrets found:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print(
            "\nRepository data must be synthetic (CLAUDE.md section 2.1). If this is a false positive, add "
            f"'{ALLOW_MARKER}' to the line or a path glob to {ALLOWLIST_FILE}, with a reason.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
