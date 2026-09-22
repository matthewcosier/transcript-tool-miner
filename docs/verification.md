# Version 0.2 verification

Change category: parser and workflow-classification fixes, counterfactual reporting,
compact persistence, and optional recorded-usage analysis.

## Automated coverage

The 37-test suite runs against invented fixtures and isolated temporary databases.
It exercises the public CLI through subprocesses, including parallel scanning,
analysis, candidate exports and the corpus report. Tests do not read a real user's
transcript directories.

Coverage includes:

- Claude/Codex adapters, legacy response items, literal wrapper calls and meaningful
  Git-operation variants.
- Unknown commands and mutations as boundaries, already-batched calls, request
  changes, linked polling, exact-result reuse and compaction resets.
- Replayed-event exclusion, grouping related workflows, and non-duplicated output
  credits when several candidates cover the same result.
- Diagnostic retention, failed/incomplete-result protection, and conservative
  treatment of changed source content.
- Recorded API counters, cached/uncached separation, duplicate usage snapshots,
  inconsistent counters and exact-plan requirements.
- Incremental scans, unsupported-input caching, migration of populated v1 tables,
  refusal to modify non-SQLite files, owner-only permissions and export redaction.
- A memory-bound check demonstrating that mining does not load large raw payloads
  from the cold action-data column.
- The public-tree guard rejecting a force-staged synthetic JSONL file in a
  disposable Git repository.

## Detection evidence

The corpus regression cases first failed against the prior implementation on
missing legacy/wrapper support, read-heavy ranking, already-batched calls,
replayed-event support and unsupported-index caching.

The final assertions were also checked with two buildable local reversals:

1. Returning immediately from `_stage()` made the CLI acceptance test fail because
   no candidates were produced.
2. Allowing separate report credits for each overlapping candidate made the
   counterfactual-accounting test fail because the portfolio double-counted them.

Each reversal was restored in `finally` before rerunning the same tests and the
full suite successfully. No reversal is part of the committed implementation.

```sh
PYTHONPATH=src:tests python3 -B -m unittest \
  test_miner.CliAcceptanceTests.test_cli_scan_analyse_candidates_export_and_corpus_report \
  test_miner.StorageTests.test_report_has_deduplicated_counterfactual_accounting -v

python3 -m unittest discover -s tests -v
python3 scripts/check_public_tree.py
```

## Evidence limits

Local validation used Python 3.14.4. CI is configured for Python 3.10, 3.12 and
3.14. The corpus report is a conditional replay estimate, not a measured reduction
in provider bills. Recorded usage on potentially removable model calls is a
separate gross-exposure scenario and must not be added to output reductions.

Only source code, documentation and invented fixtures belong in this repository.
Private corpus data, databases, reports and local audit scripts are excluded.
