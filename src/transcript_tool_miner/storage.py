"""Private SQLite persistence with a compact mining path and bounded ingestion."""
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from .normalize import Normalizer, hash_text
from .parsers import PARSER_VERSION, parse
from .usage import SCHEMA as USAGE_SCHEMA, save_usage

SCHEMA = '''
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
 id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, mtime_ns INTEGER NOT NULL,
 size INTEGER NOT NULL, digest TEXT NOT NULL, parser_version TEXT NOT NULL,
 session_id TEXT NOT NULL, source TEXT NOT NULL, project TEXT NOT NULL,
 timestamp TEXT NOT NULL, requests TEXT NOT NULL, warnings TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
 id INTEGER PRIMARY KEY, session INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 position INTEGER NOT NULL, category TEXT NOT NULL, data TEXT NOT NULL,
 UNIQUE(session, position)
);
CREATE INDEX IF NOT EXISTS actions_session ON actions(session, position);
CREATE INDEX IF NOT EXISTS sessions_digest ON sessions(digest, parser_version);
CREATE TABLE IF NOT EXISTS sources (
 path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL,
 digest TEXT NOT NULL, parser_version TEXT NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patterns (id TEXT PRIMARY KEY, score REAL NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS occurrences (
 pattern TEXT NOT NULL REFERENCES patterns(id) ON DELETE CASCADE,
 session INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 start INTEGER NOT NULL, end INTEGER NOT NULL,
 PRIMARY KEY(pattern, session, start)
);
'''
ACTION_COLUMNS = {'event_key': 'TEXT', 'compact': 'TEXT', 'request_no': 'INTEGER', 'turn_no': 'INTEGER',
                  'output_tokens': 'INTEGER', 'assistant_tokens': 'INTEGER', 'success': 'INTEGER',
                  'compact_output_tokens': 'INTEGER', 'linked_to': 'TEXT', 'poll_reduction_tokens': 'INTEGER DEFAULT 0', 'repeat_output_reduction_tokens': 'INTEGER DEFAULT 0'}


@contextmanager
def connect(path):
    path = Path(path).expanduser()
    if path.is_symlink(): raise ValueError('Database path must not be a symlink')
    if path.exists() and path.stat().st_size:
        with path.open('rb') as stream:
            if stream.read(16) != b'SQLite format 3\0':
                raise ValueError('Database path is an existing non-SQLite file; refusing to modify it')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600); os.close(fd)
    os.chmod(path, 0o600)
    db = sqlite3.connect(path); db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON'); db.execute('PRAGMA busy_timeout = 5000')
    db.execute('PRAGMA temp_store = FILE')
    try:
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version not in {0, 1, 2}: raise ValueError(f'Unsupported database schema version {version}')
        db.executescript(SCHEMA + USAGE_SCHEMA)
        with db:
            existing = {row['name'] for row in db.execute('PRAGMA table_info(actions)')}
            for name, kind in ACTION_COLUMNS.items():
                if name not in existing: db.execute(f'ALTER TABLE actions ADD COLUMN {name} {kind}')
            existing = {row['name'] for row in db.execute('PRAGMA table_info(sessions)')}
            for name in ('project_key', 'project_kind'):
                if name not in existing: db.execute(f'ALTER TABLE sessions ADD COLUMN {name} TEXT')
            if version == 1:
                db.execute('DELETE FROM patterns')
                db.execute("DELETE FROM metadata WHERE key IN ('analysis','model_call_audit')")
            db.execute('PRAGMA user_version = 2')
        yield db
    finally: db.close()


def fingerprint(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''): digest.update(chunk)
    return digest.hexdigest()


def discover(paths):
    found = set()
    for value in paths:
        path = Path(value).expanduser().resolve()
        if not path.exists(): raise ValueError(f'Input path does not exist: {path}')
        if path.is_file(): found.add(path)
        else:
            for p in path.rglob('*'):
                if p.suffix in {'.jsonl', '.json'} and p.is_file() and not p.is_symlink(): found.add(p)
    return sorted(found)


@lru_cache(maxsize=8192)
def project_identity(cwd):
    if not cwd or cwd == 'unknown': return 'unknown', 'unknown'
    path = Path(cwd)
    if path.is_dir():
        for parent in (path, *path.parents):
            marker = parent / '.git'
            try:
                if marker.is_dir(): return str(marker.resolve()), 'git'
                if marker.is_file():
                    text = marker.read_text().strip()
                    if text.startswith('gitdir:'):
                        gitdir = (parent / text[7:].strip()).resolve()
                        common = gitdir / 'commondir'
                        return str((gitdir / common.read_text().strip()).resolve() if common.is_file() else gitdir), 'git'
            except (OSError, UnicodeError): break
    return str(path), 'cwd'


_WORKER_NORMALIZERS = {}


def _parse_file(job):
    path, source, matchers = job
    try:
        if matchers not in _WORKER_NORMALIZERS: _WORKER_NORMALIZERS[matchers] = Normalizer(matchers)
        normalizer = _WORKER_NORMALIZERS[matchers]
        return parse(path, normalizer, source), None
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as error:
        return None, type(error).__name__


def _parse_jobs(jobs, workers):
    if workers == 1:
        for job in jobs: yield job, _parse_file(job)
        return
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import get_context
    with ProcessPoolExecutor(max_workers=workers, mp_context=get_context('spawn')) as pool:
        pending = deque(); iterator = iter(jobs)
        for _ in range(workers * 2):
            job = next(iterator, None)
            if job is None: break
            pending.append((job, pool.submit(_parse_file, job)))
        while pending:
            job, future = pending.popleft(); yield job, future.result()
            job = next(iterator, None)
            if job is not None: pending.append((job, pool.submit(_parse_file, job)))


def scan(db, paths, source=None, matchers=None, workers=1):
    if not 1 <= workers <= 8: raise ValueError('--workers must be between 1 and 8')
    Normalizer(matchers)  # Validate local configuration before starting workers.
    _WORKER_NORMALIZERS.clear(); project_identity.cache_clear()
    version = PARSER_VERSION + ':' + (fingerprint(matchers) if matchers else 'builtin') + ':' + str(source)
    files = discover(paths)
    summary = dict(discovered=len(files), imported=0, unchanged=0, duplicates=0, unsupported=0,
                   actions=0, warnings=[], errors=[])
    jobs = []; before = {}
    for path in files:
        try:
            stat = path.stat(); before[str(path)] = stat
            old = db.execute('SELECT * FROM sources WHERE path=?', (str(path),)).fetchone()
            if old and (old['mtime_ns'], old['size'], old['parser_version']) == (stat.st_mtime_ns, stat.st_size, version):
                summary['unchanged'] += 1; continue
            jobs.append((str(path), source, str(Path(matchers).resolve()) if matchers else None))
        except OSError as error: summary['errors'].append(f'{path.name}: {type(error).__name__}')
    invalidated = False
    with db:
        for (value, _, _), (session, error) in _parse_jobs(jobs, workers):
            path = Path(value); stat = before[value]
            if error:
                summary['errors'].append(f'{path.name}: {error}'); continue
            try: after = path.stat()
            except OSError:
                summary['errors'].append(f'{path.name}: disappeared while reading'); continue
            if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                summary['warnings'].append(f'{path.name}: changed while reading; retry later'); continue
            old = db.execute('SELECT * FROM sessions WHERE path=?', (value,)).fetchone()
            if old and session.warnings:
                summary['errors'].append(f'{path.name}: malformed records; previous import retained'); continue
            digest = session.digest
            if old and old['digest'] == digest and old['parser_version'] == version:
                summary['unchanged'] += 1; status = 'imported'
            else:
                duplicate = db.execute('SELECT id FROM sessions WHERE digest=? AND parser_version=? AND path!=?', (digest, version, value)).fetchone()
                status = 'duplicate' if duplicate else 'imported' if session.actions else 'unsupported'
                if old or status == 'imported':
                    if not invalidated:
                        db.execute('DELETE FROM patterns'); db.execute("DELETE FROM metadata WHERE key IN ('analysis','model_call_audit')"); invalidated = True
                    if old: db.execute('DELETE FROM sessions WHERE id=?', (old['id'],))
                if status == 'imported':
                    project_key, project_kind = project_identity(session.project)
                    cursor = db.execute('''INSERT INTO sessions
                        (path,mtime_ns,size,digest,parser_version,session_id,source,project,timestamp,requests,warnings,project_key,project_kind)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''', (value, stat.st_mtime_ns, stat.st_size, digest, version, session.session_id,
                        session.source, session.project, session.timestamp, json.dumps(session.requests), json.dumps(session.warnings), project_key, project_kind))
                    session_id = cursor.lastrowid
                    def action_rows():
                        for position, action in enumerate(session.actions):
                            identity = action.call_id if len(action.call_id) >= 20 else f'{session.session_id}|{action.call_id}|{position}'
                            event_key = hash_text(session.source + '|' + identity + '|' + action.command)
                            compact = [{key: op[key] for key in ('label', 'template', 'exact')} for op in action.operations]
                            yield (session_id, position, action.category, json.dumps(asdict(action)), event_key, json.dumps(compact),
                                   action.request, action.turn, action.output_tokens, action.assistant_tokens, action.success,
                                   action.compact_output_tokens, action.linked_to, action.poll_reduction_tokens, action.repeat_output_reduction_tokens)
                    db.executemany('INSERT INTO actions(session,position,category,data,event_key,compact,request_no,turn_no,'
                                   'output_tokens,assistant_tokens,success,compact_output_tokens,linked_to,poll_reduction_tokens,repeat_output_reduction_tokens) '
                                   'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', action_rows())
                    summary['imported'] += 1; summary['actions'] += len(session.actions)
                    save_usage(db, session_id, session)
                    session.actions.clear()
                else: summary['duplicates' if status == 'duplicate' else 'unsupported'] += 1
            db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?,?,?)', (value, stat.st_mtime_ns, stat.st_size, digest, version, status))
            summary['warnings'].extend(f'{path.name}: {warning}' for warning in session.warnings)
    return summary


def prepare_mining(db):
    if db.execute('SELECT 1 FROM actions WHERE compact IS NULL LIMIT 1').fetchone():
        raise ValueError('This database contains v1 actions. Run ttm scan on its source paths to upgrade them before analysing.')
    if db.execute('SELECT 1 FROM sessions WHERE parser_version NOT LIKE ? LIMIT 1', (PARSER_VERSION + ':%',)).fetchone():
        raise ValueError('Parser version changed. Rescan the source paths before analysing this database.')
    db.execute('DROP TABLE IF EXISTS temp.mining_events')
    db.execute('CREATE TEMP TABLE mining_events(event_key TEXT PRIMARY KEY, action_id INTEGER UNIQUE) WITHOUT ROWID')
    db.execute('INSERT OR IGNORE INTO mining_events SELECT event_key,id FROM actions ORDER BY (success IS NOT NULL) DESC,output_tokens DESC,id')
    db.commit()


def compact_sessions(db):
    for row in db.execute('SELECT id,session_id,source,project,project_key,project_kind FROM sessions ORDER BY id'):
        item = dict(row)
        item['actions'] = db.execute('''SELECT a.id,a.position,a.request_no AS request,a.turn_no AS turn,a.compact,
            a.output_tokens,a.assistant_tokens,a.success,a.compact_output_tokens,a.linked_to,a.poll_reduction_tokens,a.repeat_output_reduction_tokens,
            (m.action_id IS NULL) AS replayed FROM actions a LEFT JOIN mining_events m ON m.action_id=a.id
            WHERE a.session=? ORDER BY a.position''', (row['id'],))
        yield item


def load_sessions(db):
    """Compatibility/debug reader. Mining uses compact_sessions, never raw data."""
    for row in db.execute('SELECT * FROM sessions ORDER BY id'):
        item = dict(row)
        item['actions'] = [json.loads(a['data']) for a in db.execute('SELECT data FROM actions WHERE session=? ORDER BY position', (row['id'],))]
        yield item


def candidates(db):
    return [json.loads(row['data']) for row in db.execute("SELECT data FROM patterns ORDER BY score DESC,json_extract(data,'$.opportunity_type') DESC,id")]


def get_candidate(db, candidate_id):
    row = db.execute('SELECT data FROM patterns WHERE id=?', (candidate_id,)).fetchone()
    if not row: raise ValueError(f'Candidate {candidate_id} not found; run ttm analyse and ttm candidates')
    candidate = json.loads(row['data']); examples = []; seen = set()
    for row in db.execute('''SELECT o.*,s.session_id,s.source,s.project,s.path FROM occurrences o JOIN sessions s ON s.id=o.session
                            WHERE pattern=? ORDER BY session,start''', (candidate_id,)):
        example = dict(row); key = (example['source'], example['session_id'])
        if key in seen: continue
        seen.add(key)
        example['actions'] = [json.loads(a['data']) for a in db.execute('SELECT data FROM actions WHERE session=? AND position BETWEEN ? AND ? ORDER BY position', (example['session'], example['start'], example['end']))]
        examples.append(example)
        if len(examples) == 3: break
    candidate['examples'] = examples
    return candidate
