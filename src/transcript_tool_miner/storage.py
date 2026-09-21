"""SQLite persistence and incremental, file-level ingestion."""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from .normalize import Normalizer
from .parsers import PARSER_VERSION, parse

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
 id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, mtime_ns INTEGER NOT NULL,
 size INTEGER NOT NULL, digest TEXT NOT NULL, parser_version TEXT NOT NULL,
 session_id TEXT NOT NULL, source TEXT NOT NULL, project TEXT NOT NULL,
 timestamp TEXT NOT NULL, requests TEXT NOT NULL, warnings TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_digest ON sessions(digest, parser_version);
CREATE TABLE IF NOT EXISTS actions (
 id INTEGER PRIMARY KEY, session INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 position INTEGER NOT NULL, category TEXT NOT NULL, data TEXT NOT NULL,
 UNIQUE(session, position)
);
CREATE INDEX IF NOT EXISTS actions_session ON actions(session, position);
CREATE TABLE IF NOT EXISTS patterns (
 id TEXT PRIMARY KEY, score REAL NOT NULL, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS occurrences (
 pattern TEXT NOT NULL REFERENCES patterns(id) ON DELETE CASCADE,
 session INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 start INTEGER NOT NULL, end INTEGER NOT NULL,
 PRIMARY KEY(pattern, session, start)
);
"""


@contextmanager
def connect(path):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Private file creation before SQLite connects, so initial pages are not world-readable.
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 5000")
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1}:
            raise ValueError(f"Unsupported database schema version {version}")
        db.executescript(SCHEMA)
        db.execute("PRAGMA user_version = 1")
        yield db
    finally:
        db.close()


def fingerprint(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover(paths):
    found = set()
    for value in paths:
        path = Path(value).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"Input path does not exist: {path}")
        if path.is_file():
            found.add(path)
        else:
            for suffix in ("*.jsonl", "*.json"):
                found.update(p for p in path.rglob(suffix) if p.is_file() and not p.is_symlink())
    return sorted(found)


def scan(db, paths, source=None, matchers=None):
    normalizer = Normalizer(matchers)
    version = PARSER_VERSION + ":" + (fingerprint(Path(matchers)) if matchers else "builtin") + ":" + str(source)
    files = discover(paths)
    summary = dict(discovered=len(files), imported=0, unchanged=0, duplicates=0,
                   unsupported=0, actions=0, warnings=[], errors=[])
    with db:
        for path in files:
            try:
                before = path.stat()
                old = db.execute("SELECT * FROM sessions WHERE path = ?", (str(path),)).fetchone()
                if old and (old["mtime_ns"], old["size"], old["parser_version"]) == (before.st_mtime_ns, before.st_size, version):
                    summary["unchanged"] += 1
                    continue
                digest = fingerprint(path)
                if old and old["digest"] == digest and old["parser_version"] == version:
                    db.execute("UPDATE sessions SET mtime_ns = ?, size = ? WHERE id = ?", (before.st_mtime_ns, before.st_size, old["id"]))
                    summary["unchanged"] += 1
                    continue
                duplicate = db.execute("SELECT id FROM sessions WHERE digest = ? AND parser_version = ? AND path != ?", (digest, version, str(path))).fetchone()
                if duplicate and not old:
                    summary["duplicates"] += 1
                    continue
                session = parse(path, normalizer, source)
                after = path.stat()
                if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                    summary["warnings"].append(f"{path.name}: changed while reading; retry scan after writing finishes")
                    continue
                if not session.actions and not old:
                    summary["unsupported"] += 1
                    summary["warnings"].extend(f"{path.name}: {w}" for w in session.warnings)
                    continue
                if session.source == "unknown" or (old and not session.actions and session.warnings):
                    summary["errors"].append(f"{path.name}: cannot safely replace previous import")
                    continue
                # Import and invalidate derived patterns together, in one transaction.
                db.execute("DELETE FROM patterns")
                db.execute("DELETE FROM metadata WHERE key = 'analysis'")
                if old:
                    db.execute("DELETE FROM sessions WHERE id = ?", (old["id"],))
                cursor = db.execute("""INSERT INTO sessions
                    (path,mtime_ns,size,digest,parser_version,session_id,source,project,timestamp,requests,warnings)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (str(path), before.st_mtime_ns, before.st_size, digest, version,
                    session.session_id, session.source, session.project, session.timestamp,
                    json.dumps(session.requests), json.dumps(session.warnings)))
                for position, action in enumerate(session.actions):
                    db.execute("INSERT INTO actions(session,position,category,data) VALUES(?,?,?,?)",
                               (cursor.lastrowid, position, action.category, json.dumps(asdict(action))))
                summary["imported"] += 1
                summary["actions"] += len(session.actions)
                summary["warnings"].extend(f"{path.name}: {w}" for w in session.warnings)
            except (OSError, UnicodeError) as error:
                summary["errors"].append(f"{path.name}: {type(error).__name__}")
    return summary


def load_sessions(db):
    for row in db.execute("SELECT * FROM sessions ORDER BY id"):
        item = dict(row)
        item["actions"] = [json.loads(a["data"]) for a in db.execute("SELECT data FROM actions WHERE session = ? ORDER BY position", (row["id"],))]
        yield item


def candidates(db):
    return [json.loads(row["data"]) for row in db.execute("SELECT data FROM patterns ORDER BY score DESC, id")]


def get_candidate(db, candidate_id):
    row = db.execute("SELECT data FROM patterns WHERE id = ?", (candidate_id,)).fetchone()
    if not row:
        raise ValueError(f"Candidate {candidate_id} not found; run ttm analyse and ttm candidates")
    candidate = json.loads(row["data"])
    examples = []
    seen = set()
    for occurrence in db.execute("""SELECT o.*, s.session_id, s.source, s.project, s.path FROM occurrences o
        JOIN sessions s ON s.id = o.session WHERE pattern = ? ORDER BY session, start""", (candidate_id,)):
        # Prefer distinct sessions, then fill with additional spans if necessary.
        example = dict(occurrence)
        key = (example["source"], example["session_id"])
        if key in seen:
            continue
        seen.add(key)
        example["actions"] = [json.loads(row["data"]) for row in db.execute(
            "SELECT data FROM actions WHERE session = ? AND position BETWEEN ? AND ? ORDER BY position",
            (example["session"], example["start"], example["end"]))]
        examples.append(example)
        if len(examples) == 3:
            break
    candidate["examples"] = examples
    return candidate
