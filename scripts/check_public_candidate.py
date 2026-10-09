"""Scan a staged source tree for common accidental private artifacts."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


TEXT_SUFFIXES = {
    ".cfg", ".conf", ".css", ".diff", ".html", ".ini", ".js", ".json",
    ".md", ".patch", ".py", ".sh", ".toml", ".txt", ".yaml", ".yml",
}
FORBIDDEN_SUFFIXES = {
    ".apk", ".db", ".jpeg", ".jpg", ".log", ".mitm", ".png", ".qcow2",
    ".sqlite", ".sqlite3", ".webp", ".img",
}
RULES = (
    ("absolute_user_path", re.compile("/" + "Us" + "ers/" + r"[^/\s]+/")),
    ("numeric_private_session", re.compile(r"FriendMessage:\d{5,12}\b")),
    ("persona_identifier", re.compile(r"\u6c88\u661f\u56de")),
    ("literal_secret_assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|ssoid|token_auth_id)"
        r"\s*[:=]\s*['\"][^'\"]{8,}['\"]"
    )),
)
SKIP_PARTS = {".git", ".venv", "__pycache__"}


def scan(root: Path) -> list[tuple[str, str]]:
    """Return path-only findings without echoing matched text."""
    findings: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or SKIP_PARTS.intersection(path.parts):
            continue
        relative = path.relative_to(root).as_posix()
        suffix = path.suffix.lower()
        if suffix in FORBIDDEN_SUFFIXES:
            findings.append((relative, "binary_or_runtime_artifact"))
        if suffix not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            findings.append((relative, "unreadable_text_file"))
            continue
        for name, pattern in RULES:
            if pattern.search(text):
                findings.append((relative, name))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="staged tree to scan")
    args = parser.parse_args()
    if not args.root.is_dir():
        parser.error("root must be a directory")
    findings = scan(args.root)
    if findings:
        for path, rule in findings:
            print(f"{path}: {rule}")
        return 1
    print("No configured private-data patterns or runtime artifacts found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
