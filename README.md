# Claude Code 多会话压力测试

使用 LiveCodeBench 编程题驱动多个持续运行的 Claude Code 会话，测试模型服务的并发处理、长上下文、KV Cache 和自动压缩行为。模型服务需自行启动，并提供 Claude Code 能调用的 Anthropic 兼容接口。

此目录可整体复制到其他机器或移动到任意路径，不依赖原 xLLM 仓库。原始文件位于 `testspace/run_cc.sh` 和 `testspace/test_cc/`；本副本调整了路径，并补充独立的 `claude.sh` 环境配置。

## 文件结构

```text
claude_code_pressure_test/
├── README.md
├── run_cc.sh                     # 持续压测入口
├── claude.sh                     # 模型接口配置，不含真实密钥
└── test_cc/
    ├── scripts/                 # 压测、数据准备及评测脚本
    ├── tests/                   # 本地回归测试
    ├── data/                    # 原有数据探测样本
    └── runs/lcb1055_hf_20260830_011500/
        ├── manifest.jsonl       # 已冻结的 1,055 道题目
        └── run_metadata.json    # 数据来源、版本和清单摘要
```

预置题目来自 ModelScope 的 `evalscope/livecodebench_code_generation_lite_parquet`、`release_v6`，实际来源以 `run_metadata.json` 为准。目录名称沿用原名称。

分享包省略约 9 GB 的 `judge_cases.jsonl`、历史会话、日志、结果和 Python 缓存。压力测试无需判题数据，可直接使用预置清单。`test_cc/README.original.md` 保留原仓库说明，其中的路径和部分默认参数已过时，以本文件为准。

## 环境准备

- Linux，提供 Bash、`flock`、`realpath`。
- Python 3.11 或以上。压测 runner 仅使用 Python 标准库。
- 已安装并配置的 Claude Code，`claude` 在 `PATH` 中。此 runner 使用 `--autocompact` 和 stream JSON 输入输出，请通过 `claude --help` 确认所用版本支持这些参数。
- 可访问的模型服务，具备工具调用、长上下文及所需并发容量。

脚本会跳过 Claude Code 权限确认，并允许模型生成的代码在当前环境执行。请在专门的可丢弃容器中运行，只挂载测试需要的目录。

进入分享目录并检查依赖：

```bash
cd /claude_code_pressure_test   # 若移动目录，请改成实际路径
python3 --version
command -v claude flock realpath
claude --help
export PYTHON_BIN=python3
```

配置服务地址和模型名称：

```bash
export CC_BASE_URL=http://127.0.0.1:7442
export CC_MODEL=GLM-5.3-W4A8
```

`claude.sh` 将上述变量映射为 Anthropic 配置，默认使用认证值 `EMPTY`。若服务需要认证，请在本地修改 `claude.sh` 中的 `ANTHROPIC_AUTH_TOKEN`，分享前移除真实密钥。三个默认模型档位均指向 `CC_MODEL`。

## 快速开始

先检查任务计划。直接调用 runner 的 `--dry-run` 不创建会话、不调用模型、不清理历史结果，但仍检查 Claude 可执行文件和环境配置是否存在：

```bash
RUN_DIR="$PWD/test_cc/runs/lcb1055_hf_20260830_011500"
python3 test_cc/scripts/run_claude_code_stress.py \
  --run-dir "$RUN_DIR" \
  --sessions 16 --autocompact 512k --repeat-forever --dry-run
```

先运行一个会话、一个任务，确认接口和工具调用能正常工作：

```bash
CC_SESSIONS=1 bash ./run_cc.sh "$RUN_DIR" --max-turns-per-session 1
```

随后启动默认压力测试：

```bash
bash ./run_cc.sh
```

默认启动 16 个持续会话。每个会话从清单第一道题开始，依次处理全部 1,055 道题，完成后在同一会话中从头循环，直到手动停止。每轮额外附加约 16,384 字符的可见题目文本，促使上下文增长。字符数不等于 token 数。

前台运行时使用 Ctrl+C 停止；runner 收到 SIGINT 或 SIGTERM 后尝试等待当前轮结束、写入摘要，超过关闭等待时间后终止进程，默认关闭等待为 30 秒。

**每次运行 `run_cc.sh` 都会删除所选目录中旧的 `stress_sessions/` 和 `stress_results.jsonl`。** 题目清单和元数据保留；目录锁防止两个 wrapper 同时操作同一目录。需要保存历史结果时，先备份，或复制清单和元数据到 `test_cc/runs/` 下的新目录。不要通过 wrapper 做 dry-run，因为它会先执行清理。

## 调整参数

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `CC_BASE_URL` | `http://127.0.0.1:7442` | 模型接口地址 |
| `CC_MODEL` | `GLM-5.3-W4A8` | 模型名称 |
| `PYTHON_BIN` | `python` | Python 可执行文件 |
| `CC_SESSIONS` | `16` | 持续会话数，支持 1–32 |
| `CC_TURN_TIMEOUT_SEC` | `7200` | 单轮超时，单位秒 |
| `CC_AUTOCOMPACT` | `512k` | 自动压缩窗口，支持 100k–1M |
| `CC_MAX_CONTEXT_TOKENS` | 与压缩窗口相同 | 上下文 token 窗口 |
| `CC_AUTOCOMPACT_PCT` | `80` | 自动压缩触发百分比 |
| `CC_CONTEXT_PAYLOAD_CHARS` | `16384` | 每轮附加文本字符数，支持 0–200000；0 表示仅发送当前任务 |
| `CC_RUNS_ROOT` | 本目录的 `test_cc/runs` | 存放准备完成的数据目录 |
| `CC_RUN_DIR` | 预置的 1,055 题目录 | 未传第一个位置参数时使用的数据目录 |
| `CC_STRESS_RUNNER` | 本目录的压测 runner | 自定义 runner 路径 |

已有的 `CLAUDE_CODE_MAX_CONTEXT_TOKENS`、`CLAUDE_CODE_AUTO_COMPACT_WINDOW` 和 `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` 优先于对应 `CC_*` 值。wrapper 固定导出 `API_FORCE_IDLE_TIMEOUT=false` 和 `API_TIMEOUT_MS=1200000`。

示例：32 会话、128k 窗口，不添加额外上下文文本：

```bash
CC_SESSIONS=32 CC_AUTOCOMPACT=128k CC_CONTEXT_PAYLOAD_CHARS=0 \
  bash ./run_cc.sh
```

指定其他已准备目录，并限制每个会话最多运行三轮：

```bash
bash ./run_cc.sh "$PWD/test_cc/runs/my_run" --max-turns-per-session 3
```

该目录必须直接位于 `CC_RUNS_ROOT` 下，并含非空的 `manifest.jsonl` 和 `run_metadata.json`。第一个位置参数用于目录，其后参数传递给 runner。查看完整选项：

```bash
python3 test_cc/scripts/run_claude_code_stress.py --help
```

自动压缩的实际窗口取决于 Claude Code 对模型上下文的识别及服务端容量。512k 配置不代表服务一定支持 512k；需结合服务日志中的 prompt token 数和会话事件确认。

## 后台运行与输出

```bash
nohup bash ./run_cc.sh > stress_runner.log 2>&1 &
echo $! > stress_runner.pid

# 停止这个后台 runner
kill -TERM "$(cat stress_runner.pid)"
```

runner 的状态日志位于 `stress_runner.log`，详细输出位于所选数据目录：

```text
stress_results.jsonl              # 所有会话的逐轮执行状态
stress_sessions/
├── stress_metadata.json          # 本次参数和环境记录
└── session_00/                  # 每个会话分别记录
    ├── session_plan.json
    ├── session_summary.json
    ├── claude.input.jsonl        # 输入，包括附加上下文
    ├── claude.stdout.jsonl       # 模型、工具调用及终止事件
    ├── claude.stderr.log
    ├── turn_results.jsonl
    ├── tasks/
    └── solutions/
```

逐轮结果会及时写入。无限循环会持续产生日志和解答文件，需要关注磁盘空间。执行状态表示会话是否完成任务，代码正确率需要另外判题。

## 可选：准备新数据与评测

压测不需要安装 EvalScope。只有重新下载数据或评测代码时，才需要安装 `evalscope` 和 `requests`；预置元数据记录的 EvalScope 版本为 `1.12.0`。

```bash
python3 -m pip install 'evalscope==1.12.0' requests

python3 test_cc/scripts/prepare_livecodebench_hf_full.py \
  --limit 1055 --output-dir test_cc/runs/my_run
```

下载中断后使用相同参数并加 `--resume`。新数据会包含判题数据，请预留足够空间。下载脚本使用的 Hugging Face 数据源可能与预置清单来源不同，以新生成的元数据为准。

`run_claude_code.py` 与 `score_livecodebench.py` 是单题独立会话及正确率评测工具。判题需要对应的 `judge_cases.jsonl`，且不能暴露给模型工作区；本包未附带该文件。`score_livecodebench.py` 消费 `agent_results*.jsonl`，不能直接用于汇总压力测试的 `stress_results.jsonl`。

## 本地验证

```bash
bash -n run_cc.sh claude.sh
python3 -m unittest discover -s test_cc/tests -v
```

回归测试使用临时目录与模拟 Claude 进程，无需调用真实模型服务。
