# MVP verification

Change category: new CLI capability, parser adapters, SQLite persistence and local export.

Validated locally on Python 3.14.4 using synthetic inputs only:

- Editable package installation in a fresh virtual environment and the installed `ttm` entry point.
- 20 unittest cases, including real CLI subprocesses with isolated home/source directories and temporary SQLite databases.
- Four invented fixture sessions produce one candidate: `git_diff → find_related_files → run_tests`, four occurrences, two projects, score 3,228 and three model turns per occurrence.
- Incremental scanning skips unchanged files, replaces changed action sets, invalidates stale candidates and reclassifies when matchers change.
- Markdown/JSON exports retain score components and bounded representative traces. Default redaction removes the synthetic secret and invented personal home prefix tested by the suite.
- Owner-only permissions for database/export files, refusal to overwrite exports, and a pre-commit guard tested against a force-staged synthetic JSONL file in a disposable repository.

## Detection evidence

The unchanged acceptance test was run with this small, buildable local reversal in `analyse()`:

```python
result = []  # replaces the production mine(...) call
```

Command:

```sh
PYTHONPATH=src:tests python3 -B -m unittest \
  test_miner.CliAcceptanceTests.test_end_to_end_scan_analyse_list_show_export -v
```

With mining disabled, the CLI scan and analyse commands ran normally, but the ranked-candidate assertion failed with `0 != 1`. Restoring the production call made the same test pass. Fixtures, assertions, temporary persistence and process boundaries were unchanged. The reversal was restored immediately and is not part of the implementation.

## Limits of this evidence

No real user transcript contents were read or copied for these tests. Compatibility is demonstrated against representative synthetic provider records, not every historical release. Token savings are heuristic and have not been calibrated against billing or a deployed deterministic replacement. Python 3.10/3.12/3.14 are configured in GitHub CI; local execution used 3.14.4.
