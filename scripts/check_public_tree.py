#!/usr/bin/env python3
"""Reject private artifacts in the Git index; never reads local transcript folders."""
import json
from pathlib import PurePosixPath
import subprocess
import sys


def check():
    paths = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
    failures = []
    forbidden_dirs = {".ttm", "transcripts", "sessions", "history", "imports", "private", "exports", "reports", "scan-output", ".venv"}
    for name in filter(None, paths):
        path = PurePosixPath(name)
        lower = name.lower()
        if (set(path.parts) & forbidden_dirs or ".jsonl" in lower or
                any(ending in lower for ending in (".sqlite", ".db")) or
                path.name.startswith(".env") or path.suffix in {".pem", ".key"}):
            failures.append(name + ": private artifact type")
        if path.parent == PurePosixPath("tests/fixtures"):
            # Read the staged bytes, not an unstaged file that could mask them.
            raw = subprocess.check_output(["git", "show", ":" + name])
            try:
                data = json.loads(raw)
                if not isinstance(data, list) or not data or data[0].get("_synthetic_fixture") is not True:
                    raise ValueError("fixture is not marked synthetic")
            except (ValueError, TypeError, AttributeError):
                failures.append(name + ": fixture must be explicitly synthetic JSON")
    if failures:
        print("Public-tree check failed:\n" + "\n".join(failures), file=sys.stderr)
        return 1
    print(f"Public-tree check passed ({len(list(filter(None, paths)))} tracked files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(check())
