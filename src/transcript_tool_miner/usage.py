"""Recorded API usage on narrowly defined, potentially removable model calls.

These are gross historical call costs under a fixed-plan counterfactual. They
are NOT added to payload reductions, and are not a claim about net billing.
"""
from collections import defaultdict
import json
from pathlib import Path
from .normalize import hash_text, Normalizer

EXTRACTOR_VERSION = '1'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS model_usage (
 session INTEGER REFERENCES sessions(id) ON DELETE CASCADE, turn INTEGER, usage_key TEXT,
 input_tokens INTEGER,cached_input_tokens INTEGER,cache_write_input_tokens INTEGER,
 output_tokens INTEGER,visible_text INTEGER,trusted INTEGER,empty_poll INTEGER,
 PRIMARY KEY(session,turn));
CREATE TABLE IF NOT EXISTS usage_sources (
 session INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,digest TEXT,version TEXT);
'''


class UsageCollector:
    def __init__(self):
        self.turns = defaultdict(lambda: {'calls': [], 'visible': False})
        self.previous_total = None

    def tool(self, turn, call_id):
        self.turns[turn]['calls'].append(call_id)

    def text(self, turn, text):
        if text.strip(): self.turns[turn]['visible'] = True

    def observe(self, record, turn):
        if not turn: return
        entry = self.turns[turn]
        message = record.get('message')
        if record.get('type') == 'assistant' and isinstance(message, dict) and isinstance(message.get('usage'), dict):
            usage = message['usage']
            keys = ('input_tokens','cache_read_input_tokens','cache_creation_input_tokens','output_tokens')
            values = [usage.get(k,0) for k in keys]
            if any(type(v) is not int or v < 0 for v in values): return
            fresh, cached, written, output = values
            value = (fresh+cached+written,cached,written,output)
            if 'usage' in entry and entry['usage'][:3] != value[:3]: entry['trusted'] = False
            else: entry.setdefault('trusted', True)
            if 'usage' in entry: value = (*value[:3],max(output,entry['usage'][3]))
            entry['usage'] = value; entry['provider'] = 'claude'
            entry['response'] = message.get('id', '')
        payload = record.get('payload')
        if record.get('type') != 'event_msg' or not isinstance(payload, dict) or payload.get('type') != 'token_count': return
        info = payload.get('info') or {}
        if not isinstance(info, dict): return
        last = info.get('last_token_usage'); total = info.get('total_token_usage')
        if not isinstance(last, dict) or not isinstance(total, dict): return
        keys = ('input_tokens','cached_input_tokens','output_tokens')
        if any(type(last.get(k)) is not int or type(total.get(k)) is not int for k in keys): return
        current = tuple(total[k] for k in keys)
        if current == self.previous_total: return
        delta = current if self.previous_total is None else tuple(a-b for a,b in zip(current,self.previous_total))
        self.previous_total = current
        valid = delta == tuple(last[k] for k in keys) and 0 <= last['cached_input_tokens'] <= last['input_tokens']
        if 'usage' in entry: valid = False  # Multiple changing counters cannot be attributed to one observed turn.
        entry['trusted'] = valid
        entry['usage'] = (last['input_tokens'],last['cached_input_tokens'],last.get('cache_write_input_tokens',0),last['output_tokens'])
        entry['provider'] = 'codex'

    def rows(self, actions):
        by_turn = defaultdict(list); by_call = {a.call_id:a for a in actions}
        for action in actions: by_turn[action.turn].append(action)
        rows = []
        for turn, entry in self.turns.items():
            if not entry.get('calls') or 'usage' not in entry: continue
            values = entry['usage']
            if any(type(v) is not int or v < 0 for v in values): continue
            if values[1]+values[2] > values[0]: continue
            actions_here = by_turn[turn]
            empty = bool(actions_here) and all(a.empty_poll and a.linked_to in by_call and by_call[a.linked_to].completion_observed for a in actions_here)
            identity = entry.get('response') or '|'.join(sorted(entry['calls']))
            rows.append((turn,hash_text(entry['provider']+'|'+identity),*values,int(entry['visible']),int(entry.get('trusted',False)),int(empty)))
        return rows


def save_usage(db, session_id, session):
    db.execute('DELETE FROM model_usage WHERE session=?', (session_id,))
    db.executemany('INSERT INTO model_usage VALUES(?,?,?,?,?,?,?,?,?,?)', [(session_id,*row) for row in session.turn_usage])
    db.execute('INSERT OR REPLACE INTO usage_sources VALUES(?,?,?)',(session_id,session.digest,EXTRACTOR_VERSION))


def audit_model_calls(db):
    from .parsers import parse
    db.executescript(SCHEMA)
    # Only source files containing the relevant proposals need usage enrichment.
    sources = db.execute('''SELECT DISTINCT s.* FROM sessions s JOIN occurrences o ON o.session=s.id JOIN patterns p ON p.id=o.pattern
        WHERE json_extract(p.data,'$.opportunity_type')='workflow_bundle' OR json_extract(p.data,'$.family')='await_completion'
        ORDER BY s.id''').fetchall()
    read = 0; skipped = 0
    with db:
        for source in sources:
            existing = db.execute('SELECT * FROM usage_sources WHERE session=?',(source['id'],)).fetchone()
            if existing and existing['digest']==source['digest'] and existing['version']==EXTRACTOR_VERSION: continue
            path = Path(source['path'])
            try:
                stat = path.stat()
                if (stat.st_size,stat.st_mtime_ns)!=(source['size'],source['mtime_ns']): skipped+=1;continue
                session = parse(path,Normalizer())
                if session.digest!=source['digest']: skipped+=1;continue
                save_usage(db,source['id'],session);read+=1
            except (OSError,ValueError,TypeError): skipped+=1
        # A stable plan requires the same exact operation arguments in multiple
        # distinct sessions. Parameter-template similarity alone is insufficient.
        db.executescript('''DROP TABLE IF EXISTS temp.exact_plans;
            CREATE TEMP TABLE exact_plans(session INT,logical TEXT,start INT,end INT,signature TEXT,first_turn INT);
            DROP TABLE IF EXISTS temp.call_exposure;
            CREATE TEMP TABLE call_exposure(usage_key TEXT PRIMARY KEY,reason TEXT,input INT,cached INT,written INT,output INT);''')
        for row in db.execute('''SELECT o.*,s.source,s.session_id FROM occurrences o JOIN patterns p ON p.id=o.pattern JOIN sessions s ON s.id=o.session
            WHERE json_extract(p.data,'$.family')='validation_report' '''):
            actions = db.execute('SELECT position,turn_no,compact,success,linked_to FROM actions WHERE session=? AND position BETWEEN ? AND ? ORDER BY position',
                                 (row['session'],row['start'],row['end'])).fetchall()
            meaningful = [a for a in actions if not a['linked_to']]
            if not meaningful or any(a['success']!=1 for a in meaningful): continue
            signature = hash_text('|'.join(op['exact'] for a in meaningful for op in json.loads(a['compact'])))
            db.execute('INSERT INTO exact_plans VALUES(?,?,?,?,?,?)',(row['session'],row['source']+':'+row['session_id'],row['start'],row['end'],signature,min(a['turn_no'] for a in meaningful)))
        eligible_plans = db.execute('''SELECT e.* FROM exact_plans e WHERE signature IN
            (SELECT signature FROM exact_plans GROUP BY signature HAVING count(DISTINCT logical)>=2)''').fetchall()
        for plan in eligible_plans:
            turns = db.execute('''SELECT DISTINCT turn_no FROM actions WHERE session=? AND position BETWEEN ? AND ? AND turn_no>?''',
                               (plan['session'],plan['start'],plan['end'],plan['first_turn'])).fetchall()
            for (turn,) in turns:
                outside = db.execute('SELECT 1 FROM actions WHERE session=? AND turn_no=? AND (position<? OR position>?) LIMIT 1',
                                     (plan['session'],turn,plan['start'],plan['end'])).fetchone()
                if outside: continue
                db.execute('''INSERT OR IGNORE INTO call_exposure SELECT usage_key,'fixed_validation_plan',input_tokens,cached_input_tokens,cache_write_input_tokens,output_tokens
                    FROM model_usage WHERE session=? AND turn=? AND trusted=1 AND visible_text=0''',(plan['session'],turn))
        # Empty process polling is independently deterministic. Preserve terminal
        # results and any turn with commentary or unrelated actions.
        for source in sources:
            db.execute('''INSERT OR IGNORE INTO call_exposure SELECT usage_key,'empty_process_polling',input_tokens,cached_input_tokens,cache_write_input_tokens,output_tokens
                FROM model_usage WHERE session=? AND trusted=1 AND visible_text=0 AND empty_poll=1''',(source['id'],))
        by_reason = []
        for row in db.execute('SELECT reason,count(*) calls,sum(input) input,sum(cached) cached,sum(written) written,sum(output) output FROM call_exposure GROUP BY reason'):
            by_reason.append({'reason':row['reason'],'model_calls':row['calls'],'recorded_input_tokens':row['input'],
                'cached_input_tokens':row['cached'],'cache_write_input_tokens':row['written'],
                'uncached_input_tokens':row['input']-row['cached']-row['written'],'recorded_output_tokens':row['output']})
        totals = {key:sum(row[key] for row in by_reason) for key in ('model_calls','recorded_input_tokens','cached_input_tokens','cache_write_input_tokens','uncached_input_tokens','recorded_output_tokens')}
        result = {'scope':'Recorded API usage on tool-only turns inside exact repeated successful validation plans, or empty polls of observed completed processes.',
                  'sources_considered':len(sources),'sources_read_for_usage':read,'sources_skipped_changed_or_unavailable':skipped,
                  'repeated_exact_plan_occurrences':len(eligible_plans),'gross_historical_call_exposure':totals,'by_reason':by_reason,
                  'net_savings_measured':False,
                  'limits':['These are the historical costs of calls a proposed tool could remove, not measured net savings.',
                            'Cached, cache-write and uncached input are separated; token counts are not equivalent to monetary cost.',
                            'No commentary or unrelated sibling tool calls may be removed by this calculation.',
                            'New tool definitions, larger initial arguments, retries, and changed model behaviour are not priced.',
                            'Do not add call-exposure totals to tool-output reductions: they overlap.']}
        db.execute("INSERT OR REPLACE INTO metadata VALUES('model_call_audit',?)",(json.dumps(result),))
    return result
