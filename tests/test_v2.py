"""Synthetic acceptance cases for the corpus audit's observed failure classes."""
import json
from pathlib import Path
import tempfile
import unittest
from transcript_tool_miner.parsers import parse
from transcript_tool_miner.normalize import Normalizer
from transcript_tool_miner.storage import connect, scan, candidates, get_candidate
from transcript_tool_miner.miner import analyse
from transcript_tool_miner.export import package


def rollout(session, commands, *, turns=True, output='ok', legacy=False):
    items=[{'type':'message','role':'user','content':[{'type':'input_text','text':'Validate the invented project.'}]}]
    for i,command in enumerate(commands):
        items.append({'type':'function_call','name':'exec_command','call_id':f'{session}-unique-invented-call-{i}', 'arguments':json.dumps({'cmd':command})})
        if turns:
            items.append({'type':'function_call_output','call_id':f'{session}-unique-invented-call-{i}', 'output':json.dumps({'exit_code':0,'output':output})})
    if not turns:
        for i in range(len(commands)):
            items.append({'type':'function_call_output','call_id':f'{session}-unique-invented-call-{i}','output':json.dumps({'exit_code':0,'output':output})})
    meta={'id':session,'cwd':'/invented/repository'}
    return {'session':meta,'items':items} if legacy else [{'type':'session_meta','payload':meta},*[{'type':'response_item','payload':i} for i in items]]


class CorpusRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def write(self,name,data):
        p=self.root/name;p.write_text(json.dumps(data));return p

    def test_legacy_response_items_are_not_silently_dropped(self):
        p=self.write('legacy.json',rollout('legacy',['pytest tests/sample.py','git status --short'],legacy=True))
        s=parse(p,Normalizer())
        self.assertEqual(len(s.actions),2)
        self.assertEqual(s.session_id,'legacy')

    def test_literal_wrapped_tool_is_recognized(self):
        data=rollout('wrapped',[])
        data.append({'type':'response_item','payload':{'type':'custom_tool_call','name':'functions.exec','call_id':'wrapped-real','input':'text(await tools.exec_command({cmd: "pytest tests/sample.py"}));'}})
        s=parse(self.write('wrapped.json',data),Normalizer())
        self.assertEqual(s.actions[0].category,'run_tests')

    def test_pure_reads_do_not_claim_savings_or_outrank_validation(self):
        with connect(self.root/'db.sqlite3') as db:
            paths=[]
            for i in range(3):
                paths.append(self.write(f'read-{i}.json',rollout(f'read-{i}',['cat one.py','cat two.py','cat three.py'],output='invented code '*3000)))
                paths.append(self.write(f'validate-{i}.json',rollout(f'validate-{i}',['pytest tests/sample.py','git status --short','git diff --check'],output='invented passing test output\n'*200)))
            scan(db,paths);result=analyse(db)
            self.assertTrue(result)
            self.assertEqual(result[0]['name'],'validate_and_report')
            self.assertFalse(any(all(step=='read_file' for step in c['sequence']) for c in result))
            self.assertNotIn('estimated_historical_avoidable_tokens',result[0])
            self.assertIsNone(result[0]['measured_token_savings'])

    def test_already_batched_calls_are_not_a_model_turn_opportunity(self):
        with connect(self.root/'db.sqlite3') as db:
            scan(db,[self.write(f'batch-{i}.json',rollout(f'batch-{i}',['pytest tests/sample.py','git status --short'],turns=False,output='passing\n'*1000)) for i in range(3)])
            self.assertFalse(any(c['opportunity_type']=='workflow_bundle' for c in analyse(db)))

    def test_build_and_test_variants_form_one_nonoverlapping_family(self):
        with connect(self.root/'db.sqlite3') as db:
            paths=[]
            for i in range(4):
                command='dotnet build app.csproj' if i%2 else 'dotnet test app.csproj'
                paths.append(self.write(f'family-{i}.json',rollout(f'family-{i}',[command,'git status --short','git diff --stat'],output='all checks passed\n'*200)))
            scan(db,paths);result=[c for c in analyse(db) if c['opportunity_type']=='workflow_bundle']
            self.assertEqual(len(result),1)
            self.assertEqual(result[0]['occurrences'],4)
            self.assertEqual(len(result[0]['variants']),2)

    def test_copied_events_do_not_inflate_support(self):
        original=rollout('original',['pytest tests/sample.py','git status --short'],output='passed\n'*1000)
        copied=json.loads(json.dumps(original));copied[0]['payload']['id']='fork'
        with connect(self.root/'db.sqlite3') as db:
            scan(db,[self.write('original.json',original),self.write('fork.json',copied)])
            self.assertEqual(analyse(db),[])

    def test_opaque_or_mutating_step_is_a_boundary(self):
        with connect(self.root/'db.sqlite3') as db:
            scan(db,[self.write(f'mutate-{i}.json',rollout(f'mutate-{i}',['pytest tests/sample.py','git push','git status --short'],output='passed\n'*1000)) for i in range(3)])
            self.assertFalse(any(c['opportunity_type']=='workflow_bundle' for c in analyse(db)))

    def test_excluded_prompt_indexes_are_cached(self):
        p=self.write('index.json',{'display':'Invented prompt without tool calls'})
        with connect(self.root/'db.sqlite3') as db:
            self.assertEqual(scan(db,[p])['unsupported'],1)
            self.assertEqual(scan(db,[p])['unchanged'],1)


class PollingAndReplayTests(unittest.TestCase):
    def test_empty_polls_are_credited_only_after_observed_completion(self):
        from transcript_tool_miner.miner import corpus_report
        for completed in (False,True):
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory);paths=[]
                for session in ('one','two'):
                    rows=rollout('poll-'+session,[])
                    def call(name,call_id,args,result):
                        rows.extend([{'type':'response_item','payload':{'type':'function_call','name':name,'call_id':session+call_id,'arguments':json.dumps(args)}},
                                     {'type':'response_item','payload':{'type':'function_call_output','call_id':session+call_id,'output':json.dumps(result)}}])
                    call('exec_command','launch',{'cmd':'pytest tests/a.py'},{'session_id':101,'output':''})
                    for i in range(4):call('write_stdin',f'poll{i}',{'session_id':101,'chars':''},{'session_id':101,'output':''})
                    if completed:call('write_stdin','final',{'session_id':101,'chars':''},{'exit_code':1,'output':'FAILED invented test'})
                    p=root/f'{session}.json';p.write_text(json.dumps(rows));paths.append(p)
                with connect(root/'db.sqlite3') as db:
                    scan(db,paths);result=analyse(db);report=corpus_report(db)
                    if completed:
                        self.assertEqual([r['name'] for r in result],['await_process_completion'])
                        self.assertGreater(report['estimated_tool_output_tokens_avoided'],0)
                        self.assertEqual(report['credited_results'],2)
                    else:self.assertEqual(report['estimated_tool_output_tokens_avoided'],0)

    def test_terminal_and_poll_credits_are_disjoint(self):
        from transcript_tool_miner.miner import corpus_report
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);paths=[]
            for session in ('one','two'):
                rows=rollout('sum-'+session,[])
                for i,(name,args,result) in enumerate([
                    ('exec_command',{'cmd':'pytest tests/a.py'},{'session_id':11,'output':''}),
                    ('write_stdin',{'session_id':11,'chars':''},{'session_id':11,'output':''}),
                    ('write_stdin',{'session_id':11,'chars':''},{'exit_code':0,'output':'passed test\n'*300}),
                    ('exec_command',{'cmd':'git status --short'},{'exit_code':0,'output':''})]):
                    rows.extend([{'type':'response_item','payload':{'type':'function_call','name':name,'call_id':f'{session}-invented-long-call-{i}','arguments':json.dumps(args)}},
                                 {'type':'response_item','payload':{'type':'function_call_output','call_id':f'{session}-invented-long-call-{i}','output':json.dumps(result)}}])
                p=root/f'{session}.json';p.write_text(json.dumps(rows));paths.append(p)
            with connect(root/'db.sqlite3') as db:
                scan(db,paths);analyse(db);r=corpus_report(db)
                self.assertEqual(r['credited_results'],2)
                self.assertEqual(r['credited_mechanisms'],4)
                self.assertEqual(r['estimated_tool_output_tokens_avoided'],sum(c['estimated_tokens_avoided'] for c in r['by_tool']))
                self.assertLess(r['estimated_tool_output_tokens_avoided'],r['observed_tool_output_tokens'])

class IdenticalResultTests(unittest.TestCase):
    def test_identical_reads_can_reuse_results_but_not_across_reset_boundaries(self):
        from transcript_tool_miner.miner import corpus_report
        for boundary in ('none','user','compaction','changed_content','same_tail'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                root=Path(directory);paths=[]
                for session in ('one','two'):
                    rows=rollout('reuse-'+session,['cat same.py','cat same.py'],output='invented source line\n'*500)
                    if boundary=='user':rows.insert(4,{'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'A new invented request'}]}})
                    elif boundary=='compaction':rows.insert(4,{'type':'compacted','payload':{}})
                    elif boundary=='same_tail':
                        rows[3]['payload']['output']=json.dumps({'exit_code':0,'output':'different prefix one\nOutput:\n'+'shared text\n'*500})
                        rows[-1]['payload']['output']=json.dumps({'exit_code':0,'output':'different prefix two\nOutput:\n'+'shared text\n'*500})
                    elif boundary=='changed_content':rows[-1]['payload']['output']=json.dumps({'exit_code':0,'output':'changed invented source\n'*500})
                    p=root/f'{session}.json';p.write_text(json.dumps(rows));paths.append(p)
                with connect(root/'db.sqlite3') as db:
                    scan(db,paths);result=analyse(db);r=corpus_report(db)
                    if boundary=='none':
                        self.assertEqual([c['name'] for c in result],['reuse_unchanged_results'])
                        self.assertEqual(r['credited_results'],2)
                        self.assertGreater(r['estimated_tool_output_tokens_avoided'],4000)
                    else:self.assertEqual(r['estimated_tool_output_tokens_avoided'],0)

if __name__ == '__main__':
    unittest.main()
