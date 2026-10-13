#!/usr/bin/env python3
"""Durably fetch the complete official LiveCodeBench release through HF ranges."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from evalscope.benchmarks.live_code_bench.load_utils import transform


DATASET_ID = "livecodebench/code_generation_lite"
REVISION = "0fe84c3912ea0c4d4a78037083943e8f0c4dd505"
DATA_FILES = ("test.jsonl", "test2.jsonl", "test3.jsonl", "test4.jsonl", "test5.jsonl", "test6.jsonl")
URL_TEMPLATE = "https://huggingface.co/datasets/{dataset}/resolve/{revision}/{file_name}"
STATE_FILE = "download_state.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=1055)
    parser.add_argument("--range-bytes", type=int, default=128 * 1024 * 1024)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def json_line(record: dict[str, Any]) -> bytes:
    return (json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode("utf-8")


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


def new_state() -> dict[str, Any]:
    return {
        "file_index": 0,
        "offset": 0,
        "pending_b64": "",
        "records": 0,
        "manifest_size": 0,
        "judge_size": 0,
        "source_offsets": {},
    }


def checkpoint_path(output_dir: Path) -> Path:
    return output_dir / STATE_FILE


def save_checkpoint(output_dir: Path, state: dict[str, Any]) -> None:
    temporary = checkpoint_path(output_dir).with_suffix(".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(checkpoint_path(output_dir))


def restore_output(output_dir: Path) -> dict[str, Any]:
    path = checkpoint_path(output_dir)
    if not path.is_file():
        raise SystemExit(f"Cannot resume without checkpoint: {path}")
    state = json.loads(path.read_text(encoding="utf-8"))
    required = {"file_index", "offset", "pending_b64", "records", "manifest_size", "judge_size", "source_offsets"}
    if not required.issubset(state):
        raise SystemExit(f"Invalid checkpoint: {path}")
    for file_name, size_key in (("manifest.jsonl", "manifest_size"), ("judge_cases.jsonl", "judge_size")):
        path = output_dir / file_name
        if not path.is_file() or path.stat().st_size < state[size_key]:
            raise SystemExit(f"Checkpoint does not match output: {path}")
        with path.open("r+b") as output:
            output.truncate(state[size_key])
    return state


def init_output(args: argparse.Namespace) -> dict[str, Any]:
    if args.output_dir.exists():
        if not args.resume:
            raise SystemExit(f"Refusing to overwrite existing output directory: {args.output_dir}")
        return restore_output(args.output_dir)
    if args.resume:
        raise SystemExit(f"Cannot resume missing directory: {args.output_dir}")
    args.output_dir.mkdir(parents=True)
    (args.output_dir / "manifest.jsonl").touch()
    (args.output_dir / "judge_cases.jsonl").touch()
    state = new_state()
    save_checkpoint(args.output_dir, state)
    return state


def fetch_range(file_name: str, offset: int, range_bytes: int) -> tuple[bytes, int]:
    url = URL_TEMPLATE.format(dataset=DATASET_ID, revision=REVISION, file_name=file_name)
    last_error: Exception | None = None
    for attempt in range(1, 6):
        try:
            response = requests.get(
                url,
                headers={"Range": f"bytes={offset}-{offset + range_bytes - 1}"},
                timeout=(20, 300),
            )
            if response.status_code != 206:
                raise RuntimeError(f"HTTP {response.status_code}")
            content_range = response.headers.get("Content-Range", "")
            if not content_range.startswith(f"bytes {offset}-") or "/" not in content_range:
                raise RuntimeError(f"unexpected Content-Range: {content_range}")
            total = int(content_range.rsplit("/", maxsplit=1)[1])
            if not response.content:
                raise RuntimeError("received an empty range")
            return response.content, total
        except (requests.RequestException, RuntimeError, ValueError) as error:
            last_error = error
            if attempt < 5:
                print(f"Retry {attempt}/5: {file_name} at byte {offset}: {error}", flush=True)
    raise RuntimeError(f"Could not fetch {file_name} at byte {offset}: {last_error}")


def fsync(output) -> int:
    output.flush()
    os.fsync(output.fileno())
    return output.tell()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def append_record(manifest, judge, state: dict[str, Any], raw_record: dict[str, Any]) -> None:
    record = transform(raw_record)
    row_index = state["records"]
    identifier = task_id(record, row_index)
    manifest.write(
        json_line(
            {
                "row_index": row_index,
                "task_id": identifier,
                "contest_date": str(record["contest_date"]),
                "prompt": visible_prompt(record),
            }
        )
    )
    judge.write(
        json_line(
            {
                "row_index": row_index,
                "task_id": identifier,
                "evaluation_sample": record["evaluation_sample"],
            }
        )
    )
    state["records"] += 1


def main() -> None:
    args = parse_args()
    if args.limit <= 0 or args.range_bytes <= 0:
        raise SystemExit("--limit and --range-bytes must be positive")
    state = init_output(args)
    manifest_path = args.output_dir / "manifest.jsonl"
    judge_path = args.output_dir / "judge_cases.jsonl"

    with manifest_path.open("ab") as manifest, judge_path.open("ab") as judge:
        while state["records"] < args.limit and state["file_index"] < len(DATA_FILES):
            file_name = DATA_FILES[state["file_index"]]
            chunk, total_bytes = fetch_range(file_name, state["offset"], args.range_bytes)
            state["offset"] += len(chunk)
            pending = base64.b64decode(state["pending_b64"]) + chunk
            lines = pending.split(b"\n")
            pending = lines.pop()
            for line in lines:
                if line and state["records"] < args.limit:
                    append_record(manifest, judge, state, json.loads(line))
            if state["offset"] >= total_bytes:
                if pending.strip() and state["records"] < args.limit:
                    append_record(manifest, judge, state, json.loads(pending))
                state["source_offsets"][file_name] = state["offset"]
                state.update(file_index=state["file_index"] + 1, offset=0)
                pending = b""
            if state["records"] == args.limit:
                pending = b""
            state["pending_b64"] = base64.b64encode(pending).decode("ascii")
            state["manifest_size"] = fsync(manifest)
            state["judge_size"] = fsync(judge)
            save_checkpoint(args.output_dir, state)
            print(
                f"Downloaded {state['records']}/{args.limit} records; "
                f"{file_name} byte {state['offset']}/{total_bytes}",
                flush=True,
            )

    if state["records"] != args.limit:
        raise SystemExit(f"Upstream data has only {state['records']} records, expected {args.limit}")
    metadata = {
        "created_at": datetime.now(UTC).isoformat(),
        "dataset_id": DATASET_ID,
        "source": "official Hugging Face LiveCodeBench range download",
        "revision": REVISION,
        "source_files_read": DATA_FILES[: state["file_index"]],
        "source_offsets": state["source_offsets"],
        "selection": "source order, no shuffle, first limit records",
        "limit": args.limit,
        "evalscope_version": importlib.metadata.version("evalscope"),
        "manifest_sha256": sha256_file(manifest_path),
    }
    (args.output_dir / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    checkpoint_path(args.output_dir).unlink()
    print(f"Prepared {args.limit} tasks: {args.output_dir}")
    print(f"Visible manifest SHA-256: {metadata['manifest_sha256']}")


if __name__ == "__main__":
    main()
