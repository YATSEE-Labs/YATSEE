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
| Records | `yatsee intel records` | Normalized TXT, optional segment JSONL | Structured JSON records, optional timestamps, and deterministic Markdown view |
| Events | `yatsee intel events` | Complete records JSON | Provenance-preserving meeting events, Markdown, and entity JSON indexes |

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
├── intel_<model>/
│   ├── summaries/
│   ├── records/
│   ├── events/
│   └── audit/
└── yatsee_db/              # optional indexing/search data
```

Each `intel_<model>` workspace preserves one complete model lineage. An optional `intel_append_dir` model setting may override the workspace name.

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
  → optionally align final evidence to timestamped segments
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
timestamp           optional numeric seconds
video_id            optional case-preserved source video ID
```

Raw provider responses and parsed chunk artifacts are retained beside the final JSON. Invalid chunk output is written to the `errors/` directory; later chunks continue and the attempt is marked partial.

The final JSON includes the normalized transcript hash plus human-readable extraction provenance such as model, prompt route, profile, and chunking settings. The normalized transcript hash is the only automatic records-cache invalidator; prompt, model, profile, schema/version, status, and chunk metadata changes require an explicit `--force`. Reused artifacts do not invoke the model, and the Markdown view is regenerated directly from JSON. Partial attempts are written separately and do not replace a previous final artifact.

Timestamp enrichment runs when `--align-timestamps` is supplied. The command first performs the normal source-hash records workflow: matching records artifacts are reused, while missing artifacts or artifacts whose normalized source hash changed are extracted. Every available successful records artifact is then aligned. `--force --align-timestamps` rebuilds every selected records artifact before alignment; partial rebuilds preserve the previous final artifact. `--timestamp-only` is the separate forced-realignment mode that returns before extraction prompt loading, provider resolution, chunk generation, and LLM session creation. It accepts any existing records JSON object with a usable `records` list and non-empty evidence, ignores compatible alignment reuse, recalculates timestamps, and skips missing or unusable artifacts. Hybrid mode follows the indexing path: record evidence and context embeddings are compared in one vectorized operation against stored segment embeddings, the top source neighborhoods are expanded into adjacent windows, and conservative lexical and ambiguity rules run only on that shortlist. Exact repeated evidence occurrences are retained so top-K filtering cannot hide a competing procedural phrase. Accepted matches add `timestamp` and optional `video_id`; the Markdown renderer turns that pair into a timestamped source-video link. Ambiguous and unsupported evidence remains unmodified. Detailed candidate scores are written to `<transcript>.records.alignment.json`. Persisted records and alignment input paths are serialized relative to the directory containing `yatsee.toml`. Alignment has an independent fingerprint.

### Events

```text
read complete records JSON
  → preserve one-based source-record provenance
  → collapse only strong adjacent duplicates
  → group local motion / second / request / vote sequences
  → preserve uncertain records as separate events
  → write meeting events JSON and Markdown
  → write entity events.jsonl and meetings.json indexes
```

Events are a deterministic projection over records. They do not invoke an LLM, alter source records, or claim semantic reconciliation when evidence is ambiguous. Every event retains its contributing source records, chunks, evidence, timestamps, and optional video IDs. Derived review flags identify unattached motion requests, unattached seconds, conflicting outcomes, or mixed source-video IDs.

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
yatsee intel events -e <entity>
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
