"""Public parser, persistence, privacy and installed-CLI contracts; invented inputs only."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from transcript_tool_miner.export import package, markdown
from transcript_tool_miner.miner import analyse, corpus_report
from transcript_tool_miner.models import estimate_tokens
from transcript_tool_miner.normalize import Normalizer
from transcript_tool_miner.parsers import parse, outcome
from transcript_tool_miner.results import compact_validation, result_metadata
from transcript_tool_miner.storage import connect, scan, candidates, get_candidate, project_identity
from test_v2 import rollout

FIXTURES=Path(__file__).parent/'fixtures'
ROOT=Path(__file__).resolve().parents[1]


class ParsingTests(unittest.TestCase):
    def test_providers_preserve_operation_variants_and_turns(self):
        for provider in ('claude','codex'):
            s=parse(FIXTURES/f'{provider}-a.json',Normalizer())
            self.assertEqual(s.source,provider)
            self.assertEqual(s.project,'/synthetic/repo-a')
            self.assertEqual([a.category for a in s.actions],['run_tests','git_status','git_diff'])
            self.assertEqual(s.actions[-1].operations[0]['label'],'git_diff_check')
            self.assertEqual([a.turn for a in s.actions],[1,2,3])
            self.assertTrue(all(a.success for a in s.actions))
            self.assertGreater(s.actions[0].output_tokens,s.actions[0].compact_output_tokens)
            self.assertEqual(s.digest,hashlib.sha256((FIXTURES/f'{provider}-a.json').read_bytes()).hexdigest())

    def test_jsonl_partial_tail_and_duplicate_records(self):
        records=json.loads((FIXTURES/'claude-a.json').read_text())
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'invented.jsonl'
            path.write_text('\n'.join(json.dumps(r) for r in records+[records[2]])+'\n{"partial":')
            s=parse(path,Normalizer())
            self.assertEqual(len(s.actions),3)
            self.assertEqual(len(s.warnings),1)
            self.assertEqual(s.actions[0].line,3)

    def test_event_only_user_boundaries(self):
        rows=[{'type':'session_meta','payload':{'id':'invented-boundaries'}}]
        for i,command in enumerate(('pytest tests/a.py','git status')):
            rows.extend([{'type':'event_msg','payload':{'type':'user_message','message':f'Request {i}'}},
                         {'type':'response_item','payload':{'type':'function_call','name':'shell','call_id':str(i),'arguments':json.dumps({'command':['bash','-lc',command]})}},
                         {'type':'response_item','payload':{'type':'function_call_output','call_id':str(i),'output':'Process exited with code 0'}}])
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'input.json';p.write_text(json.dumps(rows))
            self.assertEqual([a.request for a in parse(p,Normalizer()).actions],[1,2])

    def test_linked_process_output_and_completion(self):
        rows=rollout('invented-poll',[]) + [
            {'type':'response_item','payload':{'type':'function_call','name':'exec_command','call_id':'launch','arguments':'{"cmd":"pytest tests/sample.py"}'}},
            {'type':'response_item','payload':{'type':'function_call_output','call_id':'launch','output':'{"session_id":42,"output":"started"}'}},
            {'type':'response_item','payload':{'type':'function_call','name':'write_stdin','call_id':'poll','arguments':'{"session_id":42,"chars":""}'}},
            {'type':'response_item','payload':{'type':'function_call_output','call_id':'poll','output':json.dumps({'exit_code':0,'output':'passing line\n'*300})}}]
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'poll.json';p.write_text(json.dumps(rows));s=parse(p,Normalizer())
            self.assertEqual(s.actions[1].linked_to,'launch')
            self.assertIs(s.actions[0].success,True)
            self.assertEqual(s.actions[1].output_tokens,0)
            self.assertGreater(s.actions[0].output_tokens,s.actions[0].compact_output_tokens)
            self.assertEqual(len(s.actions[0].result_lines),2)

    def test_token_estimate_and_explicit_outcomes(self):
        self.assertEqual(estimate_tokens(''),0);self.assertEqual(estimate_tokens('12345'),2)
        self.assertEqual(estimate_tokens('☃'*8),2)
        self.assertIsNone(outcome('looks good'))
        self.assertIs(outcome('Process exited with code 1',False),False)
        self.assertIs(result_metadata('[{"type":"text","text":"Process exited with code 1"}]',False)[0],False)


class NormalizationTests(unittest.TestCase):
    def labels(self,command):
        return [op.label for op in Normalizer().operations('Bash',{'command':command},'/invented')]

    def test_compounds_preserve_variants_and_quoted_paths(self):
        normalizer=Normalizer()
        ops=normalizer.operations('Bash',{'command':'cd "repo with spaces" && git diff --check; git diff --stat; git diff --name-only; git diff'},'/invented')
        self.assertEqual([o.label for o in ops],['git_diff_check','git_diff_stat','git_diff_names','git_diff_patch'])
        self.assertEqual(ops[0].cwd,'/invented/repo with spaces')
        self.assertEqual(self.labels('git -C "repo with spaces" diff --check'),['git_diff_check'])
        self.assertEqual(self.labels('rg --files | head -20'),['find_related_files'])

    def test_opaque_constructs_and_mutations_stay_boundaries(self):
        for command in ('echo $(cat source)','pytest > output','python - <<EOF\nprint(1)\nEOF','git push','cat input | tee output'):
            self.assertIn('unknown',self.labels(command))
        self.assertEqual(self.labels('python -c "print(1)\ngit diff"'),['unknown'])
        self.assertEqual(self.labels('git status || pytest tests/a.py'),['git_status','unknown','run_tests'])
        self.assertEqual(self.labels('pytest --collect-only'),['inspection'])

    def test_wrapper_literals_cannot_smuggle_fake_calls(self):
        n=Normalizer()
        for source in ('text("tools.exec_command({cmd: 1})")','// tools.exec_command({cmd: 1})\ntext(1)',
                       'tools.exec_command({cmd: commandVariable})','tools.exec_command({cmd: `git diff ${revision}`})',
                       'if (ready) tools.exec_command({cmd:"pytest"})','fetch("local-placeholder"); tools.exec_command({cmd:"pytest"})'):
            self.assertEqual([o.kind for o in n.operations('functions.exec',{'input':source})],['unknown'])
        valid='text(await tools.exec_command({cmd: "pytest tests/a.py", workdir: "/invented"}));'
        self.assertEqual(n.operations('functions.exec',{'code':valid})[0].kind,'run_tests')
        formatting='const results = await Promise.allSettled([tools.exec_command({cmd: \"pytest tests/a.py\"})]); for (let i=0; i<results.length; i++) text(results[i]);'
        self.assertEqual(n.operations('functions.exec',{'input':formatting})[0].kind,'run_tests')
        self.assertEqual(n.operations('write_stdin',{'session_id':1,'chars':'rm file\n'})[0].kind,'unknown')

    def test_configured_matcher(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'matchers.json';p.write_text('[{"action":"run_tests","pattern":"^make check$"}]')
            self.assertEqual(Normalizer(p).classify('Bash','make check'),'run_tests')


class ReducerTests(unittest.TestCase):
    def test_status_contract_preserves_diagnostics_and_tail(self):
        text='passed\n'*300+'WARNING: invented diagnostic\ncontext\n'+'passed\n'*30+'300 passed\n'
        result=compact_validation(text,{'label':'run_tests'},True,True)
        kept='\n'.join(result['summary']['diagnostics_and_tail'])
        self.assertIn('WARNING: invented diagnostic',kept);self.assertIn('context',kept);self.assertIn('300 passed',kept)
        self.assertLess(result['tokens'],estimate_tokens(text))
        self.assertTrue(result['summary']['full_log_available_on_request'])

    def test_conflicting_failure_text_is_not_summarized(self):
        self.assertIsNone(compact_validation('passed\n'*300+'FAILED invented test', {'label':'run_tests'},True,True))
        self.assertIs(result_metadata('Exit code 1\nFAILED',False)[0],False)

    def test_failed_incomplete_and_source_outputs_are_never_compressed(self):
        for label,success,complete in [('run_tests',False,True),('build',True,False),('read_file',True,True),('git_diff_patch',True,True)]:
            self.assertIsNone(compact_validation('text\n'*500,{'label':label},success,complete))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'input';shutil.copytree(FIXTURES,self.source)
        self.database=self.root/'miner.sqlite3'

    def test_incremental_scan_invalidates_derived_results(self):
        with connect(self.database) as db:
            self.assertEqual(scan(db,[self.source])['actions'],12)
            self.assertTrue(analyse(db));self.assertEqual(scan(db,[self.source])['unchanged'],4)
            self.assertEqual(db.execute('select count(*) from actions').fetchone()[0],12)
            p=self.source/'claude-a.json';rows=json.loads(p.read_text());rows[2]['message']['content'][1]['input']['command']='opaque-task';p.write_text(json.dumps(rows))
            self.assertEqual(scan(db,[self.source])['imported'],1);self.assertEqual(candidates(db),[])
            self.assertEqual(db.execute('select count(*) from actions').fetchone()[0],12)
        self.assertEqual(stat.S_IMODE(self.database.stat().st_mode),0o600)

    def test_duplicates_unsupported_indexes_and_config_changes(self):
        shutil.copyfile(self.source/'claude-a.json',self.source/'copy.json')
        (self.source/'index.jsonl').write_text('{"display":"Invented prompt only"}\n')
        with connect(self.database) as db:
            result=scan(db,[self.source]);self.assertEqual(result['duplicates'],1);self.assertEqual(result['unsupported'],1)
            self.assertEqual(scan(db,[self.source])['unchanged'],6)
            matcher=self.root/'matchers.json';matcher.write_text('[{"action":"custom_test","pattern":"^pytest"}]')
            scan(db,[self.source],matchers=matcher)
            self.assertEqual(db.execute("select count(*) from actions where category='custom_test'").fetchone()[0],4)

    def test_database_path_cannot_modify_a_transcript(self):
        p=self.root/'private.jsonl';p.write_text('{"invented":true}\n');p.chmod(0o640)
        before=p.read_bytes()
        with self.assertRaises(ValueError):
            with connect(p):pass
        self.assertEqual(p.read_bytes(),before);self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o640)

    def test_report_has_deduplicated_counterfactual_accounting(self):
        with connect(self.database) as db:
            scan(db,[self.source]);rows=analyse(db);report=corpus_report(db)
            self.assertGreater(report['estimated_tool_output_tokens_avoided'],1000)
            self.assertEqual(report['credited_results'],4)
            self.assertEqual(report['estimated_tool_output_tokens_avoided'],sum(t['estimated_tokens_avoided'] for t in report['by_tool']))
            self.assertEqual(report['observed_tool_output_tokens']-report['estimated_tool_output_tokens_with_tools'],report['estimated_tool_output_tokens_avoided'])
            self.assertGreater(sum(c['modeled_tool_output_reduction_tokens'] for c in rows),report['estimated_tool_output_tokens_avoided'])
            self.assertIsNone(report['measured_billing_savings'])

    def test_export_retains_evidence_without_raw_identifiers_by_default(self):
        with connect(self.database) as db:
            scan(db,[self.source]);identifier=analyse(db)[0]['id'];candidate=get_candidate(db,identifier)
            self.assertEqual(len(candidate['examples']),3)
            candidate['examples'][0]['actions'][0]['command']='TOKEN=invented-secret git -C /Users/invented/repo diff'
            bundle=package(candidate);text=json.dumps(bundle)
            self.assertNotIn('invented-secret',text);self.assertNotIn('/Users/invented',text)
            self.assertNotIn(str(self.source),text);self.assertNotIn('raw_arguments',text)
            self.assertEqual(bundle['schema_version'],2);self.assertIn('untrusted data',bundle['generation_prompt'])
            self.assertIn('not measured',markdown(bundle))
            self.assertIn('invented-secret',json.dumps(package(candidate,True)))

    def test_mining_memory_does_not_load_large_raw_payloads(self):
        with connect(self.database) as db:
            scan(db,[self.source])
            # Large cold payloads must not enter the mining working set.
            db.execute('update actions set data=?',(json.dumps({'unused_raw_argument':'x'*2_000_000}),));db.commit()
            tracemalloc.start()
            try:
                self.assertTrue(analyse(db));peak=tracemalloc.get_traced_memory()[1]
            finally:tracemalloc.stop()
            self.assertLess(peak,3_000_000)

    def test_git_worktrees_share_repository_identity(self):
        repo=self.root/'repo';repo.mkdir();(repo/'.git').mkdir();work=repo/'.git'/'worktrees'/'one';work.mkdir(parents=True);(work/'commondir').write_text('../..')
        checkout=self.root/'checkout';checkout.mkdir();(checkout/'.git').write_text(f'gitdir: {work}')
        project_identity.cache_clear()
        self.assertEqual(project_identity(str(repo)),project_identity(str(checkout)))

    def test_v1_migration_preserves_raw_actions_and_requires_rescan(self):
        from transcript_tool_miner.storage import SCHEMA
        db=sqlite3.connect(self.database);db.executescript(SCHEMA)
        db.execute('pragma user_version=1')
        db.execute("insert into sessions values(1,?,0,0,'old','1:builtin:None','old','claude','/invented','','[]','[]')",(str((self.source/'claude-a.json').resolve()),))
        db.execute("insert into actions values(1,1,0,'read_file','{}')");db.commit();db.close()
        with connect(self.database) as db:
            self.assertEqual(db.execute('select data from actions').fetchone()[0],'{}')
            with self.assertRaisesRegex(ValueError,'v1 actions'):analyse(db)
            scan(db,[self.source]);self.assertTrue(analyse(db))


class CliAcceptanceTests(unittest.TestCase):
    def cli(self,cwd,*args,success=True):
        env=dict(os.environ,PYTHONPATH=str(ROOT/'src'),HOME=str(cwd/'isolated-home'),CODEX_HOME=str(cwd/'isolated-codex'),CLAUDE_CONFIG_DIR=str(cwd/'isolated-claude'))
        result=subprocess.run([sys.executable,'-m','transcript_tool_miner',*args],cwd=cwd,env=env,capture_output=True,text=True)
        if success:self.assertEqual(result.returncode,0,result.stderr)
        else:self.assertNotEqual(result.returncode,0)
        return result

    def test_cli_scan_analyse_candidates_export_and_corpus_report(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd=Path(directory);self.cli(cwd,'scan',str(FIXTURES),'--workers','2');self.cli(cwd,'analyse')
            rows=json.loads(self.cli(cwd,'candidates','--json').stdout)
            self.assertTrue(rows)
            self.assertEqual(rows[0]['name'],'validate_and_report');self.assertEqual(rows[0]['occurrences'],4)
            identifier=rows[0]['id'];self.assertIn('not measured',self.cli(cwd,'candidate','show',identifier).stdout)
            target=cwd/'exports'/'candidate.json'
            self.cli(cwd,'candidate','export',identifier,'--format','json','-o',str(target))
            self.assertEqual(json.loads(target.read_text())['candidate']['id'],identifier)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode),0o600)
            self.cli(cwd,'candidate','export',identifier,'-o',str(target),success=False)
            report=json.loads(self.cli(cwd,'report','--format','json').stdout)
            self.assertEqual(report['credited_results'],4)
            self.assertGreater(report['estimated_tool_output_tokens_avoided'],1000)
            self.assertEqual(json.loads(self.cli(cwd,'scan',str(FIXTURES),'--json').stdout)['unchanged'],4)

    def test_default_discovery_and_database_option_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd=Path(directory)
            for provider,folder in [('claude','isolated-claude/projects/demo'),('codex','isolated-codex/sessions/demo')]:
                target=cwd/folder;target.mkdir(parents=True);shutil.copyfile(FIXTURES/f'{provider}-a.json',target/'invented.json')
            db=str(cwd/'other.sqlite3')
            self.cli(cwd,'--db',db,'scan','--claude');self.cli(cwd,'scan','--codex','--db',db)
            self.cli(cwd,'analyse','--db',db)
            self.assertEqual(json.loads(self.cli(cwd,'candidates','--db',db,'--json').stdout)[0]['occurrences'],2)
            self.cli(cwd,'scan',str(cwd/'missing'),success=False)
            self.cli(cwd,'analyse','--min-length','11',success=False)
            self.cli(cwd,'candidate','show','missing',success=False)

if __name__=='__main__':unittest.main()
