"""Stream compact events into disjoint workflow families and replay-backed reducers."""
from collections import Counter
from datetime import datetime, timezone
import json
from .normalize import VALIDATION, REPOSITORY, hash_text
from .storage import compact_sessions, prepare_mining

TITLES = {'reuse_results': 'reuse_unchanged_results', 'await_completion': 'await_process_completion', 'validation_report': 'validate_and_report', 'validation_output': 'summarize_validation_results',
          'ci_report': 'collect_ci_status', 'repository_report': 'summarize_repository_state',
          'context_review': 'review_context_collection'}
WORKFLOW_LABELS = VALIDATION | REPOSITORY | {'github_pr_checks', 'github_ci_status'}


def family(labels):
    if any(label in VALIDATION for label in labels): return 'validation_report'
    if any(label.startswith('github_') for label in labels): return 'ci_report'
    return 'repository_report'


def _stage(db, session, name, nodes, diagnostics):
    actions = {node['id']: node for node in nodes}
    sequence = [node['label'] for node in nodes]
    turns = len({node['turn'] for node in nodes})
    if name not in {'validation_output', 'await_completion', 'reuse_results'} and (len(actions) < 2 or turns < 2):
        diagnostics['already_batched_episodes'] += 1; return
    if name not in {'validation_output', 'await_completion', 'reuse_results'} and len(set(sequence)) < 2:
        diagnostics['homogeneous_episodes'] += 1; return
    output = sum(a['output_tokens'] for a in actions.values())
    status_reduction = sum(max(0, a['output_tokens'] - a['compact_output_tokens']) if a['compact_output_tokens'] is not None else 0 for a in actions.values()) if name not in {'await_completion','reuse_results'} else 0
    poll_reduction = sum(a.get('poll_reduction_tokens', 0) for a in actions.values()) if name not in {'validation_output','reuse_results'} else 0
    reuse_reduction = sum(a.get('repeat_output_reduction_tokens', 0) for a in actions.values()) if name == 'reuse_results' else 0
    reduction = status_reduction + poll_reduction + reuse_reduction
    known = all(a['success'] is not None for a in actions.values())
    failed = any(a['success'] == 0 for a in actions.values())
    logical = session['source'] + ':' + session['session_id']
    db.execute('INSERT INTO episode_stage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (name, session['id'], logical, session['project_key'], session['project_kind'],
                min(a['position'] for a in actions.values()), max(a['position'] for a in actions.values()),
                json.dumps(sequence), hash_text('|'.join(n['template'] for n in nodes)), hash_text('|'.join(n['exact'] for n in nodes)),
                turns, output, reduction, int(known), int(failed), sum(a['assistant_tokens'] for a in actions.values())))
    for action in actions.values():
        status = max(0, action['output_tokens']-action['compact_output_tokens']) if action['compact_output_tokens'] is not None and name not in {'await_completion','reuse_results'} else 0
        poll = action.get('poll_reduction_tokens', 0) if name not in {'validation_output','reuse_results'} else 0
        reuse = action.get('repeat_output_reduction_tokens', 0) if name == 'reuse_results' else 0
        for mechanism, amount in (('validation_status', status), ('empty_poll', poll), ('unchanged_result', reuse)):
            if amount:
                db.execute('INSERT INTO member_stage VALUES(?,?,?,?) ON CONFLICT(family,action,mechanism) DO UPDATE SET reduction=max(reduction,excluded.reduction)',
                           (name, action['id'], mechanism, amount))


def analyse(db, min_length=2, max_length=10, min_occurrences=2, min_sessions=2,
            result_budget=200, keep_nested=False, include_exploratory=False):
    if not 2 <= min_length <= max_length <= 10: raise ValueError('Sequence lengths must satisfy 2 <= min <= max <= 10')
    if min_occurrences < 2 or min_sessions < 1 or result_budget < 0: raise ValueError('Invalid occurrence/session/result-budget setting')
    prepare_mining(db)
    db.executescript('''
      DROP TABLE IF EXISTS temp.episode_stage;
      DROP TABLE IF EXISTS temp.member_stage;
      CREATE TEMP TABLE episode_stage(family TEXT,session INT,logical TEXT,project TEXT,project_kind TEXT,
        start INT,end INT,sequence TEXT,signature TEXT,exact TEXT,turns INT,output INT,reduction INT,known INT,failed INT,assistant INT);
      CREATE TEMP TABLE member_stage(family TEXT,action INT,mechanism TEXT,reduction INT,PRIMARY KEY(family,action,mechanism)) WITHOUT ROWID;
      DROP TABLE IF EXISTS candidate_actions;
      CREATE TABLE candidate_actions(pattern TEXT REFERENCES patterns(id) ON DELETE CASCADE,
        action INTEGER REFERENCES actions(id) ON DELETE CASCADE,mechanism TEXT NOT NULL,reduction INTEGER NOT NULL,PRIMARY KEY(pattern,action,mechanism));
    ''')
    diagnostics = Counter()
    for session in compact_sessions(db):
        run = []; request = None
        def flush():
            if len(run) >= min_length:
                _stage(db, session, family([n['label'] for n in run]), run, diagnostics)
            run.clear()
        for row in session['actions']:
            action = dict(row)
            if action['replayed']:
                diagnostics['replayed_actions_excluded'] += 1; flush(); continue
            diagnostics['unique_actions'] += 1
            diagnostics['observed_tool_output_tokens'] += action['output_tokens']
            operations = json.loads(action['compact'])
            if action.get('repeat_output_reduction_tokens', 0):
                reuse_node = dict(action, label='reuse_unchanged_result', template='identical-read-result', exact='identical-read-result')
                _stage(db, session, 'reuse_results', [reuse_node], diagnostics)
            if action.get('poll_reduction_tokens', 0):
                poll_node = dict(action, label='await_completion', template='linked-process-completion', exact='linked-process-completion')
                _stage(db, session, 'await_completion', [poll_node], diagnostics)
            # Budget is a minimum output allowance, in addition to the actual
            # replayed reducer size. It cannot invent a reduction on small logs.
            if action['compact_output_tokens'] is not None:
                action['compact_output_tokens'] = max(result_budget, action['compact_output_tokens'])
            if request is not None and action['request'] != request: flush()
            request = action['request']
            if action['linked_to'] and all(op['label'] == 'poll' for op in operations):
                diagnostics['linked_polls_folded'] += 1; continue
            if len(operations) > max_length or any(op['label'] == 'unknown' for op in operations):
                diagnostics['opaque_or_oversized_batches'] += 1; flush(); continue
            if run and len(run) + len(operations) > max_length: flush()
            if len(operations) == 1 and action['compact_output_tokens'] is not None and action['output_tokens'] > action['compact_output_tokens']:
                _stage(db, session, 'validation_output', [dict(action, **operations[0])], diagnostics)
            for operation in operations:
                label = operation['label']
                if label not in WORKFLOW_LABELS:
                    diagnostics['content_or_unknown_boundaries'] += 1; flush(); continue
                run.append(dict(action, **operation))
                if len(run) >= max_length: flush()
        flush()
    db.execute('CREATE INDEX episode_family ON episode_stage(family)')
    results = []
    with db:
        db.execute('DELETE FROM patterns')
        db.execute("DELETE FROM metadata WHERE key='model_call_audit'")
        for (name,) in db.execute('SELECT DISTINCT family FROM episode_stage'):
            stats = db.execute('''SELECT count(*) n,count(DISTINCT logical) sessions,avg(turns) turns,avg(output) output,
                sum(reduction) reduction,avg(known) known,sum(failed) failed,sum(known AND NOT failed) success
                FROM episode_stage WHERE family=?''', (name,)).fetchone()
            if stats['n'] < min_occurrences or stats['sessions'] < min_sessions: continue
            # Recurrence is measured across distinct sessions with the same
            # parameterised operation interface, not inferred from category names.
            recurring = db.execute('''SELECT coalesce(sum(n),0) FROM
                (SELECT count(*) n FROM episode_stage WHERE family=? GROUP BY signature HAVING count(DISTINCT logical)>=2)''', (name,)).fetchone()[0]
            recurrence = recurring / stats['n']
            score = stats['reduction'] * recurrence
            if score <= 0 and not include_exploratory: continue
            variants = [{'sequence': json.loads(row['sequence']), 'occurrences': row['n']} for row in db.execute(
                'SELECT sequence,count(*) n FROM episode_stage WHERE family=? GROUP BY sequence ORDER BY n DESC,sequence LIMIT 8', (name,))]
            projects = db.execute('''SELECT count(DISTINCT CASE WHEN project_kind='git' THEN project END) repos,
                count(DISTINCT CASE WHEN project_kind!='git' THEN project END) unresolved FROM episode_stage WHERE family=?''', (name,)).fetchone()
            directories = [row[0] for row in db.execute('''SELECT DISTINCT s.project FROM sessions s JOIN episode_stage e ON e.session=s.id
                WHERE e.family=? ORDER BY s.project LIMIT 50''', (name,))]
            identifier = hash_text('v2:' + name)[:12]
            kind = 'result_reuse' if name == 'reuse_results' else 'polling_reducer' if name == 'await_completion' else 'result_reducer' if name == 'validation_output' else 'workflow_bundle'
            candidate = dict(id=identifier, name=TITLES[name], family=name, opportunity_type=kind,
                sequence=variants[0]['sequence'], variants=variants, occurrences=stats['n'], session_count=stats['sessions'],
                projects=directories, project_count=projects['repos'], unresolved_project_count=projects['unresolved'],
                average_model_turns=round(stats['turns'], 2), average_tool_output_tokens=round(stats['output'], 2),
                modeled_tool_output_reduction_tokens=stats['reduction'], measured_token_savings=None,
                score=round(score, 2), score_components={'eligible_occurrences': stats['n'],
                    'average_replayed_output_reduction_tokens': round(stats['reduction']/stats['n'], 2),
                    'cross_session_interface_recurrence': round(recurrence, 4), 'minimum_result_budget': result_budget, 'reuse_window_model_turns': 5},
                evidence={'known_outcome_fraction': round(stats['known'], 4),
                          'counting': 'Non-overlapping episodes per family; replayed calls excluded.',
                          'output_contract': 'Reference an identical freshly verified read result from the same request, within five model turns and without an observed compaction; full results remain retrievable.' if name == 'reuse_results' else 'Wait for completion instead of returning empty intermediate process polls; preserve the terminal result.' if name == 'await_completion' else 'Successful validation status, diagnostic context and final 12 nonempty lines; full logs available on request.'},
                outcomes={'success': stats['success'], 'failure': stats['failed'], 'unknown': stats['n']-stats['success']-stats['failed']},
                deterministic_boundary='Perform the fresh read, compare its exact content with a retained result, and return an unchanged-result reference when identical.' if name == 'reuse_results' else 'Await the linked process completion, preserving terminal output and errors.' if name == 'await_completion' else 'Execute explicitly configured validation commands and return structured status with diagnostics.' if kind == 'workflow_bundle' else 'Replace verbose successful validation output with status, diagnostic context and its final lines; retain the full log for retrieval.',
                suggested_inputs=['read operation and parameters', 'request-scoped retained-result cache'] if name == 'reuse_results' else ['process handle', 'deadline or cancellation policy'] if name == 'await_completion' else ['repository path', 'explicit build/test/lint command and scope'],
                suggested_output=['unchanged-result reference or full changed content', 'full-result retrieval handle'] if name == 'reuse_results' else ['terminal process result', 'timeout or cancellation state'] if name == 'await_completion' else ['completion and exit code', 'diagnostics and final output', 'full-log reference'],
                estimates_note='Counterfactual visible-text estimate (ceil(characters / 4)), conditional on the output contract and no later full-log retrieval. Not measured billing savings. Candidate estimates overlap; use ttm report for a deduplicated portfolio.',
                review_notes=['Repetition is not proof that choosing the command or scope is deterministic.',
                              'Failures, incomplete terminal results, source reads, full patches and unattributed batches are not summarised. Empty linked polls can be omitted only after completion is observed.'])
            db.execute('INSERT INTO patterns VALUES(?,?,?)', (identifier, score, json.dumps(candidate)))
            db.execute('''INSERT OR IGNORE INTO occurrences SELECT ?,session,start,end FROM episode_stage WHERE family=?''', (identifier, name))
            db.execute('INSERT INTO candidate_actions SELECT ?,action,mechanism,reduction FROM member_stage WHERE family=?', (identifier, name))
            results.append(candidate)
        db.execute("INSERT OR REPLACE INTO metadata VALUES('analysis',?)", (json.dumps({'version': 2, 'timestamp': datetime.now(timezone.utc).isoformat(),
            'diagnostics': dict(diagnostics), 'options': {'min_length': min_length, 'max_length': max_length, 'min_occurrences': min_occurrences,
            'min_sessions': min_sessions, 'result_budget': result_budget, 'include_exploratory': include_exploratory}}),))
    return sorted(results, key=lambda c: (-c['score'], c['opportunity_type'] != 'workflow_bundle', c['id']))


def corpus_report(db):
    row = db.execute("SELECT value FROM metadata WHERE key='analysis'").fetchone()
    if not row: raise ValueError('Run ttm analyse before requesting a corpus report')
    analysis = json.loads(row[0]); diagnostics = analysis['diagnostics']
    # Assign each original result once, preferring a workflow over its general
    # fallback result reducer. Model turns and speculative context are not billed.
    db.execute('DROP TABLE IF EXISTS temp.report_credits')
    db.execute('CREATE TEMP TABLE report_credits(action INTEGER,mechanism TEXT,pattern TEXT,reduction INTEGER,PRIMARY KEY(action,mechanism))')
    rows = db.execute('SELECT id,data FROM patterns ORDER BY score DESC,id').fetchall()
    rows.sort(key=lambda row: json.loads(row['data'])['opportunity_type'] != 'workflow_bundle')
    for row in rows:
        db.execute('INSERT OR IGNORE INTO report_credits SELECT action,mechanism,pattern,reduction FROM candidate_actions WHERE pattern=? AND reduction>0', (row['id'],))
    credit = db.execute('SELECT count(DISTINCT action),coalesce(sum(reduction),0) FROM report_credits').fetchone()
    baseline = diagnostics.get('observed_tool_output_tokens', 0)
    breakdown = []
    for row in rows:
        candidate = json.loads(row['data'])
        values = db.execute('SELECT count(DISTINCT action),coalesce(sum(reduction),0) FROM report_credits WHERE pattern=?', (row['id'],)).fetchone()
        breakdown.append({'id': row['id'], 'tool': candidate['name'], 'opportunity_type': candidate['opportunity_type'],
                          'credited_results': values[0], 'estimated_tokens_avoided': values[1], 'occurrences': candidate['occurrences']})
    usage_row = db.execute("SELECT value FROM metadata WHERE key='model_call_audit'").fetchone()
    model_usage = json.loads(usage_row[0]) if usage_row else None
    mechanisms = {row[0]: row[1] for row in db.execute('SELECT mechanism,sum(reduction) FROM report_credits GROUP BY mechanism')}
    return {'schema_version': 2, 'analysis_timestamp': analysis['timestamp'],
            'counterfactual': 'If these output contracts had been used, and omitted logs were not fetched again',
            'unique_actions': diagnostics.get('unique_actions', 0), 'observed_tool_output_tokens': baseline,
            'estimated_tool_output_tokens_with_tools': max(0, baseline-credit[1]),
            'estimated_tool_output_tokens_avoided': credit[1], 'reduction_percent': round(100*credit[1]/baseline, 3) if baseline else 0,
            'reduction_by_mechanism': mechanisms,
            'validation_and_polling_scenario_tokens': mechanisms.get('validation_status', 0)+mechanisms.get('empty_poll', 0),
            'retained_result_reuse_scenario_tokens': mechanisms.get('unchanged_result', 0),
            'credited_results': credit[0], 'credited_mechanisms': db.execute('SELECT count(*) FROM report_credits').fetchone()[0], 'by_tool': breakdown, 'diagnostics': diagnostics,
            'measured_billing_savings': None, 'model_call_scenario': model_usage,
            'limits': ['Tool-output estimates use ceil(characters / 4), not provider tokenizers or billing. Recorded API usage is a separate scenario.',
                       'Results are replayed under a proposed output contract; actual model behaviour and retrieval are not measured.',
                       'Each action/reduction mechanism receives credit once. Terminal output and empty polling are disjoint. Failures, source/patch outputs and ambiguous batches are never summarised.',
                       'Identical-result reuse requires an available prior result in the same request and no observed compaction; later retrieval reduces savings.',
                       'No savings are assumed for hidden reasoning, cached context, repeated context replay or fewer future model turns.']}


def report_markdown(report):
    lines = ['# Corpus token opportunity report', '',
        f"**Estimated tool-output tokens avoided: {report['estimated_tool_output_tokens_avoided']:,} ({report['reduction_percent']}%).**", '',
        report['counterfactual'] + '.', '',
        f"Validation and polling scenario: {report['validation_and_polling_scenario_tokens']:,} tokens.",
        f"Additional retained-result reuse scenario: {report['retained_result_reuse_scenario_tokens']:,} tokens.", '',
        f"Observed before: {report['observed_tool_output_tokens']:,} estimated tool-output tokens.",
        f"Modelled after: {report['estimated_tool_output_tokens_with_tools']:,} estimated tool-output tokens.",
        f"Coverage: {report['unique_actions']:,} unique actions; {report['credited_results']:,} results receive reduction credit.", '',
        '| Proposed tool | Credited results | Estimated tokens avoided |', '| --- | ---: | ---: |']
    lines += [f"| {tool['tool']} | {tool['credited_results']:,} | {tool['estimated_tokens_avoided']:,} |" for tool in report['by_tool']]
    lines += ['', 'These contributions are deduplicated, so they can be added together.', '',
              '## What this estimate means', '', *['- '+limit for limit in report['limits']], '']
    if report.get('model_call_scenario'):
        usage = report['model_call_scenario']; total = usage['gross_historical_call_exposure']
        lines += ['## Separate model-call scenario', '',
                  f"Recorded input on {total['model_calls']:,} narrowly eligible calls: {total['recorded_input_tokens']:,} tokens.",
                  f"Of that: {total['cached_input_tokens']:,} cached, {total['cache_write_input_tokens']:,} cache-write, {total['uncached_input_tokens']:,} uncached. Recorded output: {total['recorded_output_tokens']:,}.",
                  '', 'This is gross historical call exposure, not measured net savings. Do not add it to the tool-output estimate.', '',
                  *['- '+limit for limit in usage['limits']], '']
    return '\n'.join(lines)
