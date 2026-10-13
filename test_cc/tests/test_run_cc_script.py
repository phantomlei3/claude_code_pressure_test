#!/usr/bin/env python3
"""Integration tests for the repeatable Claude Code stress entrypoint."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_SCRIPT = REPO_ROOT / "run_cc.sh"


class RunCcScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self._temp_dir.name)
        self.runs_root = self.root / "runs"
        self.run_dir = self.runs_root / "prepared"
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "manifest.jsonl").write_text("{}\n", encoding="utf-8")
        (self.run_dir / "judge_cases.jsonl").write_text("judge\n", encoding="utf-8")
        (self.run_dir / "run_metadata.json").write_text("{}\n", encoding="utf-8")
        session_dir = self.run_dir / "stress_sessions"
        session_dir.mkdir()
        (session_dir / "old.trace").write_text("old\n", encoding="utf-8")
        (self.run_dir / "stress_results.jsonl").write_text("old result\n", encoding="utf-8")
        self.fake_runner = self.root / "fake_runner.py"
        self.fake_runner.write_text(
            textwrap.dedent(
                """\
                import json
                import os
                import sys
                from pathlib import Path

                args = sys.argv[1:]
                run_dir = Path(args[args.index("--run-dir") + 1])
                (run_dir / "runner_args.json").write_text(json.dumps(args), encoding="utf-8")
                names = (
                    "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
                    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
                    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
                )
                (run_dir / "runner_env.json").write_text(
                    json.dumps({name: os.environ[name] for name in names}), encoding="utf-8"
                )
                """
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _run(self, run_dir: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        for name in (
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
        ):
            env.pop(name, None)
        env.update(
            {
                "CC_RUNS_ROOT": str(self.runs_root),
                "CC_STRESS_RUNNER": str(self.fake_runner),
            }
        )
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(RUN_SCRIPT), str(run_dir)],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
            env=env,
        )

    def test_removes_only_previous_stress_output_and_starts_repeated_sessions(self) -> None:
        completed = self._run(self.run_dir)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse((self.run_dir / "stress_sessions").exists())
        self.assertFalse((self.run_dir / "stress_results.jsonl").exists())
        self.assertTrue((self.run_dir / "manifest.jsonl").is_file())
        self.assertTrue((self.run_dir / "judge_cases.jsonl").is_file())
        args = json.loads((self.run_dir / "runner_args.json").read_text(encoding="utf-8"))
        self.assertEqual(args[args.index("--sessions") + 1], "16")
        self.assertEqual(args[args.index("--autocompact") + 1], "512k")
        self.assertEqual(args[args.index("--context-payload-chars") + 1], "16384")
        self.assertEqual(Path(args[args.index("--run-dir") + 1]), self.run_dir.resolve())
        self.assertIn("--allow-unsafe-host-execution", args)
        self.assertIn("--repeat-forever", args)
        self.assertEqual(
            json.loads((self.run_dir / "runner_env.json").read_text(encoding="utf-8")),
            {
                "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "512000",
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "512000",
                "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80",
            },
        )

    def test_context_settings_follow_overrides(self) -> None:
        completed = self._run(
            self.run_dir,
            {
                "CC_AUTOCOMPACT": "384k",
                "CC_MAX_CONTEXT_TOKENS": "600000",
                "CC_AUTOCOMPACT_PCT": "70",
                "CC_CONTEXT_PAYLOAD_CHARS": "12000",
                "CC_SESSIONS": "16",
            },
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        args = json.loads((self.run_dir / "runner_args.json").read_text(encoding="utf-8"))
        self.assertEqual(args[args.index("--sessions") + 1], "16")
        self.assertEqual(args[args.index("--context-payload-chars") + 1], "12000")
        self.assertEqual(
            json.loads((self.run_dir / "runner_env.json").read_text(encoding="utf-8")),
            {
                "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "600000",
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "384000",
                "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "70",
            },
        )

    def test_rejects_run_directory_outside_configured_runs_root(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "stress_sessions" / "keep.trace"
        marker.parent.mkdir()
        marker.write_text("keep\n", encoding="utf-8")

        completed = self._run(outside)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("must be directly below", completed.stderr)
        self.assertTrue(marker.is_file())

    def test_invalid_payload_size_preserves_previous_results(self) -> None:
        completed = self._run(self.run_dir, {"CC_CONTEXT_PAYLOAD_CHARS": "200001"})

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("CC_CONTEXT_PAYLOAD_CHARS", completed.stderr)
        self.assertTrue((self.run_dir / "stress_sessions" / "old.trace").is_file())
        self.assertTrue((self.run_dir / "stress_results.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
