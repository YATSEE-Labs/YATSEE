# YATSEE Troubleshooting

Start with the earliest broken artifact. Prompt tuning cannot repair corrupt audio or a weak transcript.

## Quick health checks

```bash
yatsee --help
python -m yatsee --help
python -m compileall src/yatsee

yatsee audit --help

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

Run the integrated transcript audit before changing prompts:

```bash
yatsee audit transcript \
  -e <entity> \
  --model medium
```

Reports are written to:

```text
data/<entity>/audit/transcript/<model>/report.json
data/<entity>/audit/transcript/<model>/report.md
```

ASR loops should be rebuilt, not edited into plausible text. Preview the reset
selected from the canonical report before applying it:

```bash
yatsee audit transcript reset \
  -e <entity> \
  --model medium
```

After confirming the matched VTT and tracker entries, rerun with `--apply` and
then transcribe the selected source again. Timing review candidates are reported
for inspection but do not independently cause `block` or `rerun` status.

To review cues that transcription suppressed, first create a CSV:

```bash
yatsee audit transcript restore \
  -e <entity> \
  --model medium \
  --log-file transcribe.log \
  --write-review-csv suppressed-review.csv
```

Mark only verified rows as `action=restore`, preview the insertion, and add
`--apply` only after reviewing the matched VTT:

```bash
yatsee audit transcript restore \
  -e <entity> \
  --model medium \
  --log-file transcribe.log \
  --review-csv suppressed-review.csv
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

Check model size, provider logs, structured-input chunk size, context size, device use, Records/Event density, and whether additional reduction passes are being triggered.

### Summary quality is weak

Review in this order:

1. source audio and VTT transcript;
2. normalized TXT;
3. final Records JSON;
4. deterministic Events JSON;
5. the meeting type preserved in Events;
6. the selected summary prompt route;
7. intermediate summary chunks when enabled;
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

### Records output is reused

An existing `.records.json` artifact is reused whenever its stored normalized transcript hash matches the current normalized transcript. Prompt, classifier, model, profile, schema/version, status, and chunk metadata do not invalidate reuse. Use `--force` when you intentionally want those changes to regenerate records.

A normalized transcript hash mismatch or missing records artifact triggers extraction automatically. An existing unreadable artifact, an artifact without a records list, or an artifact without `source_sha256` fails clearly and requires `--force`; it does not silently launch model work.

### Timestamp alignment is skipped

`--align-timestamps` requires a matching sliced artifact named:

```text
transcripts_<transcription_model>/<transcript>.segments.jsonl
```

Create it with:

```bash
yatsee transcript slice -e <entity> --gen-embed --force
```

Then rerun records with `--align-timestamps`. This performs the normal records workflow first: source-matching artifacts are reused, while missing records or records whose normalized source changed are extracted, and every successful artifact is then aligned. Use `--force --align-timestamps` to rebuild every selected records artifact before alignment. Use `--timestamp-only` when you need a strict no-LLM forced realignment pass over usable existing records; compatible alignment caches are ignored, while missing or unusable records and missing segment files are reported and skipped. Evidence absent from the current segments remains unmatched.

Hybrid alignment requires a valid `embedding` vector on every segment row. If the report says embeddings are missing or their dimensions do not match, regenerate the sliced JSONL with the same embedding model resolved for records:

```bash
yatsee transcript slice -e <entity> --gen-embed --force
```

The matcher reuses those stored vectors to shortlist candidate neighborhoods. It does not re-embed the full segment corpus or perform an exhaustive lexical comparison against every window.

When alignment runs, inspect:

```text
intel_<model>/records/<transcript>/<transcript>.records.alignment.json
```

Unmatched or ambiguous evidence is intentionally left without `timestamp` or `video_id`. Do not lower matcher thresholds only to increase the match count.

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
intel_<model>/records/<transcript>/raw/
intel_<model>/records/<transcript>/errors/
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

Current records runs are transcript-level, not chunk-resumable. Existing artifacts are reused when the normalized transcript hash matches, regardless of status or extraction provenance. A changed normalized transcript or missing artifact triggers a full transcript extraction. Partial attempts are written separately and do not replace a previous final artifact.

### Audit the Records corpus

Run the deterministic Records audit without invoking an LLM:

```bash
yatsee audit records \
  -e <entity> \
  --model gemma4:e4b \
  --profile balanced
```

Reports are written to:

```text
data/<entity>/audit/records/<model>/report.csv
data/<entity>/audit/records/<model>/report.json
data/<entity>/audit/records/<model>/report.md
```

Use `--profile strict` for a more sensitive review. The default audit and
`--rebuild` are deterministic and do not invoke an LLM. `--rebuild-auto` is the
explicit exception: it invokes Records generation for structurally justified
candidates and then reruns the audit. Audit findings are review signals, not proof
that the source or generated artifact is wrong.

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
