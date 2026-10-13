#!/usr/bin/env python3
"""Fetch a small ordered LiveCodeBench slice from the official HF source.

This is a network fallback for the same raw LiveCodeBench schema consumed by
EvalScope's adapter. It uses HTTP byte ranges, so creating a 64-task run does
not require downloading the multi-gigabyte upstream repository.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import requests

from evalscope.benchmarks.live_code_bench.load_utils import transform


DATASET_ID = "livecodebench/code_generation_lite"
REVISION = "0fe84c3912ea0c4d4a78037083943e8f0c4dd505"
DATA_FILES = ("test.jsonl", "test2.jsonl", "test3.jsonl", "test4.jsonl", "test5.jsonl", "test6.jsonl")
URL_TEMPLATE = "https://huggingface.co/datasets/{dataset}/resolve/{revision}/{file_name}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=64)
    parser.add_argument("--range-bytes", type=int, default=16 * 1024 * 1024)
    return parser.parse_args()


def json_line(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    with path.open("w", encoding="utf-8") as output:
        for record in records:
            line = json_line(record) + "\n"
            output.write(line)
            digest.update(line.encode("utf-8"))
    return digest.hexdigest()


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


def iter_records(file_name: str, range_bytes: int) -> Iterator[tuple[dict[str, Any], int]]:
    url = URL_TEMPLATE.format(dataset=DATASET_ID, revision=REVISION, file_name=file_name)
    offset = 0
    pending = b""
    while True:
        response = requests.get(
            url,
            headers={"Range": f"bytes={offset}-{offset + range_bytes - 1}"},
            timeout=(15, 180),
        )
        if response.status_code != 206:
            raise RuntimeError(f"Range request for {file_name} at {offset} returned HTTP {response.status_code}")
        content_range = response.headers.get("Content-Range", "")
        expected_prefix = f"bytes {offset}-"
        if not content_range.startswith(expected_prefix):
            raise RuntimeError(f"Unexpected Content-Range for {file_name}: {content_range}")
        chunk = response.content
        if not chunk:
            return
        offset += len(chunk)
        pending += chunk
        lines = pending.split(b"\n")
        pending = lines.pop()
        for line in lines:
            if line:
                yield json.loads(line), offset
        if len(chunk) < range_bytes:
            if pending.strip():
                yield json.loads(pending), offset
            return


def main() -> None:
    args = parse_args()
    if args.limit <= 0 or args.range_bytes <= 0:
        raise SystemExit("--limit and --range-bytes must be positive")
    if args.output_dir.exists():
        raise SystemExit(f"Refusing to overwrite existing output directory: {args.output_dir}")
    args.output_dir.mkdir(parents=True)

    visible_records: list[dict[str, Any]] = []
    judge_records: list[dict[str, Any]] = []
    source_bytes = 0
    source_files: list[str] = []
    for file_name in DATA_FILES:
        source_files.append(file_name)
        for raw_record, source_bytes in iter_records(file_name, args.range_bytes):
            row_index = len(visible_records)
            record = transform(raw_record)
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
            if len(visible_records) == args.limit:
                break
        if len(visible_records) == args.limit:
            break
    if len(visible_records) != args.limit:
        raise SystemExit(f"Upstream data has only {len(visible_records)} records, expected {args.limit}")

    visible_digest = write_jsonl(args.output_dir / "manifest.jsonl", visible_records)
    write_jsonl(args.output_dir / "judge_cases.jsonl", judge_records)
    metadata = {
        "created_at": datetime.now(UTC).isoformat(),
        "dataset_id": DATASET_ID,
        "source": "official Hugging Face LiveCodeBench range download",
        "revision": REVISION,
        "source_files_read": source_files,
        "source_bytes_read_from_last_file": source_bytes,
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
