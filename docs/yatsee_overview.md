# YATSEE Pipeline Overview

YATSEE is a staged, local-first media processing pipeline. Each stage produces explicit files that can be inspected, compared, and reused without rerunning the entire workflow.

## Architecture

```text
compatible local media
  → normalized audio
  → VTT transcript
  → sliced TXT and optional segment JSONL
  → normalized transcript
  ├── summary
  ├── structured records
  └── optional embeddings, indexing, search, or publication
```

Source acquisition is handled outside YATSEE core. Compatible local media can come from `yatsee-fetch`, a recorder, an upload workflow, an archive, or a manual copy.

## Pipeline stages

| Stage | Command | Primary input | Primary output |
|---|---|---|---|
| Configure | `yatsee config ...` | Global and entity TOML | Resolved runtime configuration |
| Format | `yatsee audio format` | Audio or video media | Mono 16 kHz WAV or FLAC |
| Transcribe | `yatsee audio transcribe` | Normalized audio | VTT transcript |
| Slice | `yatsee transcript slice` | VTT | TXT and optional segment JSONL |
| Normalize | `yatsee transcript normalize` | Transcript TXT | Clean normalized TXT |
| Summarize | `yatsee intel summarize` | Normalized TXT | Markdown or YAML summary |
| Records | `yatsee intel records` | Normalized TXT | Structured JSON records and deterministic Markdown view |

Each processing command can accept an entity or explicit input and output paths. Entity-backed runs use the configured entity data directory.

## Entity artifact layout

A typical entity directory looks like:

```text
data/<entity>/
├── config.toml
├── downloads/
├── audio/
├── transcripts_<model>/
├── normalized/
├── summary_<model>/
├── records/
│   └── <model>/
└── yatsee_db/              # optional indexing/search data
```

Model-specific directories preserve provenance. Custom model configuration may use different summary or records directory names.

## Intelligence workflows

### Summaries

```text
classify meeting
  → select first / multi / final prompt route
  → extract chunk summaries
  → reduce again only when needed
  → final LLM synthesis
```

Summary output is designed for readability. It may combine related facts and reorganize content according to the selected profile.

### Records

```text
classify meeting
  → select one meeting-specific extraction prompt
  → extract JSON records from each chunk once
  → parse and validate each chunk
  → deterministically remove exact duplicates
  → write final records JSON
  → render an itemized Markdown view
```

Records do not use summary reductions or an LLM consolidation pass. Similar, conflicting, or uncertain records remain separate unless they are exact normalized duplicates.

The final artifact contains run metadata and records with:

```text
record_type
subject
summary
evidence
attribution         optional
attributes          optional
source_chunks
```

Raw provider responses and parsed chunk artifacts are retained beside the final JSON. Invalid chunk output is written to the `errors/` directory; later chunks continue and the attempt is marked partial.

The final JSON includes the normalized transcript hash and an extraction fingerprint covering the model, prompts, profile, chunking settings, and relevant entity context. Compatible artifacts are reused without invoking the model, while the Markdown view is regenerated directly from JSON. Partial attempts are written separately and do not replace a previous complete artifact.

## Prompt profile layout

```text
prompts/<profile>/
├── classifier.toml
├── prompts.toml
└── records_prompt.toml
```

| File | Responsibility |
|---|---|
| `classifier.toml` | Allowed meeting types and classifier prompt |
| `prompts.toml` | Summary prompts, routes, and optional density keywords |
| `records_prompt.toml` | Records extraction prompts and meeting-type routes |

Entity-local profile files under `data/<entity>/prompts/<profile>/` take precedence over project files.

## Configuration flow

```text
yatsee.toml
  + data/<entity>/config.toml
  → merged runtime configuration
  → stage-specific path, model, provider, and prompt resolution
```

Config inspection redacts secret-like fields by default. Provider security checks distinguish loopback HTTP, remote HTTPS, remote plain HTTP, and custom executables.

## Full local run

```bash
yatsee config validate -e <entity>
yatsee config resolve -e <entity>

yatsee audio format -e <entity>
yatsee audio transcribe -e <entity> --faster
yatsee transcript slice -e <entity> --force
yatsee transcript normalize -e <entity> --force

yatsee intel summarize -e <entity>
yatsee intel records -e <entity> -j civic
```

## Trust boundaries

- Media files are untrusted input to `ffmpeg`, transcription engines, and parsers.
- Transcript and model output are untrusted generated content.
- Provider URLs and CLI executables are explicit trust decisions.
- Summary and record artifacts should be checked against source evidence before official or high-stakes use.
- Deterministic mechanics should remain separate from probabilistic extraction and synthesis.

## Scope boundaries

YATSEE core owns media-to-artifact processing. The following remain adjacent unless explicitly integrated:

- media acquisition and provider-specific fetching
- hosted accounts, billing, and upload UX
- publication and public archive workflows
- production search interfaces
- downstream investigative analysis
