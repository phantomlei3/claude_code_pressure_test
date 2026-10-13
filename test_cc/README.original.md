# LiveCodeBench Claude Code runner

This directory evaluates xLLM's `GLM-5.3-W4A8` through **Claude Code** rather
than through EvalScope's ordinary OpenAI-compatible client. EvalScope supplies
the LiveCodeBench data and preserves its Python pass@1 evaluation semantics.

## Layout

- `scripts/prepare_livecodebench.py` freezes the first 64 records into a
  visible task manifest and a separate judge manifest.
- `scripts/run_claude_code.py` creates one workspace per task and runs at most
  eight Claude Code processes concurrently.
- `scripts/score_livecodebench.py` evaluates the resulting `solution.py`
  files with EvalScope's bundled LiveCodeBench evaluator.
- `runs/<name>/manifest.jsonl` contains only task information that may be shown
  to Claude Code. `judge_cases.jsonl` contains test cases and must never be
  exposed to an agent workspace.

## Prepare a reproducible 64-question run

Run from the repository root. The default EvalScope hub is ModelScope.

```bash
python testspace/test_cc/scripts/prepare_livecodebench.py \
  --limit 64 \
  --output-dir testspace/test_cc/runs/lcb64_$(date +%Y%m%d_%H%M%S)
```

The generated `run_metadata.json` records the dataset ID, EvalScope version,
selection order, and SHA-256 digest of the visible manifest. Do not change the
manifest once a run has started.

If the ModelScope endpoint is unavailable, the following fallback reads only
the needed prefix through byte ranges from the official upstream
LiveCodeBench Hugging Face repository. Its raw record fields are the schema
that EvalScope's LiveCodeBench adapter transforms and judges.

```bash
python testspace/test_cc/scripts/prepare_livecodebench_hf_range.py \
  --limit 64 \
  --output-dir testspace/test_cc/runs/lcb64_$(date +%Y%m%d_%H%M%S)
```

For the complete 1,055-task release, use the durable downloader below. It
checkpoints each source range; resume an interrupted transfer with the same
arguments plus `--resume`.

```bash
python testspace/test_cc/scripts/prepare_livecodebench_hf_full.py \
  --limit 1055 \
  --output-dir testspace/test_cc/runs/lcb1055_$(date +%Y%m%d_%H%M%S)
```

## Claude Code execution

First materialize and inspect the workspaces without calling the model:

```bash
python testspace/test_cc/scripts/run_claude_code.py \
  --run-dir testspace/test_cc/runs/<run-name> --dry-run
```

All real Claude Code invocations load `pd-testspace/claude.sh`, use a distinct
workspace, record stream JSON, and pass `--autocompact 128k`. Thus each agent
will compact its context automatically after reaching 128K.

The runner intentionally requires the explicit
`--allow-unsafe-host-execution` acknowledgement before it starts an agent.
Both Claude Code and the LiveCodeBench evaluator may run model-produced
Python. The current disposable Docker environment is the approved isolation
boundary for this run. In any non-disposable environment, mount only the
individual task workspace into the Claude Code container; keep
`judge_cases.jsonl` exclusively in the separate judging container.

After such isolation is available, the temporary host-only command is:

```bash
python testspace/test_cc/scripts/run_claude_code.py \
  --run-dir testspace/test_cc/runs/<run-name> \
  --max-concurrency 8 \
  --allow-unsafe-host-execution
```

Each task is one independent Claude Code trajectory, with no automatic retry;
the resulting metric is therefore agentic pass@1, not raw-completion pass@1.

## Persistent 32-session KV-cache stress run

`run_claude_code_stress.py` is a separate stress mode. It starts exactly the
requested number of persistent Claude Code processes (at most 32) and assigns
the frozen LiveCodeBench tasks round-robin. Each process receives multiple user
turns through Claude Code's stream-JSON input, so conversation context remains
in the same Claude session instead of restarting for every problem.

Prepare a dedicated full LiveCodeBench run directory:

```bash
RUN_DIR=testspace/test_cc/runs/lcb_kv_stress_$(date +%Y%m%d_%H%M%S)
python testspace/test_cc/scripts/prepare_livecodebench_hf_full.py \
  --limit 1055 \
  --output-dir "${RUN_DIR}"
```

Check the assignment without creating session workspaces or calling the model:

```bash
python testspace/test_cc/scripts/run_claude_code_stress.py \
  --run-dir "${RUN_DIR}" \
  --sessions 32 \
  --dry-run
```

Run the workload in the approved disposable environment. The runner sources
`pd-testspace/claude.sh` automatically and defaults to `--autocompact 128k`.
Using all 1,055 tasks gives each session 32 or 33 consecutive LiveCodeBench
turns, which is preferable to a short slice when the goal is to repeatedly
exercise long contexts.

```bash
python testspace/test_cc/scripts/run_claude_code_stress.py \
  --run-dir "${RUN_DIR}" \
  --sessions 32 \
  --turn-timeout-sec 2700 \
  --allow-unsafe-host-execution
```

For a detached run, redirect the runner's own status output separately:

```bash
nohup python testspace/test_cc/scripts/run_claude_code_stress.py \
  --run-dir "${RUN_DIR}" \
  --sessions 32 \
  --turn-timeout-sec 2700 \
  --allow-unsafe-host-execution \
  > "${RUN_DIR}/stress_runner.log" 2>&1 &
```

The stress runner refuses to reuse `stress_sessions/`; direct invocations must
use a new workspace name or explicitly remove the old stress output first. It
records each session independently while the run is in progress:

```text
<run-dir>/
  stress_results.jsonl
  stress_sessions/
    stress_metadata.json
    session_00/
      session_plan.json
      session_summary.json
      claude.input.jsonl
      claude.stdout.jsonl
      claude.stderr.log
      turn_results.jsonl
      tasks/*.md
      solutions/*.py
    ...
    session_31/
```

`claude.input.jsonl` preserves every user turn, while
`claude.stdout.jsonl` preserves Claude's stream events, including assistant
messages, tool calls, tool results, and terminal result events. Stderr is kept
separate so the stream file remains parseable JSONL. `turn_results.jsonl` is
flushed after every task, so completed-turn status survives interruption of a
long run. The `128k` autocompaction setting is an approximate Claude Code
context boundary rather than an independently measured hard token limit.
If Claude Code returns an error result, including a failed automatic
compaction, that session stops so subsequent turns do not keep growing an
unusable conversation. `stress_metadata.json` records the context variables
inherited by the runner. The model server must have enough request capacity to
complete compaction while all sessions are active.

For repeated stress runs against the repository's prepared 1,055-task dataset,
use the wrapper below. It locks the selected run directory, deletes only its
previous `stress_sessions/` and `stress_results.jsonl`, and starts 32 persistent
sessions. Every session solves the full manifest in order, beginning at row 0.
After row 1054, it returns to row 0 in the same Claude conversation and keeps
going until stopped. Send SIGINT or SIGTERM to finish after the current turn
and write the session summaries:

```bash
bash testspace/run_cc.sh
```

The default prepared directory is
`runs/lcb1055_hf_20260830_011500`. Its restored contents use ModelScope
`evalscope/livecodebench_code_generation_lite_parquet`, subset `release_v6`,
revision `c67f165ab17088d2049036ebe351257b52b8eca4`. The directory retains its
existing name for compatibility; `run_metadata.json` records the actual source,
source-file SHA-256 values, and visible-manifest SHA-256. All 315 recovered
Hugging Face prefix records matched the restored visible manifest exactly.

The wrapper defaults to 32 sessions, a 512,000-token context window, a
512,000-token auto-compact window, an 80% auto-compact trigger, and a
7,200-second per-turn timeout. Each user turn includes approximately 65,536
characters of archived, visible LiveCodeBench problem statements before the
current task instruction. This grows the conversation without requiring the
agent to solve dozens of problems first. The payload is recorded in
`claude.input.jsonl` and its configured size in `stress_metadata.json`.
`CC_CONTEXT_PAYLOAD_CHARS` changes its size; set it to `0` for the original
task-only prompts. The actual token count depends on the model tokenizer.
For a 16-session run, use `CC_SESSIONS=16 bash testspace/run_cc.sh`.
The 80% setting makes the intended trigger roughly 410k tokens, before the
512k context limit. The previous 32-session run encountered
`No enough memory to schedule single sequence` around 200k–270k prompt
tokens; increasing each session's input rate does not increase server KV-cache
capacity. Monitor the server's
`prompt_tokens` and Claude stream events to confirm compaction on a live run.
The wrapper exports `CLAUDE_CODE_MAX_CONTEXT_TOKENS`,
`CLAUDE_CODE_AUTO_COMPACT_WINDOW`, and `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` to
every Claude process. `CC_AUTOCOMPACT` accepts values such as `512k` and sets
the auto-compact window; `CC_MAX_CONTEXT_TOKENS` and `CC_AUTOCOMPACT_PCT`
override the other defaults. Existing values of the three Claude variables
take precedence. `CC_SESSIONS` and `CC_TURN_TIMEOUT_SEC` control concurrency
and per-turn timeout. Claude Code caps the effective auto-compact window at
the context window it recognizes for the model.
`pd-testspace/claude.sh` defaults to `http://127.0.0.1:7442` and
`GLM-5.3-W4A8`; `CC_BASE_URL` and `CC_MODEL` override them. These dedicated variables prevent
unrelated inherited Anthropic settings from redirecting a local stress run.

To check the repeated task plan without removing existing results, call the
runner directly:

```bash
python testspace/test_cc/scripts/run_claude_code_stress.py \
  --run-dir testspace/test_cc/runs/lcb1055_hf_20260830_011500 \
  --sessions 32 --autocompact 512k --repeat-forever --dry-run
```

For a short real run (the wrapper removes previous stress results):

```bash
CC_SESSIONS=1 bash testspace/run_cc.sh \
  testspace/test_cc/runs/lcb1055_hf_20260830_011500 --max-turns-per-session 1
```


Pass another completed prepared directory as the first argument when needed:

```bash
bash testspace/run_cc.sh testspace/test_cc/runs/<prepared-run-name>
```

The directory must contain non-empty `manifest.jsonl` and `run_metadata.json`.
An incomplete resumable download must be finished before this wrapper is used.

For a long full download, batches can overlap data transfer. Keep a distinct
result file for every non-overlapping row interval; the scorer combines all
`agent_results*.jsonl` files by default.

`run_livecodebench_full.sh <run-dir>` waits for the initial 282-task batch and
the durable full download, runs rows 282--1054 at concurrency eight, then
starts the combined EvalScope score.

## Scoring

Score only in a disposable sandbox/worker. This uses EvalScope's installed
`codegen_metrics` and its six-second-per-test limit.

```bash
python testspace/test_cc/scripts/score_livecodebench.py \
  --run-dir testspace/test_cc/runs/<run-name> \
  --allow-unsafe-local-eval
```

The output is `scores.jsonl` plus `summary.json`. The score reports how many
of the 64 tasks passed, but does not copy private expected outputs into public
agent artefacts.
