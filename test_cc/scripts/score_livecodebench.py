#!/usr/bin/env python3
"""Score Claude Code solutions with EvalScope's LiveCodeBench evaluator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evalscope.benchmarks.live_code_bench.evaluate_utils import codegen_metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--timeout-sec", type=int, default=6)
    parser.add_argument("--agent-results-glob", default="agent_results*.jsonl")
    parser.add_argument("--allow-unsafe-local-eval", action="store_true")
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise SystemExit(f"Missing required file: {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def workspace_name(task: dict[str, Any]) -> str:
    return f"{task['row_index']:04d}_{str(task['task_id']).replace('/', '_')}"


def test_codes(results: dict[int, list[list[Any]]]) -> list[Any]:
    return list(results.get(0, [[-1]])[0])


def main() -> None:
    args = parse_args()
    if args.timeout_sec <= 0:
        raise SystemExit("--timeout-sec must be positive")
    if not args.allow_unsafe_local_eval:
        raise SystemExit(
            "Refusing to execute model-produced code on this host. Run inside a sandbox, or explicitly pass "
            "--allow-unsafe-local-eval for a disposable host-only smoke test."
        )

    visible_tasks = load_jsonl(args.run_dir / "manifest.jsonl")
    judge_cases = load_jsonl(args.run_dir / "judge_cases.jsonl")
    result_paths = sorted(args.run_dir.glob(args.agent_results_glob))
    if not result_paths:
        raise SystemExit(f"No agent result files match: {args.agent_results_glob}")
    agents: dict[int, dict[str, Any]] = {}
    for result_path in result_paths:
        for entry in load_jsonl(result_path):
            row_index = entry["row_index"]
            if row_index in agents:
                raise SystemExit(f"Duplicate agent result for row {row_index}: {result_path}")
            agents[row_index] = entry
    judge_by_index = {entry["row_index"]: entry for entry in judge_cases}
    scores: list[dict[str, Any]] = []

    for task in visible_tasks:
        agent = agents.get(task["row_index"], {})
        solution_path = args.run_dir / "agent_workspaces" / workspace_name(task) / "solution.py"
        score: dict[str, Any] = {"task_id": task["task_id"], "row_index": task["row_index"]}
        if agent.get("status") != "completed" or not solution_path.is_file():
            score.update(passed=False, status="no_submission")
        else:
            code = solution_path.read_text(encoding="utf-8")
            sample = {"input_output": judge_by_index[task["row_index"]]["evaluation_sample"]}
            _, results, _ = codegen_metrics(
                [sample], [[code]], k_list=[1], num_process_evaluate=1, timeout=args.timeout_sec, debug=False
            )
            codes = test_codes(results)
            score.update(passed=all(code is True for code in codes), status="judged", test_result_codes=codes)
        scores.append(score)

    output = args.run_dir / "scores.jsonl"
    output.write_text(
        "".join(json.dumps(score, ensure_ascii=False, sort_keys=True) + "\n" for score in scores), encoding="utf-8"
    )
    passed = sum(score["passed"] for score in scores)
    summary = {
        "tasks": len(scores),
        "passed": passed,
        "pass_at_1": passed / len(scores) if scores else 0.0,
        "timeout_sec": args.timeout_sec,
    }
    (args.run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
