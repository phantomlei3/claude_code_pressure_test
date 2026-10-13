#!/usr/bin/env python3
"""Replace unsuccessful agent records with successful retry records."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )
    temporary.replace(path)


def workspace_name(task_id: str, row_index: int) -> str:
    return f"{row_index:04d}_{task_id.replace('/', '_')}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--retry-results", required=True, type=Path)
    parser.add_argument("--source-workspace-dir", default="retry_agent_workspaces")
    parser.add_argument("--target-workspace-dir", default="agent_workspaces")
    parser.add_argument("--original-results-glob", default="agent_results_[0-9]*_[0-9]*.jsonl")
    args = parser.parse_args()

    retry_path = args.retry_results if args.retry_results.is_absolute() else args.run_dir / args.retry_results
    retries = {row["row_index"]: row for row in load_jsonl(retry_path) if row.get("status") == "completed"}
    if not retries:
        print("No successful retry records to merge.")
        return

    replaced = 0
    for result_path in sorted(args.run_dir.glob(args.original_results_glob)):
        rows = load_jsonl(result_path)
        changed = False
        for index, row in enumerate(rows):
            retry = retries.get(row["row_index"])
            if retry is None or row.get("status") == "completed":
                continue
            source = args.run_dir / args.source_workspace_dir / workspace_name(retry["task_id"], retry["row_index"]) / "solution.py"
            target = args.run_dir / args.target_workspace_dir / workspace_name(row["task_id"], row["row_index"]) / "solution.py"
            if not source.is_file() or source.stat().st_size == 0:
                raise SystemExit(f"Successful retry has no solution file: {source}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            row = dict(retry)
            row["workspace"] = str(target.parent)
            rows[index] = row
            changed = True
            replaced += 1
        if changed:
            write_jsonl(result_path, rows)
    print(f"Merged {replaced} successful retries.")


if __name__ == "__main__":
    main()
