import copy
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from transcript_tool_miner.export import package, markdown, redact
from transcript_tool_miner.miner import mine, analyse
from transcript_tool_miner.models import estimate_tokens
from transcript_tool_miner.normalize import Normalizer
from transcript_tool_miner.parsers import parse, outcome
from transcript_tool_miner.storage import connect, scan, candidates, get_candidate

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]


class ParsingTests(unittest.TestCase):
    def test_both_providers_normalize_to_same_workflow(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                session = parse(FIXTURES / f"{provider}-a.json", Normalizer())
                self.assertEqual(session.source, provider)
                self.assertEqual(session.project, "/synthetic/repo-a")
                self.assertEqual([a.category for a in session.actions], ["git_diff", "find_related_files", "run_tests"])
                self.assertEqual([a.turn for a in session.actions], [1, 2, 3])
                self.assertTrue(all(a.success for a in session.actions))
                self.assertIn("HEAD", session.actions[0].command)
                self.assertGreater(session.actions[0].output_tokens, 300)

    def test_jsonl_malformed_and_duplicate_records(self):
        data = json.loads((FIXTURES / "claude-a.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in data + [data[2]]) + '\n{"partial":')
            session = parse(path, Normalizer())
            self.assertEqual(len(session.actions), 3)
            self.assertEqual(len(session.warnings), 1)
            self.assertEqual(session.actions[0].line, 3)

    def test_missing_outcome_is_unknown(self):
        self.assertIsNone(outcome("looks good"))
        self.assertIs(outcome("Process exited with code 1"), False)
        self.assertIs(outcome('{"exit_code": 0}'), True)
        self.assertIs(outcome("anything", explicit_error=True), False)
        self.assertIs(outcome("Process exited with code 1", explicit_error=False), False)

    def test_custom_codex_tools_and_unknown_commands_are_retained(self):
        records = [{"type": "session_meta", "payload": {"id": "synthetic-custom"}},
                   {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch", "call_id": "x", "input": "synthetic patch"}},
                   {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "x", "output": "done"}}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "example.json"
            path.write_text(json.dumps(records))
            action = parse(path, Normalizer()).actions[0]
            self.assertEqual(action.category, "unknown")
            self.assertEqual(action.command, "synthetic patch")
            self.assertEqual(action.output_tokens, 1)
            self.assertIsNone(action.success)

    def test_codex_event_only_requests_split_sequences(self):
        records = [{"type": "session_meta", "payload": {"id": "synthetic-boundaries"}}]
        for i, command in enumerate(("git diff", "rg --files")):
            records += [
                {"type": "event_msg", "payload": {"type": "user_message", "message": f"Request {i}"}},
                {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": str(i), "arguments": json.dumps({"command": ["bash", "-lc", command]})}},
                {"type": "response_item", "payload": {"type": "function_call_output", "call_id": str(i), "output": "Process exited with code 0"}},
            ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.json"
            path.write_text(json.dumps(records))
            session = parse(path, Normalizer())
            self.assertEqual([a.request for a in session.actions], [1, 2])
            self.assertEqual([a.category for a in session.actions], ["git_diff", "find_related_files"])
            self.assertEqual(session.requests, ["Request 0", "Request 1"])

    def test_token_estimate_is_visible_character_heuristic(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("1234"), 1)
        self.assertEqual(estimate_tokens("12345"), 2)
        self.assertEqual(estimate_tokens("☃" * 8), 2)


class NormalizationTests(unittest.TestCase):
    def test_revisions_runners_and_tool_names(self):
        normalizer = Normalizer()
        for command in ("git diff --name-only HEAD", "git diff --name-only HEAD~1", "git -C /synthetic diff"):
            self.assertEqual(normalizer.classify("Bash", command), "git_diff")
        for command in ("pytest tests/a.py", "python3 -m pytest tests/b.py", "npm test -- --runInBand", "dotnet test", "cargo test"):
            self.assertEqual(normalizer.classify("functions.exec_command", command), "run_tests")
        self.assertEqual(normalizer.classify("Read", '{"file_path":"x.py"}'), "read_file")
        for command in ("git diff && rm example", "cat x; npm test", "echo $(cat example)", "pytest > log", "cat a\nrm a"):
            self.assertEqual(normalizer.classify("Bash", command), "unknown")

    def test_custom_matcher(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matchers.json"
            path.write_text(json.dumps([{"action": "run_tests", "pattern": "^make check$"}]))
            self.assertEqual(Normalizer(path).classify("Bash", "make check"), "run_tests")


def simple_sessions():
    actions = [dict(category="git_diff", request=1, turn=1, output_tokens=1000, assistant_tokens=10, success=True),
               dict(category="find_related_files", request=1, turn=2, output_tokens=500, assistant_tokens=30, success=True),
               dict(category="run_tests", request=1, turn=3, output_tokens=300, assistant_tokens=40, success=True)]
    return [dict(id=i, session_id=f"s{i}", source="claude", project=f"project-{i}", actions=copy.deepcopy(actions)) for i in (1, 2)]


class MiningTests(unittest.TestCase):
    def test_aggregation_scores_and_closed_sequences(self):
        result = mine(simple_sessions())
        self.assertEqual(len(result), 1)
        candidate, spans = result[0]
        self.assertEqual(candidate["name"], "validate_changed_files")
        self.assertEqual(candidate["occurrences"], 2)
        self.assertEqual(candidate["project_count"], 2)
        self.assertEqual(candidate["average_model_turns"], 3)
        self.assertEqual(candidate["average_intermediate_tokens"], 1570)
        self.assertEqual(candidate["average_tool_output_tokens"], 1800)
        self.assertEqual(candidate["estimated_historical_avoidable_tokens"], 2740)
        self.assertEqual(candidate["score"], 2740)
        self.assertEqual(candidate["outcomes"], {"success": 2, "failure": 0, "unknown": 0})
        self.assertEqual(spans, [(1, 0, 2), (2, 0, 2)])

    def test_no_cross_request_unknown_or_session_stitching(self):
        for alteration in ("request", "unknown"):
            sessions = simple_sessions()
            for session in sessions:
                if alteration == "request":
                    session["actions"][1]["request"] = 2
                else:
                    session["actions"][1]["category"] = "unknown"
            self.assertEqual(mine(sessions), [])
        sessions = simple_sessions()
        sessions[0]["actions"] = sessions[0]["actions"][:1]
        sessions[1]["actions"] = sessions[1]["actions"][1:]
        self.assertEqual(mine(sessions), [])

    def test_failure_unknown_and_distinct_sessions(self):
        sessions = simple_sessions()
        sessions[0]["actions"][2]["success"] = False
        sessions[1]["actions"][2]["success"] = None
        candidate = mine(sessions)[0][0]
        self.assertEqual(candidate["outcomes"], {"failure": 1, "unknown": 1, "success": 0})
        sessions[1]["session_id"] = sessions[0]["session_id"]
        self.assertEqual(mine(sessions), [])

    def test_nonoverlap_and_more_frequent_subpatterns(self):
        sessions = simple_sessions()
        sessions[0]["actions"] += copy.deepcopy(sessions[0]["actions"][:2])
        result = mine(sessions)
        self.assertEqual({tuple(c["sequence"]): c["occurrences"] for c, _ in result}, {
            ("git_diff", "find_related_files", "run_tests"): 2,
            ("git_diff", "find_related_files"): 3})
        for session in sessions:
            session["actions"] = [dict(session["actions"][0], category="read_file") for _ in range(5)]
        pair = next(c for c, _ in mine(sessions, keep_nested=True) if len(c["sequence"]) == 2)
        self.assertEqual(pair["occurrences"], 4)

    def test_expensive_fewer_occurrences_outrank_cheap(self):
        sessions = simple_sessions()
        for i in range(3, 13):
            session = copy.deepcopy(sessions[0])
            session.update(id=i, session_id=f"s{i}")
            for action in session["actions"]:
                action.update(category="read_file", output_tokens=1, assistant_tokens=1)
            sessions.append(session)
        result = mine(sessions)
        self.assertEqual(result[0][0]["name"], "validate_changed_files")
        self.assertEqual(result[0][0]["occurrences"], 2)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "input"
        shutil.copytree(FIXTURES, self.source)
        self.database = self.directory / "private" / "miner.sqlite3"

    def test_incremental_scan_refresh_and_stale_candidate_invalidation(self):
        with connect(self.database) as db:
            first = scan(db, [self.source])
            self.assertEqual(first["imported"], 4)
            self.assertEqual(first["actions"], 12)
            candidate = analyse(db)[0]
            self.assertEqual(candidate["occurrences"], 4)
            self.assertEqual(candidate["session_count"], 4)
            self.assertEqual(candidate["project_count"], 2)
            self.assertEqual(scan(db, [self.source])["unchanged"], 4)
            self.assertEqual(db.execute("SELECT count(*) FROM actions").fetchone()[0], 12)
            self.assertEqual(len(candidates(db)), 1)
            path = self.source / "claude-a.json"
            rows = json.loads(path.read_text())
            rows[2]["message"]["content"][1]["input"]["command"] = "unrecognized-task"
            path.write_text(json.dumps(rows))
            self.assertEqual(scan(db, [self.source])["imported"], 1)
            self.assertEqual(candidates(db), [])
            self.assertEqual(db.execute("SELECT count(*) FROM actions").fetchone()[0], 12)
            self.assertEqual(analyse(db)[0]["occurrences"], 3)
        self.assertEqual(stat.S_IMODE(self.database.stat().st_mode), 0o600)

    def test_content_duplicate_unsupported_and_matcher_reclassification(self):
        shutil.copyfile(self.source / "claude-a.json", self.source / "copy.json")
        (self.source / "history.jsonl").write_text('{"display":"Only a prompt", "sessionId":"history"}\n')
        with connect(self.database) as db:
            summary = scan(db, [self.source])
            self.assertEqual(summary["duplicates"], 1)
            self.assertEqual(summary["unsupported"], 1)
            self.assertEqual(summary["imported"], 4)
            matcher = self.directory / "matchers.json"
            matcher.write_text('[{"action":"custom_diff","pattern":"^git diff"}]')
            scan(db, [self.source], matchers=matcher)
            self.assertEqual(db.execute("SELECT count(*) FROM actions WHERE category='custom_diff'").fetchone()[0], 4)

    def test_exports_are_bounded_structured_and_redacted(self):
        with connect(self.database) as db:
            scan(db, [self.source])
            candidate_id = analyse(db)[0]["id"]
            candidate = get_candidate(db, candidate_id)
            self.assertEqual(len(candidate["examples"]), 3)
            candidate["examples"][0]["actions"][0]["command"] = "TOKEN=synthetic-secret git -C /Users/invented/repo diff"
            bundle = package(candidate)
            text = json.dumps(bundle)
            self.assertNotIn("synthetic-secret", text)
            self.assertNotIn("/Users/invented", text)
            self.assertNotIn(str(self.source), text)
            self.assertNotIn("raw_arguments", text)
            self.assertEqual(bundle["schema_version"], 1)
            self.assertIn("untrusted data", bundle["generation_prompt"])
            self.assertIn("Historical avoidable tokens (estimated)", markdown(bundle))
            self.assertIn("synthetic-secret", json.dumps(package(candidate, include_sensitive=True)))


class CliAcceptanceTests(unittest.TestCase):
    def cli(self, cwd, *args, success=True):
        environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"), HOME=str(cwd / "isolated-home"), CODEX_HOME=str(cwd / "isolated-codex"), CLAUDE_CONFIG_DIR=str(cwd / "isolated-claude"))
        result = subprocess.run([sys.executable, "-m", "transcript_tool_miner", *args], cwd=cwd, env=environment, capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def test_end_to_end_scan_analyse_list_show_export(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self.cli(cwd, "scan", str(FIXTURES))
            self.cli(cwd, "analyse")
            rows = json.loads(self.cli(cwd, "candidates", "--json").stdout)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["occurrences"], 4)
            self.assertEqual(rows[0]["sequence"], ["git_diff", "find_related_files", "run_tests"])
            self.assertGreater(rows[0]["score"], 0)
            identifier = rows[0]["id"]
            self.assertIn("validate_changed_files", self.cli(cwd, "candidate", "show", identifier).stdout)
            destination = cwd / "exports" / "candidate.json"
            self.cli(cwd, "candidate", "export", identifier, "--format", "json", "--output", str(destination))
            self.assertEqual(json.loads(destination.read_text())["candidate"]["id"], identifier)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.cli(cwd, "candidate", "export", identifier, "--output", str(destination), success=False)
            repeat = json.loads(self.cli(cwd, "scan", str(FIXTURES), "--json").stdout)
            self.assertEqual(repeat["unchanged"], 4)

    def test_default_provider_discovery_stays_in_isolated_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            for folder, provider in (("isolated-claude/projects/demo", "claude"), ("isolated-codex/sessions/demo", "codex")):
                target = cwd / folder
                target.mkdir(parents=True)
                records = json.loads((FIXTURES / f"{provider}-a.json").read_text())
                (target / "invented.jsonl").write_text("\n".join(json.dumps(row) for row in records))
            first = json.loads(self.cli(cwd, "scan", "--claude", "--json").stdout)
            second = json.loads(self.cli(cwd, "scan", "--codex", "--json").stdout)
            self.assertEqual(first["imported"], 1)
            self.assertEqual(second["imported"], 1)
            self.cli(cwd, "analyse")
            rows = json.loads(self.cli(cwd, "candidates", "--json").stdout)
            self.assertEqual(rows[0]["occurrences"], 2)

    def test_error_paths_and_database_option_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            self.cli(cwd, "scan", str(cwd / "missing"), success=False)
            self.cli(cwd, "scan", success=False)
            self.cli(cwd, "analyse", "--min-length", "11", success=False)
            self.cli(cwd, "candidate", "show", "missing", success=False)
            db = str(cwd / "alternative.sqlite3")
            self.cli(cwd, "--db", db, "scan", str(FIXTURES))
            self.cli(cwd, "analyse", "--db", db)
            self.assertEqual(len(json.loads(self.cli(cwd, "candidates", "--db", db, "--json").stdout)), 1)


if __name__ == "__main__":
    unittest.main()
