# YATSEE Troubleshooting

Start with the earliest broken artifact. Prompt tuning cannot repair corrupt audio or a weak transcript.

## Quick health checks

```bash
yatsee --help
python -m yatsee --help
python -m compileall src/yatsee

yatsee config validate
yatsee config resolve -e <entity>

yatsee intel prompts validate -j civic
yatsee intel records -e <entity> -j civic --print-prompts
```

Confirm the active checkout when commands appear stale:

```bash
which yatsee
python -c "import yatsee; print(yatsee.__file__)"
pip install -e .
```

## Configuration and discovery

| Symptom | Check | Typical fix |
|---|---|---|
| Entity missing | `yatsee config entity list` | Register it, then run `config init` |
| Local config missing | `data/<entity>/config.toml` | `yatsee config init -e <entity>` |
| Unexpected paths or models | `yatsee config resolve -e <entity>` | Correct global or entity-local config |
| No files discovered | Inspect resolved input directory | Move files or pass an explicit input path |

## Audio and transcription

### Formatting finds nothing

```bash
find data/<entity>/downloads -maxdepth 2 -type f
yatsee audio format -e <entity> --dry-run
```

Check the configured media input path, supported extension, file size, and whether the source is corrupt.

### `ffmpeg` is unavailable

```bash
ffmpeg -version
ffprobe -version
```

Install both tools and restart the shell if the executable path changed.

### Transcription skips files

```bash
find data/<entity>/audio -type f
find data/<entity>/transcripts_medium -name '*.vtt'
```

Existing VTTs are skipped. Confirm the selected model directory and whether the run expects whole files or audio chunks.

### GPU memory or performance problems

- confirm the intended device is active;
- check other GPU processes;
- reduce model or context size;
- avoid concurrent GPU-heavy stages;
- retry on CPU when necessary;
- confirm backend-specific dependencies are installed.

## Transcript preparation

### Slice finds nothing

```bash
find data/<entity>/transcripts_medium -name '*.vtt'
yatsee transcript slice -e <entity> --force
```

Pass `--vtt-input` or `--model` when using a non-default transcript directory.

### Normalize finds nothing

```bash
find data/<entity>/transcripts_medium -name '*.txt'
yatsee transcript normalize -e <entity> --force
```

Use `--input-path` and `--output-dir` for nonstandard layouts.

### Transcript quality is poor

Run QA before changing prompts:

```bash
python scripts/qa_transcript_report.py --details
python scripts/qa_transcript_report.py --json qa_report.json
```

ASR loops should be rebuilt, not edited into plausible text. Preview reset actions before applying them:

```bash
python scripts/qa_reset_vtt_for_rebuild.py \
  --entity <entity> \
  --qa-report qa_report.json
```

Also review audio quality, language, model choice, people aliases, local entities, and proven replacement rules.

## Provider failures

### Provider unavailable or request fails

Check:

- `llm_provider` and `llm_provider_url`;
- whether the server or CLI is running;
- whether the model is installed or accessible;
- authentication for hosted providers;
- provider hardening settings;
- provider logs for timeout or context failures.

### Target blocked by security policy

| Target | Required settings |
|---|---|
| Loopback HTTP | `llm_allow_loopback_http=true` |
| Remote HTTPS | `llm_allow_remote=true` |
| Remote HTTP | `llm_allow_remote=true`, `llm_allow_insecure_http=true` |
| Custom executable | `llm_allow_custom_executable=true` |

Do not enable remote or insecure access without confirming the target.

### Hosted authentication failure

Prefer `llm_api_key_env` or `YATSEE_LLM_API_KEY`. Confirm the provider, key, model access, and base URL. HTTP 401 and 403 errors normally occur before generation.

## Summary problems

### Summary command is slow

Check model size, provider logs, chunk size, context size, device use, and whether additional reduction passes are being triggered.

### Summary quality is weak

Review in this order:

1. source audio;
2. VTT transcript;
3. sliced TXT;
4. normalized TXT;
5. entity names and replacements;
6. meeting classification;
7. selected prompt route;
8. model behavior.

Inspect prompts and chunk output:

```bash
yatsee intel summarize -e <entity> --print-prompts
yatsee intel summarize -e <entity> --enable-chunk-writer
```

## Records problems

### `records_prompt.toml` is missing

The selected profile does not support records. Use `civic`, add a valid sidecar, or choose another profile intentionally.

```bash
yatsee intel records -e <entity> -j civic --print-prompts
```

The built-in `research` profile currently has no records sidecar.

### Records output is reused

A complete `.records.json` artifact is reused only when its normalized transcript hash and extraction fingerprint match the current run. The fingerprint covers extraction-relevant inputs such as the model, prompts, profile, meeting-type controls, chunking settings, and entity context.

A stale, malformed, partial, or incompatible artifact triggers a full transcript extraction automatically. Use `--force` only to bypass an otherwise compatible cache entry.

### Classification selected the wrong route

Classification identifies the formal meeting body, but filenames and transcript openings may be ambiguous. Override the route:

```bash
yatsee intel records \
  -e <entity> \
  --meeting-type committee_of_the_whole
```

Use `--disable-auto-classification` to select the `general` route.

### Chunk response contains invalid JSONL

Inspect:

```text
records/<model>/<transcript>/raw/
records/<model>/<transcript>/errors/
```

Common causes:

- model added commentary or malformed surrounding fences;
- output was truncated;
- required fields are missing;
- `record_type` is unsupported;
- provider returned an empty response.

Reduce chunk size, verify the selected prompt, or test a more reliable structured-output model. The raw response is preserved. Valid JSONL lines survive malformed or truncated sibling lines; diagnostics are recorded under `errors/`, later chunks continue, and the transcript attempt is written as partial.

### Records are duplicated

Only exact normalized duplicates are merged. Similar records remain separate by design. This avoids unsafe fuzzy merging and conflict resolution.

### Records are sparse

Check the selected route, chunk output, transcript quality, and whether the segment actually contains explicit records. The prompt intentionally prefers omission over unsupported inference.

### Partial records run does not resume

Current records runs are transcript-level, not chunk-resumable. Compatible complete artifacts are reused when the normalized transcript hash and extraction fingerprint match. A stale or missing artifact triggers a full transcript extraction. Partial attempts are written separately and do not replace a previous complete artifact.

## Pricing output is missing

Confirm `show_pricing`, `pricing_provider`, `pricing_model`, and table coverage. Pricing is a hosted-provider reference estimate, not local compute cost.

## Isolate one file

Use explicit file paths and output directories to separate content failures from batch behavior. For records:

```bash
yatsee intel records \
  --txt-input ./normalized/meeting.txt \
  --output-dir ./records_test \
  --model gemma4:e4b \
  -j civic
```

## Reporting an issue

Include:

- operating system, Python version, CPU, GPU, and RAM;
- exact command;
- entity or explicit paths;
- provider, model, and target;
- full error text;
- whether one file or the whole batch fails;
- relevant resolved config with secrets redacted;
- prompt route and raw chunk artifact when the problem is model output.
