# YATSEE User Guide

YATSEE is a local-first audio processing pipeline designed to turn raw meeting media into clean, searchable, and auditable artifacts.

It can:

- normalize raw media into transcription-ready audio
- transcribe speech
- slice transcript cues into text and optional JSONL segments
- normalize transcript text
- generate LLM-based summaries through configurable providers
- generate deterministic meeting signal artifacts
- support optional downstream indexing and search workflows

The pipeline is intentionally staged. Each stage produces useful artifacts and can be inspected independently.

---

# Table of Contents

- [Before Stage 1: Create or Select an Entity](#before-stage-1-create-or-select-an-entity)
- [Stage 1: Raw Media Intake](#stage-1-raw-media-intake)
- [Stage 2: Audio Formatting and Chunking](#stage-2-audio-formatting-and-chunking)
- [Stage 3: Transcription](#stage-3-transcription)
- [Stage 3b: Experimental Transcript QA](#stage-3b-experimental-transcript-qa)
- [Stage 4a: Slicing Transcripts into Segments](#stage-4a-slicing-transcripts-into-segments)
- [Stage 4b: Transcript Normalization and Structure](#stage-4b-transcript-normalization-and-structure)
- [Stage 5a: Intelligence and Summarization](#stage-5a-intelligence-and-summarization)
- [Stage 5b: Meeting Signals](#stage-5b-meeting-signals)
- [Typical Full Pipeline](#typical-full-pipeline)
- [Output Directory Overview](#output-directory-overview)
- [Troubleshooting Workflow](#troubleshooting-workflow)

---

# Before Stage 1: Create or Select an Entity

Most YATSEE runs are entity-driven.

An entity represents a recurring source body such as:

- a city council
- a county board
- a school board
- a committee
- a public meeting channel
- another recurring source group

Before processing media, make sure the entity exists in the global registry and has a local `config.toml`.

## List existing entities

```bash
yatsee config entity list
```

## Add a new entity

```bash
yatsee config entity add \
  --display-name "Example County Board" \
  --entity example_county_board \
  --base "country.US.state.EX."
```

## Scaffold local config

```bash
yatsee config init --entity example_county_board
```

This creates:

```text
data/example_county_board/config.toml
```

## Edit local config

```bash
nano data/example_county_board/config.toml
```

At minimum, review:

```toml
[settings]
entity_type = "county_board"
entity_level = "county"
location = "Example County, Example State"

[media]
input_dir = "downloads"
audio_dir = "audio"
```

`input_dir` is provider-neutral. External acquisition tools, upload jobs, manual copies, or recording workflows can place compatible files there.

## Validate the entity

```bash
yatsee config validate --entity example_county_board
```

## Inspect resolved config

```bash
yatsee config resolve --entity example_county_board
```

Use this before long runs. It shows the merged runtime configuration that YATSEE will actually use.

---

# Stage 1: Raw Media Intake

## Purpose

Place compatible raw media into the pipeline so YATSEE can normalize it into transcription-ready audio.

YATSEE core does not need to know where the media came from. It only needs a file or directory containing compatible media.

## Input

Raw media from:

```text
data/<entity>/downloads/
```

or a direct input path.

## Output

No derived output is produced at this stage. This is the handoff point before audio formatting.

## Usage examples

```bash
mkdir -p data/example_entity/downloads
cp /path/to/media/* data/example_entity/downloads/

yatsee audio format -e example_entity

yatsee audio format \
  -e example_entity \
  --input-dir /path/to/raw/media
```

---

# Stage 2: Audio Formatting and Chunking

## Purpose

Convert raw media into transcription-ready audio.

## Input

Raw media files from:

```text
data/<entity>/downloads/
```

or a direct input path.

## Output

Formatted audio under:

```text
data/<entity>/audio/
```

Optional chunks under:

```text
data/<entity>/audio/chunks/<base_name>/
```

Typical target:

```text
mono
16 kHz
flac or wav
```

## CLI options

```text
-e / --entity
-c / --config
-i / --input-dir
-o / --output-dir
--format
--create-chunks
--chunk-duration
--chunk-overlap
--dry-run
--force
```

## Usage examples

```bash
yatsee audio format -e example_entity

yatsee audio format \
  -e example_entity \
  --format flac

yatsee audio format \
  -e example_entity \
  --dry-run

yatsee audio format \
  -e example_entity \
  --create-chunks \
  --chunk-duration 600 \
  --chunk-overlap 2
```

---

# Stage 3: Transcription

## Purpose

Convert formatted audio into timestamped transcripts.

## Input

Audio files from:

```text
data/<entity>/audio/
```

or chunked audio from:

```text
data/<entity>/audio/chunks/
```

or a direct input path.

## Output

VTT transcript files under:

```text
data/<entity>/transcripts_<model>/
```

Example:

```text
data/example_entity/transcripts_medium/
```

## CLI options

```text
-e / --entity
-c / --config
-i / --audio-input
-o / --output-dir
-g / --get-chunks
-m / --model
--faster
--transcription-profile
-l / --lang
-d / --device
-v / --verbose
-q / --quiet
```

## Usage examples

```bash
yatsee audio transcribe -e example_entity

yatsee audio transcribe \
  -e example_entity \
  --faster

yatsee audio transcribe \
  -e example_entity \
  --faster \
  --device cuda

yatsee audio transcribe \
  -e example_entity \
  --faster \
  --device cpu

yatsee audio transcribe \
  -e example_entity \
  --faster \
  --get-chunks \
  --transcription-profile qa_cleanup
```

---

# Stage 3b: Experimental Transcript QA

## Purpose

Inspect VTT transcripts before slicing, normalization, summarization, or signal extraction. This is especially useful for long batch runs where ASR artifacts can otherwise flow into every downstream stage.

The current QA helpers are experimental scripts, not formal CLI commands. They are intended to make transcript review and rebuild decisions repeatable while QA rules are tested against real outputs. The normal pipeline does not require this step.

## Input

VTT transcript files under:

```text
data/<entity>/transcripts_<model>/
```

## Output

Human-readable QA findings and optional JSON reports for rebuild/reset workflows.

## Current helper scripts

```bash
python scripts/qa_transcript_report.py --details
python scripts/qa_transcript_report.py --json qa_report.json
```

To reset bad VTTs for rebuild from a QA JSON report:

```bash
python scripts/qa_reset_vtt_for_rebuild.py \
  --entity example_entity \
  --qa-report qa_report.json
```

Add `--apply` only after reviewing the dry-run output.

```bash
python scripts/qa_reset_vtt_for_rebuild.py \
  --entity example_entity \
  --qa-report qa_report.json \
  --apply
```

Then rerun transcription with the QA cleanup profile. The transcript hash tracker will allow only the reset files to rebuild.

```bash
yatsee audio transcribe \
  -e example_entity \
  --faster \
  --get-chunks \
  --transcription-profile qa_cleanup
```

For ASR loop findings, rebuilding with the same transcription behavior may reproduce the same failure. The `qa_cleanup` profile disables previous-text conditioning to reduce loop propagation. Use it for QA-selected rebuilds, not as a general accuracy guarantee.

## QA boundary

QA can report ASR loops and prepare bad transcripts for rebuild. QA fixes should not repair ASR loop content. Content corruption must be handled by retranscription or future chunk-level fallback.

Timestamp-only issues, such as impossible cue durations or minor cue overlaps, may become safe repair targets in a future timestamp fixer.

---

# Stage 4a: Slicing Transcripts into Segments

## Purpose

Convert VTT transcripts into plain text and optional structured JSONL segments.

## Input

VTT files from:

```text
data/<entity>/transcripts_<model>/
```

## Output

Plain text transcript files:

```text
data/<entity>/transcripts_<model>/*.txt
```

Optional segment JSONL files:

```text
data/<entity>/transcripts_<model>/*.segments.jsonl
```

## CLI options

```text
-e / --entity
-c / --config
-i / --vtt-input
-o / --output-dir
-m / --model
-g / --gen-embed
--max-window
--force
-d / --device
```

## Usage examples

```bash
yatsee transcript slice \
  -e example_entity \
  --force

yatsee transcript slice \
  -e example_entity \
  --gen-embed \
  --device cuda \
  --force
```

---

# Stage 4b: Transcript Normalization and Structure

## Purpose

Clean transcript text into a more usable normalized form.

## Input

Plain TXT transcript files, usually from:

```text
data/<entity>/transcripts_<model>/
```

## Output

Normalized TXT files under:

```text
data/<entity>/normalized/
```

## CLI options

```text
-e / --entity
-c / --config
-i / --input-path
-o / --output-dir
-m / --model
--no-spacy
--deep-clean
--preserve-paragraphs
--force
```

## Usage examples

```bash
yatsee transcript normalize \
  -e example_entity \
  --force

yatsee transcript normalize \
  -e example_entity \
  --model medium \
  --force

yatsee transcript normalize \
  -e example_entity \
  --preserve-paragraphs \
  --force
```

---

# Stage 5a: Intelligence and Summarization

Canonical command: `yatsee intel summarize`. The older `yatsee intel run` command remains available as an alias.


## Purpose

Generate structured summaries or higher-level intelligence artifacts from transcript text using a configurable provider layer.

## Input

Usually normalized transcript text:

```text
data/<entity>/normalized/
```

or a direct TXT file/directory.

## Output

Final summaries written to a configured or specified output directory.

Common examples:

```text
data/<entity>/summary_nemo/
data/<entity>/summary_<model>/
```

## CLI options

```text
-e / --entity
-c / --config
-i / --txt-input
-o / --output-dir
-m / --model
--llm-provider
--llm-provider-url
--llm-api-key    # accepted, but prefer llm_api_key_env or YATSEE_LLM_API_KEY
--show-pricing
--no-show-pricing
--pricing-provider
--pricing-model
-f / --output-format
-j / --job-profile
-s / --chunk-style
-w / --max-words
-t / --max-tokens
-p / --max-pass
-d / --disable-auto-classification
--first-prompt
--second-prompt
--final-prompt
--context
--print-prompts
--enable-chunk-writer
```

## Usage examples

```bash
yatsee intel summarize -e example_entity

yatsee intel summarize \
  -e example_entity \
  --llm-provider ollama \
  --llm-provider-url http://localhost:11434 \
  --model mistral-nemo:latest

yatsee intel summarize \
  -e example_entity \
  --print-prompts

yatsee intel summarize \
  -e example_entity \
  --enable-chunk-writer
```

---

# Stage 5b: Meeting Signals

## Purpose

Generate deterministic meeting-signal artifacts from normalized transcripts.

This stage emits mechanical evidence candidates. It does not produce final minutes or official records.

## Input

Normalized transcript text:

```text
data/<entity>/normalized/
```

or a direct TXT file/directory.

## Output

Signal artifacts under:

```text
data/<entity>/meeting_signals/
```

Typical filename:

```text
<base_name>.signals.md
```

## CLI options

```text
-e / --entity
-c / --config
-i / --input-path
-o / --output-dir
--force
```

## Usage examples

```bash
yatsee intel signals -e example_entity

yatsee intel signals \
  -e example_entity \
  --force

yatsee intel signals \
  --input-path ./normalized \
  --output-dir ./meeting_signals
```

---

# Typical Full Pipeline

```bash
yatsee config entity add \
  --display-name "Example County Board" \
  --entity example_county_board \
  --base "country.US.state.EX."

yatsee config init --entity example_county_board
nano data/example_county_board/config.toml

mkdir -p data/example_county_board/downloads
cp /path/to/raw/media/* data/example_county_board/downloads/

yatsee config validate --entity example_county_board
yatsee config resolve --entity example_county_board

yatsee audio format -e example_county_board
yatsee audio transcribe -e example_county_board --faster
yatsee transcript slice -e example_county_board --force
yatsee transcript normalize -e example_county_board --force
yatsee intel summarize -e example_county_board
yatsee intel signals -e example_county_board
```

---

# Output Directory Overview

A typical entity directory may look like:

```text
data/example_county_board/
├── audio/
├── config.toml
├── downloads/
├── meeting_signals/
├── normalized/
├── summary_nemo/
├── transcripts_medium/
└── yatsee_db/
```

## Directory roles

| Directory | Purpose |
|---|---|
| `downloads/` | Provider-neutral raw media input. |
| `audio/` | Formatted transcription-ready audio. |
| `transcripts_<model>/` | VTT transcripts and sliced TXT outputs. |
| `normalized/` | Cleaned normalized transcript text. |
| `summary_<model>/` | LLM-generated summaries for a specific summarization model. |
| `meeting_signals/` | Deterministic signal artifacts. |
| `yatsee_db/` | Optional or experimental local vector/index storage. |

Temporary comparison directories may exist during model or prompt testing:

```text
transcripts_medium_en/
summary_nemo_bak/
transcripts_medium_old/
```

---

# Troubleshooting Workflow

## Audio formatting finds nothing

```bash
find data/example_entity/downloads -maxdepth 2 -type f
yatsee audio format -e example_entity --dry-run
```

## Transcription skips files

```bash
find data/example_entity/audio -type f
```

## Transcript QA finds ASR loops

```bash
python scripts/qa_transcript_report.py --details
python scripts/qa_transcript_report.py --json qa_report.json
```

For transcripts marked `rerun` or `block`, reset the affected VTTs and tracker rows before retranscribing:

```bash
python scripts/qa_reset_vtt_for_rebuild.py \
  --entity example_entity \
  --qa-report qa_report.json \
  --apply
```

Then run transcription again with the QA cleanup profile before slicing or normalization:

```bash
yatsee audio transcribe \
  -e example_entity \
  --faster \
  --get-chunks \
  --transcription-profile qa_cleanup
```

## Slice stage finds nothing

```bash
find data/example_entity/transcripts_medium -name '*.vtt'
yatsee transcript slice -e example_entity --force
```

## Normalize stage finds nothing

```bash
find data/example_entity/transcripts_medium -name '*.txt'
yatsee transcript normalize -e example_entity --force
```

## Summaries are poor

Check in this order:

1. raw media quality
2. formatted audio quality
3. transcription quality
4. normalized transcript quality
5. entity-specific replacements
6. local people / titles / divisions
7. supported civic prompt profile wiring
8. model choice

## Signals are missing

```bash
find data/example_entity/normalized -name '*.txt'
yatsee intel signals -e example_entity --force
```

---

# Final Guidance

The durable workflow is:

```text
raw media
→ formatted audio
→ VTT transcript
→ optional transcript QA
→ sliced text
→ normalized transcript
→ optional LLM summary
→ deterministic signal artifacts
→ optional search / publishing
```

Use LLM summaries as an enhancement, not as the spine of the system.