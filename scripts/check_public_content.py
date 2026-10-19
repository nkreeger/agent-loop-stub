#!/usr/bin/env python3
"""Scan tracked text for generic secret patterns and caller-supplied terms."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys


GENERIC_PATTERNS = {
    "private key": re.compile(
        "-" * 5 + r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY" + "-" * 5
    ),
    "credential assignment": re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[:=]\s*['\"][^'\"]+"
    ),
    "developer home path": re.compile(
        "(?:"
        + "/".join(("", "home", r"[^/\s]+"))
        + r"|[A-Za-z]:\\\\Users\\\\[^\\\\\s]+)"
    ),
    "non-public URL": re.compile(r"https?://(?:localhost|[^/\s]*\.(?:internal|corp|local))(?:/|\b)"),
}


def tracked_files(root: Path) -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    return [
        root / item.decode("utf-8")
        for item in completed.stdout.split(b"\0")
        if item
    ]


def load_terms(paths: list[Path]) -> list[str]:
    terms: list[str] = []
    for path in paths:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            term = raw_line.strip()
            if term and not term.startswith("#"):
                terms.append(term.casefold())
    return terms


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--deny-file",
        type=Path,
        action="append",
        default=[],
        help="private newline-delimited terms; files are read but never copied",
    )
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    terms = load_terms(args.deny_file)
    findings: list[str] = []
    for path in tracked_files(root):
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(root)
        for label, pattern in GENERIC_PATTERNS.items():
            if pattern.search(content):
                findings.append(f"{relative}: matched {label}")
        folded = content.casefold()
        for index, term in enumerate(terms, 1):
            if term in folded:
                findings.append(f"{relative}: matched private deny term #{index}")

    if findings:
        print("public-content scan failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print(f"public-content scan passed ({len(tracked_files(root))} tracked files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
