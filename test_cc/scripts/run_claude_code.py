#!/usr/bin/env python3
"""Run one Claude Code trajectory per frozen LiveCodeBench task."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_ENV = REPO_ROOT / "claude.sh"
TASK_INSTRUCTION = """Read TASK.md and solve the programming problem. Work only in the current directory.
You may reason and create local scratch files, but do not read files outside this task workspace.
Write the final complete Python submission to solution.py. The file must contain code only, with no Markdown fences.
Do not report a solution solely in chat: solution.py is the submission that will be judged.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--max-concurrency", type=int, default=8)
    parser.add_argument("--task-timeout-sec", type=int, default=1800)
    parser.add_argument("--autocompact", default="128k")
    parser.add_argument("--task-limit", type=int, help="Run only the first N frozen tasks (for preflight).")
    parser.add_argument("--row-start", type=int, default=0, help="Inclusive frozen row index for this batch.")
    parser.add_argument("--row-end", type=int, help="Exclusive frozen row index for this batch.")
    parser.add_argument("--result-file", default="agent_results.jsonl", help="Batch result file relative to --run-dir.")
    parser.add_argument(
        "--retry-incomplete-results-glob",
        help="Run only rows whose latest matching result has a non-completed status.",
    )
    parser.add_argument(
        "--workspace-dir",
        default="agent_workspaces",
        help="Workspace directory relative to --run-dir (use a new directory for retries).",
    )
    parser.add_argument(
        "--task-instruction-file",
        type=Path,
        help="Optional UTF-8 file that replaces the default Claude task instruction.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-unsafe-host-execution", action="store_true")
    return parser.parse_args()


def load_manifest(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "manifest.jsonl"
    if not path.is_file():
        raise SystemExit(f"Missing frozen manifest: {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def workspace_name(task: dict[str, Any]) -> str:
    return f"{task['row_index']:04d}_{str(task['task_id']).replace('/', '_')}"


def materialize_task(run_dir: Path, task: dict[str, Any], workspace_dir: str) -> Path:
    workspace_root = (run_dir / workspace_dir).resolve()
    if run_dir.resolve() not in workspace_root.parents:
        raise RuntimeError(f"Workspace directory must be below the run directory: {workspace_dir}")
    workspace = workspace_root / workspace_name(task)
    workspace.mkdir(parents=True, exist_ok=True)
    task_file = workspace / "TASK.md"
    if task_file.exists() and task_file.read_text(encoding="utf-8") != task["prompt"]:
        raise RuntimeError(f"Task workspace does not match frozen manifest: {workspace}")
    task_file.write_text(task["prompt"], encoding="utf-8")
    return workspace


def claude_command(task_instruction: str, autocompact: str) -> list[str]:
    return [
        "bash",
        "-lc",
        'source "$1"; shift; exec "$@"',
        "claude-env-wrapper",
        str(CLAUDE_ENV),
        "claude",
        "-p",
        task_instruction,
        "--bare",
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
        "--autocompact",
        autocompact,
        "--permission-mode",
        "bypassPermissions",
        "--dangerously-skip-permissions",
    ]


async def run_one(
    semaphore: asyncio.Semaphore,
    run_dir: Path,
    task: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    workspace = materialize_task(run_dir, task, args.workspace_dir)
    result: dict[str, Any] = {
        "task_id": task["task_id"],
        "row_index": task["row_index"],
        "workspace": str(workspace),
        "started_at": datetime.now(UTC).isoformat(),
    }
    if args.dry_run:
        result.update(status="planned", finished_at=datetime.now(UTC).isoformat())
        return result

    log_file = workspace / "claude.stream.jsonl"
    if (workspace / "solution.py").exists() or log_file.exists():
        raise RuntimeError(f"Refusing to reuse a task workspace: {workspace}")
    async with semaphore:
        with log_file.open("wb") as output:
            process = await asyncio.create_subprocess_exec(
                *claude_command(args.task_instruction, args.autocompact),
                cwd=workspace,
                stdout=output,
                stderr=asyncio.subprocess.STDOUT,
                env=os.environ.copy(),
            )
            try:
                return_code = await asyncio.wait_for(process.wait(), timeout=args.task_timeout_sec)
            except TimeoutError:
                process.kill()
                await process.wait()
                result.update(status="timeout", return_code=None)
            else:
                solution = workspace / "solution.py"
                if return_code == 0 and solution.is_file() and solution.stat().st_size > 0:
                    result.update(status="completed", return_code=return_code)
                else:
                    result.update(status="failed", return_code=return_code)
    result["finished_at"] = datetime.now(UTC).isoformat()
    return result


async def run_all(args: argparse.Namespace, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(args.max_concurrency)
    return await asyncio.gather(*(run_one(semaphore, args.run_dir, task, args) for task in tasks))


def main() -> None:
    args = parse_args()
    if args.max_concurrency <= 0 or args.task_timeout_sec <= 0:
        raise SystemExit("--max-concurrency and --task-timeout-sec must be positive")
    if args.row_start < 0 or (args.row_end is not None and args.row_end <= args.row_start):
        raise SystemExit("--row-start must be non-negative and --row-end must be greater than it")
    if not CLAUDE_ENV.is_file():
        raise SystemExit(f"Missing Claude environment file: {CLAUDE_ENV}")
    if shutil.which("claude") is None:
        raise SystemExit("claude is not on PATH")
    if not args.dry_run and not args.allow_unsafe_host_execution:
        raise SystemExit(
            "Refusing unisolated agent execution. Use a containerized worker, or explicitly pass "
            "--allow-unsafe-host-execution for a disposable host-only smoke test."
        )
    args.task_instruction = TASK_INSTRUCTION
    if args.task_instruction_file is not None:
        if not args.task_instruction_file.is_file():
            raise SystemExit(f"Missing task instruction file: {args.task_instruction_file}")
        args.task_instruction = args.task_instruction_file.read_text(encoding="utf-8")
        if not args.task_instruction.strip():
            raise SystemExit("Task instruction file must not be empty")

    tasks = [task for task in load_manifest(args.run_dir) if task["row_index"] >= args.row_start]
    if args.row_end is not None:
        tasks = [task for task in tasks if task["row_index"] < args.row_end]
    if args.task_limit is not None:
        if args.task_limit <= 0:
            raise SystemExit("--task-limit must be positive")
        tasks = tasks[:args.task_limit]
    if args.retry_incomplete_results_glob:
        prior_results: dict[int, dict[str, Any]] = {}
        for result_path in sorted(args.run_dir.glob(args.retry_incomplete_results_glob)):
            for entry in load_jsonl(result_path):
                prior_results[entry["row_index"]] = entry
        tasks = [task for task in tasks if prior_results.get(task["row_index"], {}).get("status") != "completed"]
    results = asyncio.run(run_all(args, tasks))
    results_path = args.run_dir / args.result_file
    if results_path.exists():
        raise SystemExit(f"Refusing to overwrite existing result file: {results_path}")
    results_path.write_text(
        "".join(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n" for result in results),
        encoding="utf-8",
    )
    completed = sum(result["status"] == "completed" for result in results)
    print(f"Agent tasks: {len(results)}, completed: {completed}, results: {results_path}")


if __name__ == "__main__":
    main()
