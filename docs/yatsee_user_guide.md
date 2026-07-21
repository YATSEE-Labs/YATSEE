# YATSEE User Guide

This guide covers the shortest reliable path from local media to transcripts, summaries, and records. Use command `--help` output for the complete option reference.

## 1. Preflight

```bash
ffmpeg -version
yatsee --help
yatsee config --help
yatsee audio --help
yatsee transcript --help
yatsee intel --help
```

Create a local config if needed:

```bash
cp yatsee.conf yatsee.toml
```

## 2. Create an entity

An entity represents a recurring body such as a council, board, committee, or agency.

```bash
yatsee config entity add \
  --display-name "Example County Board" \
  --entity example_county_board \
  --base "country.US.state.EX."

yatsee config init -e example_county_board
```

Edit:

```text
data/example_county_board/config.toml
```

At minimum, confirm the entity identity, `data_path`, and media directories. Add people, organizations, local terms, and recurring replacements as they become useful.

Validate before processing:

```bash
yatsee config validate -e example_county_board
yatsee config resolve -e example_county_board
```

## 3. Supply media

Place compatible media in the configured input directory, normally:

```text
data/example_county_board/downloads/
```

YATSEE does not care whether the files came from a fetcher, recorder, upload workflow, archive, or manual copy.

## 4. Format audio

```bash
yatsee audio format -e example_county_board --dry-run
yatsee audio format -e example_county_board
```

Default output is mono 16 kHz FLAC under the entity audio directory. Optional chunking is available for long recordings:

```bash
yatsee audio format \
  -e example_county_board \
  --create-chunks \
  --chunk-duration 600 \
  --chunk-overlap 2
```

Use `--force` only when existing formatted outputs should be replaced.

## 5. Transcribe

```bash
yatsee audio transcribe -e example_county_board --faster
```

Common overrides:

```bash
yatsee audio transcribe -e example_county_board --model medium --device cuda
yatsee audio transcribe -e example_county_board --faster --get-chunks
```

The output is VTT under `transcripts_<model>/`. Existing transcripts are normally skipped.

## 6. Optional transcript QA

For large batches, inspect VTTs before generating downstream artifacts:

```bash
python scripts/qa_transcript_report.py --details
python scripts/qa_transcript_report.py --json qa_report.json
```

Reset only reviewed failures, starting with a dry run:

```bash
python scripts/qa_reset_vtt_for_rebuild.py \
  --entity example_county_board \
  --qa-report qa_report.json
```

Add `--apply`, then rerun transcription, only after confirming the matched files and tracker updates.

## 7. Slice VTT into transcript artifacts

```bash
yatsee transcript slice -e example_county_board --force
```

This writes plain TXT beside the VTT. Optional segment JSONL and embeddings can be generated with:

```bash
yatsee transcript slice \
  -e example_county_board \
  --gen-embed \
  --device cuda \
  --force
```

## 8. Normalize transcript text

```bash
yatsee transcript normalize -e example_county_board --force
```

Normalized text is written under:

```text
data/example_county_board/normalized/
```

Useful overrides:

```bash
yatsee transcript normalize -e example_county_board --preserve-paragraphs --force
yatsee transcript normalize -e example_county_board --no-spacy --force
```

Inspect normalized text before tuning prompts or models.

## 9. Generate summaries

```bash
yatsee intel summarize -e example_county_board -j civic
```

Summary processing classifies the meeting, selects a `first / multi / final` route, and uses additional reduction passes only when needed.

Useful controls:

```bash
yatsee intel summarize -e example_county_board --print-prompts
yatsee intel summarize -e example_county_board --enable-chunk-writer
yatsee intel summarize -e example_county_board --disable-auto-classification
yatsee intel summarize -e example_county_board --chunk-style sentence --max-tokens 2500
```

`yatsee intel run` remains a compatibility alias.

## 10. Extract structured records

```bash
yatsee intel records \
  -e example_county_board \
  -j civic \
  -m gemma4:e4b \
  --max-tokens 1500
```

Records processing:

1. classifies the formal meeting type unless overridden;
2. selects one meeting-specific extraction prompt;
3. runs that prompt once per transcript chunk using compact JSONL;
4. preserves valid record lines while isolating malformed lines or records;
5. deterministically removes only exact duplicates;
6. writes a self-describing JSON artifact containing the source hash and extraction fingerprint;
7. renders the itemized Markdown view directly from the JSON artifact.

Useful controls:

```bash
# Inspect records routing and prompt text without inference.
yatsee intel records -e example_county_board -j civic --print-prompts

# Bypass the classifier and select a route explicitly.
yatsee intel records \
  -e example_county_board \
  -j civic \
  --meeting-type finance_committee

# Use the general route without model classification.
yatsee intel records \
  -e example_county_board \
  -j civic \
  --disable-auto-classification
```

Default output:

```text
data/example_county_board/records/<model>/<transcript>/
├── <transcript>.records.json
├── <transcript>.records.md
├── raw/
├── chunks/
└── errors/
```

Behavior to know:

- Existing complete records artifacts are reused only when their normalized transcript hash and extraction fingerprint still match; stale artifacts are rebuilt automatically.
- The Markdown view is regenerated from the final JSON without invoking the model.
- Records defaults to density-aware chunking; the token budget comes from CLI or model configuration.
- `--force` bypasses reuse and reruns extraction.
- Records runs are not resumable at the chunk level yet.
- Invalid records are isolated and reported while valid sibling records continue. A malformed JSONL line marks its chunk partial; an empty or unusable response marks the chunk failed. Later chunks continue, and the attempt is written as `.records.partial.json` and `.records.partial.md` without replacing a prior complete artifact.
- The civic profile supports records. The research profile currently has no `records_prompt.toml` and will fail clearly.

## 11. Inspect outputs

Typical layout:

```text
data/example_county_board/
├── downloads/
├── audio/
├── transcripts_medium/
├── normalized/
├── summary_<model>/
└── records/<model>/
```

Quick counts:

```bash
find data/example_county_board/transcripts_medium -name '*.vtt' | wc -l
find data/example_county_board/normalized -name '*.txt' | wc -l
find data/example_county_board -name '*.summary.md' -o -name '*.summary.yaml'
find data/example_county_board/records -name '*.records.json'
find data/example_county_board/records -name '*.records.md'
```

## Direct-path runs

An entity is optional for transcript intelligence stages, but explicit input and output paths are then required:

```bash
yatsee intel records \
  --txt-input ./normalized \
  --output-dir ./records_out \
  --model gemma4:e4b \
  -j civic
```

The same principle applies to the summary stage using its input option name.

## Recommended operating order

```text
validate configuration
→ inspect source media
→ format audio
→ transcribe
→ review VTT quality when needed
→ slice
→ normalize
→ inspect normalized text
→ generate summaries and records independently
→ review derived artifacts against source evidence
```

Do not compensate for weak audio or transcript quality by immediately changing prompts. Fix the earliest broken artifact first.
