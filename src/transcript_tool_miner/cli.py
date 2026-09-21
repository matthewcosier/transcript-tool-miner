"""The public ttm command. Standard library only, with no network operations."""
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from . import __version__
from .export import markdown, package
from .miner import analyse
from .storage import candidates, connect, get_candidate, scan


def default_paths(claude=False, codex=False):
    home = Path.home()
    if not claude and not codex:
        claude = codex = True
    paths = []
    if claude:
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude"))
        paths.extend(p for p in (root / "projects", root / "history.jsonl") if p.exists())
    if codex:
        root = Path(os.environ.get("CODEX_HOME", home / ".codex"))
        paths.extend(p for p in (root / "sessions", root / "archived_sessions", root / "history.jsonl") if p.exists())
    return paths


def parser():
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--db", default=argparse.SUPPRESS, help="SQLite path (default: .ttm/miner.sqlite3)")
    root = argparse.ArgumentParser(prog="ttm", description="Find deterministic tool opportunities in local agent transcripts.", parents=[shared])
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="operation", required=True)
    ingest = commands.add_parser("scan", parents=[shared], help="Import transcripts incrementally")
    ingest.add_argument("paths", nargs="*", help="Explicit files/directories; overrides home discovery")
    ingest.add_argument("--claude", action="store_true")
    ingest.add_argument("--codex", action="store_true")
    ingest.add_argument("--source", choices=["claude", "codex"], help="Force adapter for explicit paths (default: detect)")
    ingest.add_argument("--matchers", help="JSON array of custom action/pattern rules")
    ingest.add_argument("--json", action="store_true", help="Machine-readable scan summary")
    analysis = commands.add_parser("analyse", aliases=["analyze"], parents=[shared], help="Mine and score repeated sequences")
    analysis.add_argument("--min-length", type=int, default=2)
    analysis.add_argument("--max-length", type=int, default=10)
    analysis.add_argument("--min-occurrences", type=int, default=2)
    analysis.add_argument("--min-sessions", type=int, default=2)
    analysis.add_argument("--result-budget", type=int, default=200, help="Conservative concise-result allowance in tokens")
    analysis.add_argument("--keep-nested", action="store_true", help="Include fully covered subpatterns")
    listing = commands.add_parser("candidates", parents=[shared], help="Show ranked opportunities")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--limit", type=int, default=20)
    detail = commands.add_parser("candidate", parents=[shared])
    actions = detail.add_subparsers(dest="candidate_operation", required=True)
    for name in ("show", "export"):
        action = actions.add_parser(name, parents=[shared])
        action.add_argument("id")
        action.add_argument("--format", choices=["markdown", "json"], default="markdown")
        action.add_argument("--include-sensitive", action="store_true", help="Include original paths, session IDs and unredacted commands; local review only")
        if name == "export":
            action.add_argument("--output", "-o", help="Write a new file instead of stdout (refuses overwrite)")
    return root


def write_private(path, text):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text + ("" if text.endswith("\n") else "\n"))


def main(argv=None):
    args = parser().parse_args(argv)
    db_path = getattr(args, "db", os.environ.get("TTM_DB", ".ttm/miner.sqlite3"))
    try:
        with connect(db_path) as db:
            if args.operation == "scan":
                paths = args.paths or default_paths(args.claude, args.codex)
                if not paths:
                    raise ValueError("No transcript sources found. Pass an explicit file or directory.")
                summary = scan(db, paths, source=args.source, matchers=args.matchers)
                if args.json:
                    print(json.dumps(summary, indent=2))
                else:
                    print(f"Imported {summary['imported']} files, {summary['actions']} actions; {summary['unchanged']} unchanged, {summary['duplicates']} duplicate, {summary['unsupported']} without supported actions.")
                    for warning in summary["warnings"]:
                        print(f"Warning: {warning}", file=sys.stderr)
                    for error in summary["errors"]:
                        print(f"Error: {error}", file=sys.stderr)
                    if summary["imported"]:
                        print("Run ttm analyse to refresh candidates.")
                return 1 if summary["errors"] else 0
            if args.operation in {"analyse", "analyze"}:
                result = analyse(db, **{key: getattr(args, key) for key in ("min_length", "max_length", "min_occurrences", "min_sessions", "result_budget", "keep_nested")})
                print(f"Found {len(result)} candidates. Run ttm candidates to see rankings.")
            elif args.operation == "candidates":
                if args.limit < 1:
                    raise ValueError("--limit must be positive")
                rows = candidates(db)[:args.limit]
                if args.json:
                    # Listing carries no representative trace data; still use export
                    # anonymisation so project paths do not escape by default.
                    print(json.dumps([package(row)["candidate"] for row in rows], indent=2))
                elif not rows:
                    print("No candidates. Run ttm scan then ttm analyse; by default a sequence must occur in two distinct sessions.")
                else:
                    print(f"{'ID':12}  {'Occurrences':>11}  {'Projects':>8}  {'Avg tokens~':>11}  {'Score~':>12}  Candidate")
                    for c in rows:
                        print(f"{c['id']}  {c['occurrences']:>11}  {c['project_count']:>8}  {c['average_intermediate_tokens']:>11,.0f}  {c['score']:>12,.0f}  {c['name']}")
                    print("~ Estimates, not measured savings. Scores and savings overlap across candidates.")
            else:
                bundle = package(get_candidate(db, args.id), args.include_sensitive)
                rendered = json.dumps(bundle, indent=2) if args.format == "json" else markdown(bundle)
                output = getattr(args, "output", None)
                if output:
                    write_private(output, rendered)
                    print(f"Exported to {output}. Review for sensitive data before sharing.")
                else:
                    print(rendered)
        return 0
    except (ValueError, OSError, sqlite3.Error, re.error, KeyError, TypeError) as error:
        print(f"ttm: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
