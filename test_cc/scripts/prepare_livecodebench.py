#!/usr/bin/env python3
"""Freeze an ordered LiveCodeBench slice for Claude Code evaluation."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evalscope.api.dataset.hub import load_dataset_from_hub
from evalscope.benchmarks.live_code_bench.load_utils import transform


DATASET_ID = "evalscope/livecodebench_code_generation_lite_parquet"
SUBSET = "release_latest"
SPLIT = "test"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=64)
    parser.add_argument("--dataset-hub", choices=("modelscope",), default="modelscope")
    parser.add_argument("--dataset-dir", type=Path, default=Path("test_cc/data"))
    return parser.parse_args()


def json_line(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)


def task_id(record: dict[str, Any], row_index: int) -> str:
    for key in ("question_id", "task_id", "problem_id", "id"):
        value = record.get(key)
        if value not in (None, ""):
            return str(value)
    return f"row-{row_index:04d}"


def visible_prompt(record: dict[str, Any]) -> str:
    return (
        "### Question:\n"
        f"{record['question_content']}\n\n"
        f"{record['format_prompt']}"
        " ### Answer: (use the provided format with backticks)\n"
    )


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    with path.open("w", encoding="utf-8") as output:
        for record in records:
            line = json_line(record) + "\n"
            output.write(line)
            digest.update(line.encode("utf-8"))
    return digest.hexdigest()


def main() -> None:
    args = parse_args()
    if args.limit <= 0:
        raise SystemExit("--limit must be positive")
    if args.output_dir.exists():
        raise SystemExit(f"Refusing to overwrite existing output directory: {args.output_dir}")

    args.output_dir.mkdir(parents=True)
    args.dataset_dir.mkdir(parents=True, exist_ok=True)
    # Keep ModelScope's downloaded source under the requested test directory.
    os.environ["MODELSCOPE_CACHE"] = str(args.dataset_dir.resolve())
    dataset = load_dataset_from_hub(
        data_id_or_path=DATASET_ID,
        split=SPLIT,
        subset=SUBSET,
        data_source=args.dataset_hub,
    )

    visible_records: list[dict[str, Any]] = []
    judge_records: list[dict[str, Any]] = []
    for row_index, raw_record in enumerate(dataset):
        if len(visible_records) == args.limit:
            break
        record = transform(dict(raw_record))
        identifier = task_id(record, row_index)
        visible_records.append(
            {
                "row_index": row_index,
                "task_id": identifier,
                "contest_date": str(record["contest_date"]),
                "prompt": visible_prompt(record),
            }
        )
        judge_records.append(
            {
                "row_index": row_index,
                "task_id": identifier,
                "evaluation_sample": record["evaluation_sample"],
            }
        )

    if len(visible_records) != args.limit:
        raise SystemExit(f"Dataset has only {len(visible_records)} records, expected {args.limit}")

    visible_digest = write_jsonl(args.output_dir / "manifest.jsonl", visible_records)
    write_jsonl(args.output_dir / "judge_cases.jsonl", judge_records)
    metadata = {
        "created_at": datetime.now(UTC).isoformat(),
        "dataset_id": DATASET_ID,
        "dataset_hub": args.dataset_hub,
        "subset": SUBSET,
        "split": SPLIT,
        "selection": "source order, no shuffle, first limit records",
        "limit": args.limit,
        "evalscope_version": importlib.metadata.version("evalscope"),
        "manifest_sha256": visible_digest,
    }
    (args.output_dir / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Prepared {args.limit} tasks: {args.output_dir}")
    print(f"Visible manifest SHA-256: {visible_digest}")


if __name__ == "__main__":
    main()
