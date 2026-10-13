# LiveCodeBench 数据来源记录

当 ModelScope 的 `evalscope/livecodebench_code_generation_lite_parquet` 不可
访问时，使用 LiveCodeBench 官方仓库 README 所链接的官方 Hugging Face 数据集：

- GitHub: <https://github.com/LiveCodeBench/LiveCodeBench>
- 数据集: <https://huggingface.co/datasets/livecodebench/code_generation_lite>
- 固定 revision: `0fe84c3912ea0c4d4a78037083943e8f0c4dd505`

官方 README 说明 `release_v6`/`release_latest` 包含 1,055 题。该数据集的
`test.jsonl` 至 `test6.jsonl` 与 EvalScope LiveCodeBench loader 的
`release_latest` 文件映射一致。原始记录字段为 `question_title`、
`question_content`、`platform`、`question_id`、`contest_id`、`contest_date`、
`starter_code`、`difficulty`、`public_test_cases`、`private_test_cases` 和
`metadata`，可直接由 EvalScope 的 `transform()` 转为其判题所需的
`evaluation_sample`。

已在本环境确认该固定 revision 的 `test.jsonl` 支持 HTTP 206 byte range；前
64 个按源顺序的记录都在前 48 MiB 内。使用以下命令冻结该切片：

```bash
python testspace/test_cc/scripts/prepare_livecodebench_hf_range.py \
  --limit 64 \
  --output-dir testspace/test_cc/runs/lcb64_$(date +%Y%m%d_%H%M%S)
```

成功验证的运行包为 `runs/lcb64_hf_20260829_120500`：64 条题面记录和 64 条
EvalScope 判题记录一一对应；首题为 `1873_A`，第 64 题为 `2881`。可见
manifest 的 SHA-256 是
`7ff0848198166183a7f6a83eb1a40dfdc65df5aed0234339d93245ec36601952`。
