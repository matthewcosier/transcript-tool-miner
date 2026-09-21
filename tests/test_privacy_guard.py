"""The pre-commit guard is exercised in a disposable Git repository."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

GUARD = Path(__file__).resolve().parents[1] / "scripts" / "check_public_tree.py"


class PrivacyGuardTests(unittest.TestCase):
    def test_forced_staged_transcript_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            subprocess.run(["git", "init", "-q", str(cwd)], check=True)
            (cwd / "README.md").write_text("An invented test repository.\n")
            subprocess.run(["git", "add", "README.md"], cwd=cwd, check=True)
            clean = subprocess.run([sys.executable, str(GUARD)], cwd=cwd, capture_output=True)
            self.assertEqual(clean.returncode, 0)
            (cwd / "private-transcript.jsonl").write_text('{"synthetic":true}\n')
            subprocess.run(["git", "add", "-f", "private-transcript.jsonl"], cwd=cwd, check=True)
            blocked = subprocess.run([sys.executable, str(GUARD)], cwd=cwd, capture_output=True)
            self.assertEqual(blocked.returncode, 1)
            self.assertIn(b"private artifact type", blocked.stderr)


if __name__ == "__main__":
    unittest.main()
