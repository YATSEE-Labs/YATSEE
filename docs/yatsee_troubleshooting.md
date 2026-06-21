# YATSEE Troubleshooting

YATSEE is local-first, so performance and failure modes depend on the local machine, installed tools, selected transcription backend, selected LLM provider, provider security policy, model settings, and available data artifacts.

Some failures come from the core pipeline. Others come from the chosen transcription backend, LLM provider, local runtime, hosted API, CLI integration, or filesystem state.

---

# Quick Health Checks

Check both entrypoints:

```bash
yatsee --help
python -m yatsee --help
```

Check command groups:

```bash
yatsee config --help
yatsee audio --help
yatsee transcript --help
yatsee intel --help
yatsee intel signals --help
```

Compile package files:

```bash
python -m compileall src/yatsee
```

Validate config:

```bash
yatsee config validate
```

Resolve an entity:

```bash
yatsee config resolve --entity <entity>
```

---

# Common Issues

## CLI command is missing

If a command does not appear in help output:

```bash
yatsee --help
yatsee intel --help
```

Confirm the installed package is the editable checkout you are modifying:

```bash
which yatsee
python -c "import yatsee; print(yatsee.__file__)"
```

Reinstall editable mode if needed:

```bash
pip install -e .
```

If `python -m yatsee` works but `yatsee` does not, the console script may be stale. Reinstall the package.

## Entity does not appear

List registered entities:

```bash
yatsee config entity list
```

If missing, add it:

```bash
yatsee config entity add \
  --display-name "Example Entity" \
  --entity example_entity \
  --base "country.US.state.EX."
```

Scaffold local config:

```bash
yatsee config init --entity example_entity
```

## Local config missing

Expected location:

```text
data/<entity>/config.toml
```

Create it:

```bash
yatsee config init --entity <entity>
```

## Runtime config does not match expectation

Use resolve:

```bash
yatsee config resolve --entity <entity>
```

Check:

- `data_path`
- `[media].input_dir`
- `[media].audio_dir`
- selected models
- selected LLM provider
- output directories

---

# Stage Troubleshooting

## Audio formatting finds nothing

Check raw media:

```bash
find data/<entity>/downloads -maxdepth 2 -type f
```

Run dry-run:

```bash
yatsee audio format -e <entity> --dry-run
```

Provide a direct input path:

```bash
yatsee audio format \
  -e <entity> \
  --input-dir /path/to/raw/media \
  --dry-run
```

Common causes:

- media is in the wrong entity directory
- `[media].input_dir` points somewhere unexpected
- the file extension is unsupported
- the source file is empty or corrupt

## `ffmpeg` is missing or not on `PATH`

```bash
ffmpeg -version
ffprobe -version
```

Install `ffmpeg` and restart the shell if necessary.

## Transcription skips files

Check formatted audio:

```bash
find data/<entity>/audio -type f
```

Check target output directory:

```bash
find data/<entity>/transcripts_medium -name '*.vtt'
```

Common causes:

- audio stage did not produce files
- output transcripts already exist
- wrong model/output directory is being inspected
- chunked vs non-chunked input mismatch

## GPU memory exhaustion

If transcription or local summarization fails with out-of-memory errors:

- reduce the transcription model size
- reduce summarization chunk size or context size
- avoid running multiple GPU-heavy stages at the same time
- retry on CPU if needed
- check whether another process is using the GPU

## Unexpectedly slow transcription

Check:

- intended device is actually being used
- GPU is visible to the environment
- another process is not consuming GPU memory
- faster-whisper / Whisper dependencies are installed correctly
- chunking settings are sensible for the hardware

## Slice stage finds nothing

Check VTT files:

```bash
find data/<entity>/transcripts_medium -name '*.vtt'
```

Run:

```bash
yatsee transcript slice -e <entity> --force
```

If using a non-default transcription model directory, pass the model or input path explicitly.

## Normalize stage finds nothing

Check sliced TXT files:

```bash
find data/<entity>/transcripts_medium -name '*.txt'
```

Run:

```bash
yatsee transcript normalize -e <entity> --force
```

If using another transcript directory:

```bash
yatsee transcript normalize \
  --input-path /path/to/transcript_txt_files \
  --output-dir /path/to/normalized_out
```

---

# Intelligence Stage Troubleshooting

## LLM provider is unavailable

If `yatsee intel run` fails with a connection error, timeout, or empty response:

- confirm `llm_provider`
- confirm `llm_provider_url`
- confirm the requested model exists for that provider
- confirm the provider is reachable
- confirm provider hardening settings are not blocking it

Examples:

- Ollama not running
- llama.cpp server not running
- `codex_cli` not installed
- hosted API base URL is wrong

## Hosted API authentication failure

If OpenAI or Anthropic requests fail immediately:

- confirm `llm_api_key`
- confirm the key is valid
- confirm the selected provider matches the supplied key
- confirm model access

Typical symptoms:

- HTTP 401
- HTTP 403
- model access denied
- request rejected before generation

## Provider target blocked by hardening settings

If a provider target looks valid but YATSEE rejects it before execution, review:

```text
llm_allow_remote
llm_allow_insecure_http
llm_allow_custom_executable
```

Typical cases:

- remote Ollama or llama.cpp target blocked because `llm_allow_remote = false`
- hosted provider blocked because URL uses `http://`
- custom `codex_cli` executable blocked because `llm_allow_custom_executable = false`

## Local model runtime is unavailable

Check:

- runtime is running
- model is installed locally
- provider URL is correct
- host is reachable
- remote target is allowed by security settings

## `codex_cli` execution failure

Check:

- `codex` executable is installed
- executable is available on `PATH`
- CLI is authenticated
- CLI supports expected invocation style
- custom executable targets are allowed if using a non-default command

## Unexpectedly slow summarization

Check:

- selected provider
- selected model
- context size
- chunk size
- GPU/CPU behavior
- local provider logs
- whether another local model process is running

## Pricing output is missing

Check:

- `show_pricing = true` or `--show-pricing`
- selected `pricing_provider`
- selected `pricing_model`
- pricing table coverage

Remember: pricing is reference pricing, not actual local compute cost.

---

# Meeting Signals Troubleshooting

## Signals command missing

Check:

```bash
yatsee intel --help
yatsee intel signals --help
```

If missing, confirm the CLI split files are installed from the active checkout:

```bash
pip install -e .
```

## Signals find nothing

Check normalized transcripts:

```bash
find data/<entity>/normalized -name '*.txt'
```

Run with force:

```bash
yatsee intel signals -e <entity> --force
```

Use a direct input path:

```bash
yatsee intel signals \
  --input-path ./normalized \
  --output-dir ./meeting_signals
```

## Signals are sparse

Signals are deterministic evidence candidates. Sparse output usually means:

- transcript text is weak
- normalization removed useful cues
- the meeting did not contain many detectable patterns
- the signal rules need expansion
- the source contains informal discussion rather than formal actions

Signals are not a replacement for summaries or minutes.

---

# Transcript Quality Issues

If downstream summaries are weak because transcript quality is poor:

- confirm audio quality
- confirm transcription model choice
- confirm language setting
- review chunking behavior
- add `[replacements]` for recurring ASR failures
- confirm entity-specific names, titles, and people lists are current

Check normalized text before tuning prompts:

```bash
sed -n '1,220p' data/<entity>/normalized/<file>.txt
```

---

# Useful Isolation Steps

## Test one stage at a time

```bash
yatsee audio format -e <entity>
yatsee audio transcribe -e <entity>
yatsee transcript slice -e <entity>
yatsee transcript normalize -e <entity>
yatsee intel run -e <entity>
yatsee intel signals -e <entity>
```

## Test one file instead of a directory

Point a stage at a single known file to separate content issues from batch-processing issues.

## Print prompts without running inference

```bash
yatsee intel run -e <entity> --print-prompts
```

## Write chunk outputs for inspection

```bash
yatsee intel run -e <entity> --enable-chunk-writer
```

This helps determine whether the issue begins at:

- chunking
- prompt routing
- intermediate summarization
- final synthesis

## Override provider settings for one run

```bash
yatsee intel run \
  -e <entity> \
  --llm-provider ollama \
  --llm-provider-url http://localhost:11434

yatsee intel run \
  -e <entity> \
  --llm-provider llamacpp \
  --llm-provider-url http://localhost:8080
```

Provider selection can be overridden from the CLI, but provider hardening remains config-driven.

---

# Reporting Issues

When reporting a failure, include:

- operating system
- CPU, GPU, and RAM details
- exact command
- active provider and model
- full terminal error output
- whether the issue is reproducible
- whether it affects one file or all files
- whether it is provider-specific

For provider-hardening issues, include:

```text
llm_provider
llm_provider_url
llm_allow_remote
llm_allow_insecure_http
llm_allow_custom_executable
```

For pricing issues, include:

```text
show_pricing
pricing_provider
pricing_model
reported token counts
reported estimated cost
```