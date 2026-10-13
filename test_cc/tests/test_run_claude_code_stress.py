#!/usr/bin/env python3
"""Integration tests for the persistent Claude Code stress runner."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "test_cc" / "scripts" / "run_claude_code_stress.py"


class ClaudeCodeStressRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        manifest = [
            {
                "row_index": index,
                "task_id": f"task-{index}",
                "prompt": f"Solve LiveCodeBench problem {index}.",
            }
            for index in range(4)
        ]
        (self.run_dir / "manifest.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in manifest),
            encoding="utf-8",
        )
        self.env_file = self.root / "claude.sh"
        self.env_file.write_text("export ANTHROPIC_BASE_URL=http://127.0.0.1:7442\n", encoding="utf-8")
        self.fake_claude = self.root / "claude"
        self.fake_claude.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json
                import os
                import re
                import sys
                from pathlib import Path

                Path("fake_claude.pid").write_text(str(os.getpid()), encoding="utf-8")
                print(json.dumps({"type": "system", "subtype": "init", "session_id": str(os.getpid())}), flush=True)
                for line in sys.stdin:
                    event = json.loads(line)
                    text = event["message"]["content"][0]["text"]
                    match = re.search(r"write the final submission to `([^`]+)`", text, re.IGNORECASE)
                    if match is None:
                        raise SystemExit("missing solution path")
                    solution = Path(match.group(1))
                    solution.parent.mkdir(parents=True, exist_ok=True)
                    solution.write_text("print('ok')\\n", encoding="utf-8")
                    print(json.dumps({"type": "user", "message": event["message"]}), flush=True)
                    print(json.dumps({
                        "type": "assistant",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                    }), flush=True)
                    print(json.dumps({
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "session_id": str(os.getpid()),
                        "num_turns": 1,
                        "result": "done",
                    }), flush=True)
                print("fake stderr", file=sys.stderr, flush=True)
                """
            ),
            encoding="utf-8",
        )
        self.fake_claude.chmod(0o755)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _run(self, *extra_args: str) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(RUNNER),
            "--run-dir",
            str(self.run_dir),
            "--sessions",
            "2",
            "--claude-bin",
            str(self.fake_claude),
            "--claude-env",
            str(self.env_file),
            "--allow-unsafe-host-execution",
            *extra_args,
        ]
        return subprocess.run(command, text=True, capture_output=True, timeout=20, check=False)

    def test_two_persistent_sessions_process_four_tasks_and_preserve_traces(self) -> None:
        completed = self._run()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        session_dirs = sorted((self.run_dir / "stress_sessions").glob("session_*"))
        self.assertEqual(len(session_dirs), 2)
        for session_dir in session_dirs:
            self.assertTrue((session_dir / "fake_claude.pid").is_file())
            session_summary = json.loads((session_dir / "session_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(session_summary["return_code"], 0)
            self.assertEqual(session_summary["completed"], 2)
            input_events = (session_dir / "claude.input.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(input_events), 2)
            output_events = (session_dir / "claude.stdout.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(sum('"type": "result"' in line for line in output_events), 2)
            self.assertEqual((session_dir / "claude.stderr.log").read_text(encoding="utf-8"), "fake stderr\n")
            turn_results = (session_dir / "turn_results.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(turn_results), 2)
            self.assertTrue(all(json.loads(line)["status"] == "completed" for line in turn_results))
            self.assertEqual(len(list((session_dir / "solutions").glob("*.py"))), 2)
            self.assertEqual(len(list((session_dir / "tasks").glob("*.md"))), 2)

        combined = [
            json.loads(line)
            for line in (self.run_dir / "stress_results.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(combined), 4)
        self.assertTrue(all(result["status"] == "completed" for result in combined))

    def test_more_than_32_sessions_is_rejected(self) -> None:
        completed = self._run("--sessions", "33", "--dry-run")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("must not exceed 32", completed.stderr)

    def test_repeated_sessions_each_restart_after_the_full_manifest(self) -> None:
        completed = self._run("--repeat-forever", "--max-turns-per-session", "6")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        for session_id in range(2):
            session_dir = self.run_dir / "stress_sessions" / f"session_{session_id:02d}"
            results = [
                json.loads(line)
                for line in (session_dir / "turn_results.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([result["row_index"] for result in results], [0, 1, 2, 3, 0, 1])
            self.assertEqual([result["turn"] for result in results], list(range(6)))
            self.assertEqual(len(list((session_dir / "solutions").glob("*.py"))), 6)
            self.assertEqual(json.loads((session_dir / "session_summary.json").read_text())["completed"], 6)
        combined = (self.run_dir / "stress_results.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(combined), 12)

    def test_error_result_stops_repeated_session(self) -> None:
        script = self.fake_claude.read_text(encoding="utf-8")
        script = script.replace('"is_error": False,', '"is_error": True,')
        script = script.replace('"result": "done",', '"result": "automatic compaction failed",')
        self.fake_claude.write_text(script, encoding="utf-8")

        completed = self._run("--repeat-forever", "--max-turns-per-session", "6")

        self.assertEqual(completed.returncode, 2)
        for session_id in range(2):
            session_dir = self.run_dir / "stress_sessions" / f"session_{session_id:02d}"
            inputs = (session_dir / "claude.input.jsonl").read_text(encoding="utf-8").splitlines()
            results = (session_dir / "turn_results.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(inputs), 1)
            self.assertEqual(len(results), 1)
            self.assertEqual(json.loads(results[0])["status"], "failed")

    def test_context_payload_uses_visible_tasks_and_preserves_solution_instruction(self) -> None:
        completed = self._run("--context-payload-chars", "4000")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        session_dir = self.run_dir / "stress_sessions" / "session_00"
        events = [
            json.loads(line)
            for line in (session_dir / "claude.input.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        prompts = [event["message"]["content"][0]["text"] for event in events]
        self.assertEqual(len(prompts), 2)
        self.assertTrue(all(len(prompt) >= 4000 for prompt in prompts))
        self.assertIn("Solve LiveCodeBench problem 0.", prompts[0])
        self.assertIn("Solve LiveCodeBench problem 2.", prompts[1])
        self.assertTrue(prompts[0].endswith("Keep the earlier conversation context while handling this new LiveCodeBench task."))
        metadata = json.loads((self.run_dir / "stress_sessions" / "stress_metadata.json").read_text())
        self.assertEqual(metadata["context_payload_chars"], 4000)


if __name__ == "__main__":
    unittest.main()
