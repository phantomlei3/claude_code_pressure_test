#!/usr/bin/env python3
"""Run persistent, concurrent Claude Code sessions on LiveCodeBench tasks."""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import shutil
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CLAUDE_ENV = REPO_ROOT / "claude.sh"
MAX_SESSIONS = 32
STREAM_READER_LIMIT = 16 * 1024 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--sessions", type=int, default=MAX_SESSIONS)
    parser.add_argument("--task-limit", type=int, help="Use only the first N selected tasks.")
    parser.add_argument("--repeat-forever", action="store_true", help="Repeat every selected task in each session.")
    parser.add_argument("--max-turns-per-session", type=int, help="Stop each session after N turns.")
    parser.add_argument("--row-start", type=int, default=0, help="Inclusive manifest row index.")
    parser.add_argument("--row-end", type=int, help="Exclusive manifest row index.")
    parser.add_argument("--turn-timeout-sec", type=int, default=1800)
    parser.add_argument("--shutdown-timeout-sec", type=int, default=30)
    parser.add_argument("--autocompact", default="128k")
    parser.add_argument(
        "--context-payload-chars",
        type=int,
        default=0,
        help="Approximate characters of visible manifest text included in each user turn.",
    )
    parser.add_argument("--workspace-dir", default="stress_sessions")
    parser.add_argument("--result-file", default="stress_results.jsonl")
    parser.add_argument("--claude-env", type=Path, default=DEFAULT_CLAUDE_ENV)
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-unsafe-host-execution", action="store_true")
    return parser.parse_args()


def load_manifest(run_dir: Path) -> list[dict[str, Any]]:
    manifest_path = run_dir / "manifest.jsonl"
    if not manifest_path.is_file():
        raise SystemExit(f"Missing frozen manifest: {manifest_path}")
    return [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines() if line]


def select_tasks(args: argparse.Namespace) -> list[dict[str, Any]]:
    tasks = [task for task in load_manifest(args.run_dir) if task["row_index"] >= args.row_start]
    if args.row_end is not None:
        tasks = [task for task in tasks if task["row_index"] < args.row_end]
    if args.task_limit is not None:
        tasks = tasks[: args.task_limit]
    if not tasks:
        raise SystemExit("No tasks selected")
    if not args.repeat_forever and len(tasks) < args.sessions:
        raise SystemExit(
            f"Selected {len(tasks)} tasks, but --sessions={args.sessions}; each persistent session needs a task"
        )
    return tasks


def assign_tasks(tasks: list[dict[str, Any]], sessions: int) -> list[list[dict[str, Any]]]:
    assignments: list[list[dict[str, Any]]] = [[] for _ in range(sessions)]
    for index, task in enumerate(tasks):
        assignments[index % sessions].append(task)
    return assignments


def safe_task_id(task_id: Any) -> str:
    value = str(task_id)
    return "".join(character if character.isalnum() or character in "-_" else "_" for character in value)


def turn_paths(session_dir: Path, turn: int, task: dict[str, Any]) -> tuple[Path, Path]:
    stem = f"turn_{turn:04d}_{task['row_index']:04d}_{safe_task_id(task['task_id'])}"
    return session_dir / "tasks" / f"{stem}.md", session_dir / "solutions" / f"{stem}.py"


def turn_prompt(session_id: int, turn: int, task_path: Path, solution_path: Path) -> str:
    return (
        f"You are persistent LiveCodeBench session {session_id:02d}, conversation turn {turn + 1}.\n"
        f"Read `{task_path.as_posix()}` and solve that programming problem. Work only in the current session "
        "workspace. You may reason, run examples, and create scratch files. Do not read any judge data or files "
        "outside this workspace.\n"
        f"Write the final submission to `{solution_path.as_posix()}`. The file must contain complete Python code "
        "only, without Markdown fences. Do not finish this turn until that file exists and is non-empty. "
        "Keep the earlier conversation context while handling this new LiveCodeBench task."
    )


def context_payload(tasks: list[dict[str, Any]], turn: int, target_chars: int) -> str:
    if target_chars == 0:
        return ""
    parts = [
        "\n\nContext load for this session. The following archived problem statements are "
        "only background text; solve the task named after this archive.\n"
    ]
    size = len(parts[0])
    for offset in itertools.count():
        task = tasks[(turn + offset) % len(tasks)]
        block = f"\nArchived task {turn}:{offset} ({task['task_id']}):\n{task['prompt']}\n"
        parts.append(block)
        size += len(block)
        if size >= target_chars:
            break
    return "".join(parts)


def input_event(prompt: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": prompt}],
        },
    }


def claude_command(args: argparse.Namespace) -> list[str]:
    return [
        "bash",
        "-lc",
        'source "$1"; shift; exec "$@"',
        "claude-env-wrapper",
        str(args.claude_env),
        args.claude_bin,
        "-p",
        "--bare",
        "--no-session-persistence",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--replay-user-messages",
        "--verbose",
        "--autocompact",
        args.autocompact,
        "--tools",
        "Bash,Edit,Read",
        "--permission-mode",
        "bypassPermissions",
        "--dangerously-skip-permissions",
    ]


def json_line(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


def append_jsonl(output: BinaryIO, value: dict[str, Any]) -> None:
    output.write(json_line(value))
    output.flush()


async def read_result(
    stream: asyncio.StreamReader,
    output: BinaryIO,
    timeout_sec: int,
) -> tuple[dict[str, Any] | None, int]:
    parse_errors = 0
    async with asyncio.timeout(timeout_sec):
        while True:
            line = await stream.readline()
            if not line:
                return None, parse_errors
            output.write(line)
            output.flush()
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                parse_errors += 1
                continue
            if event.get("type") == "result":
                return event, parse_errors


async def stop_process(process: asyncio.subprocess.Process, timeout_sec: int) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        await process.wait()
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_sec)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


def make_turn_result(
    session_id: int,
    turn: int,
    task: dict[str, Any],
    task_path: Path,
    solution_path: Path,
    started_at: str,
    status: str,
    result_event: dict[str, Any] | None = None,
    parse_errors: int = 0,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "error": error,
        "finished_at": datetime.now(UTC).isoformat(),
        "parse_errors": parse_errors,
        "result_event": result_event,
        "row_index": task["row_index"],
        "session_id": session_id,
        "solution": str(solution_path),
        "started_at": started_at,
        "status": status,
        "task_file": str(task_path),
        "task_id": task["task_id"],
        "turn": turn,
    }


async def run_session(
    args: argparse.Namespace,
    session_id: int,
    tasks: list[dict[str, Any]],
    workspace_root: Path,
    combined_log: BinaryIO,
    stop_event: asyncio.Event,
) -> tuple[int, int]:
    session_started_at = datetime.now(UTC).isoformat()
    session_dir = workspace_root / f"session_{session_id:02d}"
    (session_dir / "tasks").mkdir(parents=True)
    (session_dir / "solutions").mkdir()
    plan = {
        "session_id": session_id,
        "repeat_forever": args.repeat_forever,
        "tasks": [{"row_index": task["row_index"], "task_id": task["task_id"]} for task in tasks],
    }
    (session_dir / "session_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    statuses = {status: 0 for status in ("completed", "failed", "timeout", "not_run")}
    attempted = 0
    input_path = session_dir / "claude.input.jsonl"
    output_path = session_dir / "claude.stdout.jsonl"
    stderr_path = session_dir / "claude.stderr.log"
    result_path = session_dir / "turn_results.jsonl"
    with (
        input_path.open("wb") as input_log,
        output_path.open("wb") as output_log,
        stderr_path.open("wb") as stderr_log,
        result_path.open("wb") as result_log,
    ):
        process = await asyncio.create_subprocess_exec(
            *claude_command(args),
            cwd=session_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr_log,
            env=os.environ.copy(),
            start_new_session=True,
            limit=STREAM_READER_LIMIT,
        )
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("Claude process pipes were not created")

        session_failed = False
        task_stream = itertools.cycle(tasks) if args.repeat_forever else iter(tasks)
        for turn, task in enumerate(task_stream):
            if stop_event.is_set() or (
                args.max_turns_per_session is not None and turn >= args.max_turns_per_session
            ):
                break
            task_path, solution_path = turn_paths(session_dir, turn, task)
            relative_task_path = task_path.relative_to(session_dir)
            relative_solution_path = solution_path.relative_to(session_dir)
            task_path.write_text(task["prompt"], encoding="utf-8")
            prompt = context_payload(tasks, turn, args.context_payload_chars)
            prompt += turn_prompt(session_id, turn, relative_task_path, relative_solution_path)
            event = input_event(prompt)
            started_at = datetime.now(UTC).isoformat()
            input_log.write(json_line(event))
            input_log.flush()
            try:
                process.stdin.write(json_line(event))
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                result = make_turn_result(
                    session_id,
                    turn,
                    task,
                    task_path,
                    solution_path,
                    started_at,
                    "failed",
                    error="Persistent Claude process closed stdin before this turn",
                )
                statuses[result["status"]] += 1
                attempted += 1
                append_jsonl(result_log, result)
                append_jsonl(combined_log, result)
                session_failed = True
                break

            try:
                result_event, parse_errors = await read_result(process.stdout, output_log, args.turn_timeout_sec)
            except TimeoutError:
                result = make_turn_result(
                    session_id,
                    turn,
                    task,
                    task_path,
                    solution_path,
                    started_at,
                    "timeout",
                    error=f"No Claude result event within {args.turn_timeout_sec} seconds",
                )
                session_failed = True
            else:
                solution_ready = solution_path.is_file() and solution_path.stat().st_size > 0
                event_ok = result_event is not None and not result_event.get("is_error", False)
                if result_event is None:
                    status = "failed"
                    error = "Claude stdout closed before a result event"
                elif not event_ok:
                    status = "failed"
                    error = "Claude returned an error result"
                elif not solution_ready:
                    status = "failed"
                    error = "Claude completed without a non-empty solution file"
                else:
                    status = "completed"
                    error = None
                result = make_turn_result(
                    session_id,
                    turn,
                    task,
                    task_path,
                    solution_path,
                    started_at,
                    status,
                    result_event,
                    parse_errors,
                    error,
                )
                session_failed = status == "failed" and (
                    result_event is None or result_event.get("is_error", False)
                )

            statuses[result["status"]] += 1
            attempted += 1
            append_jsonl(result_log, result)
            append_jsonl(combined_log, result)
            if session_failed:
                break

        if not session_failed:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
            try:
                remaining = await asyncio.wait_for(process.stdout.read(), timeout=args.shutdown_timeout_sec)
                output_log.write(remaining)
                output_log.flush()
                await asyncio.wait_for(process.wait(), timeout=args.shutdown_timeout_sec)
            except TimeoutError:
                await stop_process(process, args.shutdown_timeout_sec)
        else:
            await stop_process(process, args.shutdown_timeout_sec)

    remaining = tasks[attempted:] if session_failed and not args.repeat_forever else []
    for turn, task in enumerate(remaining, start=attempted):
        task_path, solution_path = turn_paths(session_dir, turn, task)
        result = make_turn_result(
            session_id,
            turn,
            task,
            task_path,
            solution_path,
            datetime.now(UTC).isoformat(),
            "not_run",
            error="Persistent Claude session ended before this turn",
        )
        statuses[result["status"]] += 1
        with result_path.open("ab") as result_log:
            append_jsonl(result_log, result)
        append_jsonl(combined_log, result)
    summary = {
        "completed": statuses["completed"],
        "finished_at": datetime.now(UTC).isoformat(),
        "process_id": process.pid,
        "return_code": process.returncode,
        "session_id": session_id,
        "started_at": session_started_at,
        "statuses": statuses,
        "tasks": sum(statuses.values()),
    }
    (session_dir / "session_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return sum(statuses.values()), statuses["completed"]


async def run_all(
    args: argparse.Namespace,
    assignments: list[list[dict[str, Any]]],
    workspace_root: Path,
    results_path: Path,
) -> tuple[int, int]:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop_event.set)
    with results_path.open("wb") as combined_log:
        counts = await asyncio.gather(
            *(
                run_session(args, session_id, tasks, workspace_root, combined_log, stop_event)
                for session_id, tasks in enumerate(assignments)
            )
        )
    return sum(count for count, _ in counts), sum(completed for _, completed in counts)


def validate_args(args: argparse.Namespace) -> None:
    if args.sessions <= 0:
        raise SystemExit("--sessions must be positive")
    if args.sessions > MAX_SESSIONS:
        raise SystemExit(f"--sessions must not exceed {MAX_SESSIONS}")
    if args.task_limit is not None and args.task_limit <= 0:
        raise SystemExit("--task-limit must be positive")
    if args.max_turns_per_session is not None and args.max_turns_per_session <= 0:
        raise SystemExit("--max-turns-per-session must be positive")
    if args.context_payload_chars < 0 or args.context_payload_chars > 200000:
        raise SystemExit("--context-payload-chars must be between 0 and 200000")
    if args.row_start < 0 or (args.row_end is not None and args.row_end <= args.row_start):
        raise SystemExit("--row-start must be non-negative and --row-end must be greater than it")
    if args.turn_timeout_sec <= 0 or args.shutdown_timeout_sec <= 0:
        raise SystemExit("--turn-timeout-sec and --shutdown-timeout-sec must be positive")
    if not args.claude_env.is_file():
        raise SystemExit(f"Missing Claude environment file: {args.claude_env}")
    if shutil.which(args.claude_bin) is None:
        raise SystemExit(f"Claude executable is not available: {args.claude_bin}")
    if not args.dry_run and not args.allow_unsafe_host_execution:
        raise SystemExit(
            "Refusing unisolated agent execution. Use a disposable container or pass --allow-unsafe-host-execution."
        )


def main() -> None:
    args = parse_args()
    validate_args(args)
    tasks = select_tasks(args)
    assignments = (
        [tasks for _ in range(args.sessions)]
        if args.repeat_forever
        else assign_tasks(tasks, args.sessions)
    )
    if args.dry_run:
        counts = [len(assignment) for assignment in assignments]
        print(
            f"Planned persistent Claude sessions: {args.sessions}, tasks: {len(tasks)}, "
            f"tasks per session: {min(counts)}..{max(counts)}, "
            f"repeat forever: {args.repeat_forever}"
        )
        return

    workspace_root = (args.run_dir / args.workspace_dir).resolve()
    if args.run_dir.resolve() not in workspace_root.parents:
        raise SystemExit(f"Workspace directory must be below the run directory: {args.workspace_dir}")
    results_path = args.run_dir / args.result_file
    if workspace_root.exists():
        raise SystemExit(f"Refusing to reuse persistent session workspaces: {workspace_root}")
    if results_path.exists():
        raise SystemExit(f"Refusing to overwrite existing result file: {results_path}")
    workspace_root.mkdir(parents=True)

    metadata = {
        "autocompact": args.autocompact,
        "claude_context": {
            name: os.environ.get(name)
            for name in (
                "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
                "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
                "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
            )
        },
        "context_payload_chars": args.context_payload_chars,
        "claude_env": str(args.claude_env),
        "created_at": datetime.now(UTC).isoformat(),
        "row_end": args.row_end,
        "row_start": args.row_start,
        "repeat_forever": args.repeat_forever,
        "max_turns_per_session": args.max_turns_per_session,
        "sessions": args.sessions,
        "task_count": len(tasks),
        "turn_timeout_sec": args.turn_timeout_sec,
    }
    (workspace_root / "stress_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    total, completed = asyncio.run(run_all(args, assignments, workspace_root, results_path))
    print(
        f"Persistent Claude sessions: {args.sessions}, tasks: {total}, "
        f"completed: {completed}, results: {results_path}"
    )
    if completed != total:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
