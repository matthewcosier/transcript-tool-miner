"""Recorded-usage accounting uses invented counters and temporary sources only."""
import json
from pathlib import Path
import tempfile
import unittest

from test_v2 import rollout
from transcript_tool_miner.storage import connect, scan
from transcript_tool_miner.miner import analyse, corpus_report
from transcript_tool_miner.usage import audit_model_calls


class UsageAuditTests(unittest.TestCase):
    def evaluate(self, corrupt=False, varied=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for index in range(2):
                command = f'pytest tests/sample{index if varied else 0}.py'
                records = rollout(f'usage-{index}', [command, 'git status --short', 'git diff --check'], output='passed invented test\n' * 300)
                enriched = []
                cumulative = dict(input_tokens=0, cached_input_tokens=0, output_tokens=0)
                turn = 0
                for record in records:
                    enriched.append(record)
                    if record.get('payload', {}).get('type') != 'function_call_output':
                        continue
                    turn += 1
                    last = dict(input_tokens=turn * 1000, cached_input_tokens=turn * 1000 - 500, output_tokens=turn * 10)
                    for key in cumulative:
                        cumulative[key] += last[key]
                    if corrupt and turn == 3:
                        last['input_tokens'] += 1
                    event = {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                        'last_token_usage': last, 'total_token_usage': dict(cumulative)}}}
                    enriched.extend([event, event])  # Repeated cumulative snapshots are not extra calls.
                path = root / f'{index}.json'
                path.write_text(json.dumps(enriched))
                paths.append(path)
            with connect(root / 'miner.sqlite3') as db:
                scan(db, paths)
                analyse(db)
                before = corpus_report(db)['estimated_tool_output_tokens_avoided']
                usage = audit_model_calls(db)
                report = corpus_report(db)
                self.assertEqual(report['estimated_tool_output_tokens_avoided'], before)
                self.assertEqual(report['model_call_scenario'], usage)
                self.assertFalse(usage['net_savings_measured'])
                return usage['gross_historical_call_exposure']

    def test_exact_plans_skip_initial_call_and_separate_cached_usage(self):
        totals = self.evaluate()
        self.assertEqual(totals['model_calls'], 4)
        self.assertEqual(totals['recorded_input_tokens'], 10000)
        self.assertEqual(totals['cached_input_tokens'], 8000)
        self.assertEqual(totals['uncached_input_tokens'], 2000)
        self.assertEqual(totals['recorded_output_tokens'], 100)

    def test_inconsistent_counters_are_not_credited(self):
        totals = self.evaluate(corrupt=True)
        self.assertEqual(totals['model_calls'], 2)
        self.assertEqual(totals['recorded_input_tokens'], 4000)

    def test_similar_interfaces_are_not_exact_repeated_plans(self):
        self.assertEqual(self.evaluate(varied=True)['model_calls'], 0)


if __name__ == '__main__':
    unittest.main()
