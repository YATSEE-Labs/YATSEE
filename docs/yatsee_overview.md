# YATSEE Pipeline Overview

YATSEE is a staged, local-first media processing pipeline. Each stage produces explicit files that can be inspected, compared, and reused without rerunning the entire workflow.

## Architecture

```text
compatible local media
  → normalized audio
  → VTT transcript
  → optional transcript audit and reviewed remediation
  → sliced TXT and optional segment JSONL
  → normalized transcript
  → structured records
  → optional deterministic Records audit
  → deterministic events
  → reader-oriented summary
  → optional indexing, search, site generation, or publication
```

Source acquisition is handled outside YATSEE core. Compatible local media can come from `yatsee-fetch`, a recorder, an upload workflow, an archive, or a manual copy.

## Pipeline stages

| Stage | Command | Primary input | Primary output |
|---|---|---|---|
| Configure | `yatsee config ...` | Global and entity TOML | Resolved runtime configuration |
| Format | `yatsee audio format` | Audio or video media | Mono 16 kHz WAV or FLAC |
| Transcribe | `yatsee audio transcribe` | Normalized audio | VTT transcript |
| Audit transcript | `yatsee audit transcript` | VTT transcript | JSON and Markdown QA reports |
| Slice | `yatsee transcript slice` | VTT | TXT and optional segment JSONL |
| Normalize | `yatsee transcript normalize` | Transcript TXT | Clean normalized TXT |
| Records | `yatsee intel records` | Normalized TXT, optional segment JSONL | Structured JSON records, optional timestamps, and deterministic Markdown view |
| Audit Records | `yatsee audit records` | Existing Records corpus | CSV, JSON, and Markdown QA reports |
| Events | `yatsee intel events` | Complete records JSON | Provenance-preserving meeting events, Markdown, and entity JSON indexes |
| Summarize | `yatsee intel summarize` | Normalized TXT or explicit source TXT | Records, Events, and Markdown or YAML summary |

Entity-backed runs use the configured entity data directory. Direct-path support varies by command; use each command's `--help` output for the required input and output arguments.

## Entity artifact layout

A typical entity directory looks like:

```text
data/<entity>/
├── config.toml
├── downloads/
├── audio/
├── transcripts_<model>/
├── normalized/
├── audit/
│   ├── transcript/<transcription-model>/
│   └── records/<records-model>/
├── intel_<model>/
│   ├── summaries/
│   ├── records/
│   └── events/
└── yatsee_db/              # optional indexing/search data
```

Each `intel_<model>` workspace preserves one complete model lineage. An optional `intel_append_dir` model setting may override the workspace name.

## Intelligence workflows

### Summaries

```text
run the canonical Records stage and reuse matching artifacts
  → refresh deterministic Events from the resulting Records
  → use the preserved meeting type for first / multi / final prompt routing
  → serialize compact reader-oriented structured facts
  → consolidate only when the facts exceed the configured payload
  → final LLM synthesis
```

The summary command does not implement its own extraction path. It delegates normalized source handling and classification to the canonical Records stage, then consumes the resulting Records and deterministic Events. Records remain the factual source layer, Events provide organization, and the summary profile controls consolidation and final presentation.

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
| `classifier.toml` | Allowed meeting types and classifier prompt used during Records extraction |
| `prompts.toml` | Records/Events summary prompts, routes, and optional density keywords |
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
yatsee audit transcript -e <entity> --model medium
yatsee transcript slice -e <entity> --force
yatsee transcript normalize -e <entity> --force

yatsee intel summarize -e <entity>
yatsee audit records -e <entity> --model gemma4:e4b --profile balanced
```

The standalone `yatsee intel records` and `yatsee intel events` commands remain available when only those artifacts are needed. Running both before `yatsee intel summarize` is not required because Summary already ensures Records and refreshes Events.

## Trust boundaries

- Media files are untrusted input to `ffmpeg`, transcription engines, and parsers.
- Transcript and model output are untrusted generated content.
- Provider URLs and CLI executables are explicit trust decisions.
- Summary and record artifacts should be checked against source evidence before official or high-stakes use.
- Audit findings are review signals, not proof that an artifact or source is wrong.
- Deterministic mechanics should remain separate from probabilistic extraction and synthesis.

## Scope boundaries

YATSEE core owns media-to-artifact processing. The following remain adjacent unless explicitly integrated:

- media acquisition and provider-specific fetching
- hosted accounts, billing, and upload UX
- hosted site deployment and public archive delivery
- production search interfaces
- downstream investigative analysis
