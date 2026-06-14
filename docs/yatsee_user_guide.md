# YATSEE: User Guide

YATSEE is a local-first audio extraction and processing pipeline designed to turn raw meeting audio into clean, searchable, and auditable artifacts.

It can:

- fetch public source media
- normalize audio
- transcribe speech
- slice transcript cues into text and optional JSONL segments
- normalize transcript text
- generate deterministic Meeting Record Extracts
- generate LLM-based summaries through configurable providers

The pipeline is intentionally staged. Each stage produces useful artifacts and can be inspected independently.

---

# Table of Contents

- [Before Stage 1: Create or Select an Entity](#before-stage-1-create-or-select-an-entity)
- [Stage 1: Audio Intake and Download](#stage-1-audio-intake-and-download)
- [Stage 2: Audio Formatting and Chunking](#stage-2-audio-formatting-and-chunking)
- [Stage 3: Transcription](#stage-3-transcription)
- [Stage 4a: Slicing Transcripts into Segments](#stage-4a-slicing-transcripts-into-segments)
- [Stage 4b: Transcript Normalization and Structure](#stage-4b-transcript-normalization-and-structure)
- [Stage 5a: Meeting Record Extracts](#stage-5a-meeting-record-extracts)
- [Stage 5b: Intelligence, Summarization, and Provider-Based LLM Processing](#stage-5b-intelligence-summarization-and-provider-based-llm-processing)
- [Typical Full Pipeline](#typical-full-pipeline)
- [Output Directory Overview](#output-directory-overview)
- [Troubleshooting Workflow](#troubleshooting-workflow)

---

# Before Stage 1: Create or Select an Entity

Most YATSEE runs are entity-driven.

An entity represents a source group such as:

- a city council
- a county board
- a school board
- a committee
- a public meeting channel
- another recurring source body

Before fetching media, make sure the entity exists in the global registry and has a local `config.toml`.

## List existing entities

```bash
yatsee config entity list
```

## Add a new entity

Example:

```bash
yatsee config entity add \
  --display-name "Example County Board" \
  --entity example_county_board \
  --base "country.US.state.EX." \
  --inputs youtube
```

This registers the entity globally.

## Scaffold local config

```bash
yatsee config init --entity example_county_board
```

This creates:

```text
data/example_county_board/config.toml
```

The scaffold does not overwrite an existing local config.

## Edit local config

```bash
nano data/example_county_board/config.toml
```

At minimum, set the YouTube source path:

```toml
[sources.youtube]
youtube_path = "@replace_with_channel_or_playlist"
enabled = true
```

For a county board, also review:

```toml
[settings]
entity_type = "county_board"
entity_level = "county"
location = "Example County, Example State"
```

The scaffold may need manual correction for non-city civic bodies.

## Validate the entity

```bash
yatsee config validate --entity example_county_board
```

## Inspect resolved config

```bash
yatsee config resolve --entity example_county_board
```

Use this before source fetching. It shows the merged runtime configuration that YATSEE will actually use.

---

# Stage 1: Audio Intake and Download

## Purpose

Fetch source media for a configured entity and place it into the pipeline as raw input.

This is typically used for YouTube-backed public meeting acquisition.

## Input

- configured entity source definitions
- optional explicit source adapter
- optional output directory override
- optional date filters

## Output

Downloaded source media under:

```text
data/<entity>/downloads/
```

or a user-specified output directory.

## How it works

YATSEE loads the global config, loads the entity config, merges them, resolves enabled source settings, then runs the selected source adapter.

For YouTube, YATSEE uses the configured `youtube_path`.

The stage is intended to be repeatable.

YATSEE uses tracking files such as:

```text
.downloaded
.playlist_ids.json
```

to avoid needless repeated work.

## CLI options

```text
-e / --entity
--source
-c / --config
-o / --output-dir
--date-after
--date-before
--make-playlist
```

## Usage examples

Build or refresh playlist metadata:

```bash
yatsee source fetch \
  -e example_entity \
  --make-playlist
```

Fetch media:

```bash
yatsee source fetch \
  -e example_entity
```

Fetch only recent media:

```bash
yatsee source fetch \
  -e example_entity \
  --date-after 20250101
```

Fetch from a specific source adapter:

```bash
yatsee source fetch \
  -e example_entity \
  --source youtube
```

Override output directory:

```bash
yatsee source fetch \
  -e example_entity \
  --output-dir ./downloads
```

## Design notes

- Source acquisition is separated from audio formatting.
- Repeated runs should skip already-known items.
- Playlist cache generation can be run independently.
- This is the safest first stage to test when onboarding a new entity.

---

# Stage 2: Audio Formatting and Chunking

## Purpose

Convert raw source media into transcription-ready audio.

This stage enforces a consistent audio format so transcription behavior is more predictable.

## Input

Source files from:

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

## How it works

YATSEE discovers media files, resolves formatting settings, and uses `ffmpeg` / `ffprobe` to normalize audio.

Typical target:

```text
mono
16 kHz
flac or wav
```

Long files can optionally be split into chunks.

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

Format all downloaded media for an entity:

```bash
yatsee audio format \
  -e example_entity
```

Use FLAC explicitly:

```bash
yatsee audio format \
  -e example_entity \
  --format flac
```

Preview formatting:

```bash
yatsee audio format \
  -e example_entity \
  --dry-run
```

Force reprocessing:

```bash
yatsee audio format \
  -e example_entity \
  --force
```

Create chunks:

```bash
yatsee audio format \
  -e example_entity \
  --create-chunks \
  --chunk-duration 600 \
  --chunk-overlap 2
```

## Design notes

- Formatting does not interpret content.
- Formatting preserves the source media and creates derived audio.
- FLAC usually saves space compared with WAV while preserving transcription quality.
- Chunking is useful for long recordings or constrained hardware.

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

## How it works

YATSEE runs Whisper or faster-whisper and writes timing-preserving VTT transcripts.

Supported execution devices include:

```text
auto
cuda
cpu
mps
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
-l / --lang
-d / --device
-v / --verbose
-q / --quiet
```

## Usage examples

Transcribe using entity defaults:

```bash
yatsee audio transcribe \
  -e example_entity
```

Use faster-whisper:

```bash
yatsee audio transcribe \
  -e example_entity \
  --faster
```

Force CUDA:

```bash
yatsee audio transcribe \
  -e example_entity \
  --faster \
  --device cuda
```

CPU fallback:

```bash
yatsee audio transcribe \
  -e example_entity \
  --faster \
  --device cpu
```

Transcribe direct input:

```bash
yatsee audio transcribe \
  --audio-input ./audio \
  --model medium \
  --faster
```

## Design notes

- VTT is the primary transcript artifact because it preserves timing.
- Transcription is separate from normalization and summarization.
- Better transcription quality improves every downstream stage.

---

# Stage 4a: Slicing Transcripts into Segments

## Purpose

Convert VTT transcripts into plain text and optional structured JSONL segments.

This stage bridges timing-preserving VTT files and downstream text processing.

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

## How it works

YATSEE reads VTT cues and consolidates transcript content into cleaner text.

When embedding generation is enabled, JSONL segment records can include timestamps and embeddings.

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

Slice transcripts into TXT:

```bash
yatsee transcript slice \
  -e example_entity \
  --force
```

Generate JSONL segments with embeddings:

```bash
yatsee transcript slice \
  -e example_entity \
  --gen-embed \
  --device cuda \
  --force
```

Direct VTT input:

```bash
yatsee transcript slice \
  --vtt-input ./transcripts_medium \
  --output-dir ./sliced
```

## Design notes

- Plain TXT output is required by normalization.
- JSONL segment output is optional.
- Embeddings are useful for search and retrieval workflows, but not required for the basic pipeline.

---

# Stage 4b: Transcript Normalization and Structure

## Purpose

Clean transcript text into a more usable normalized form.

This stage improves downstream extraction, summarization, and search quality.

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

## How it works

YATSEE applies text cleanup, sentence processing, spacing cleanup, and configured replacements.

Entity-specific replacements from local `config.toml` are especially useful here.

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

Normalize entity transcripts:

```bash
yatsee transcript normalize \
  -e example_entity \
  --force
```

Specify transcription model suffix:

```bash
yatsee transcript normalize \
  -e example_entity \
  --model medium \
  --force
```

Disable spaCy:

```bash
yatsee transcript normalize \
  -e example_entity \
  --no-spacy \
  --force
```

Preserve paragraph structure:

```bash
yatsee transcript normalize \
  -e example_entity \
  --preserve-paragraphs \
  --force
```

## Design notes

- Normalization is still deterministic text processing.
- It does not summarize or interpret.
- Normalized transcript text is usually the best source for deterministic extraction and search indexing.
- Replacement rules can materially improve output quality.

---

# Stage 5a: Meeting Record Extracts

## Purpose

Generate rules-based Meeting Record Extracts from normalized transcript text.

This stage does not call an LLM.

It creates deterministic Markdown artifacts that can help reviewers quickly inspect detected meeting structure.

## Input

Normalized TXT files from:

```text
data/<entity>/normalized/
```

or a direct input path.

## Output

Markdown extracts under:

```text
data/<entity>/record_extract/
```

Example:

```text
data/example_entity/record_extract/<base>.extract.md
```

## What it detects

Depending on transcript quality and meeting style, the extract may surface:

- meeting flow
- agenda items
- ordinances and resolutions
- motions
- seconds
- vote results
- roll calls
- money references
- organizations and entities
- people and roles
- discussion points
- public comment topics
- unresolved or verification-needed items

## CLI options

```text
-e / --entity
-c / --config
-i / --input-path
-o / --output-dir
--force
```

## Usage examples

Generate extracts for an entity:

```bash
yatsee intel extract \
  -e example_entity \
  --force
```

Run against direct input:

```bash
yatsee intel extract \
  --input-path ./normalized \
  --output-dir ./record_extract \
  --force
```

## Design notes

- This stage is deterministic.
- It is useful before any LLM summary exists.
- It should be treated as a review aid, not an official record.
- It may include false positives caused by transcript errors.
- It may miss items when transcript structure is poor.
- It can later be used as a summary coverage check.

## Why this stage matters

The Meeting Record Extract creates a stable civic review artifact without relying on generative AI.

That makes it useful for:

- publishing public meeting references
- checking whether summaries omitted important items
- building lightweight artifact websites
- reviewing meetings quickly before deeper analysis

---

# Stage 5b: Intelligence, Summarization, and Provider-Based LLM Processing

## Purpose

Generate structured summaries or other higher-level intelligence artifacts from transcript text using a configurable provider layer.

This is the LLM-backed intelligence stage.

## Input

Usually normalized transcript text:

```text
data/<entity>/normalized/
```

or a direct TXT file/directory.

## Output

Final summaries written to a configured or specified output directory.

Common output examples:

```text
data/<entity>/summary/
data/<entity>/summary_nemo/
```

## How it works

YATSEE can classify meeting type, resolve prompt routing, chunk large transcripts, process them through one or more passes, and write final summaries.

It uses a provider abstraction rather than a hardcoded model runtime.

Supported provider patterns include:

- local Ollama
- local llama.cpp / OpenAI-compatible runtimes
- OpenAI
- Anthropic
- CLI-backed providers such as `codex_cli`

## Provider security defaults

Provider hardening defaults to safe behavior:

```text
remote local-runtime targets blocked unless allowed
insecure hosted HTTP blocked unless allowed
custom executable targets blocked unless allowed
```

These are controlled in config, not casual one-off CLI flags.

## CLI options

```text
-e / --entity
-c / --config
-i / --txt-input
-o / --output-dir
-m / --model
--llm-provider
--llm-provider-url
--llm-api-key
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

Run with entity defaults:

```bash
yatsee intel run \
  -e example_entity
```

Use Ollama explicitly:

```bash
yatsee intel run \
  -e example_entity \
  --llm-provider ollama \
  --llm-provider-url http://localhost:11434 \
  --model mistral-nemo:latest
```

Run against one transcript:

```bash
yatsee intel run \
  --txt-input ./normalized/meeting.txt \
  --context "Example public meeting" \
  --model mistral-nemo:latest
```

Print resolved prompts:

```bash
yatsee intel run \
  -e example_entity \
  --print-prompts
```

Write intermediate chunk outputs:

```bash
yatsee intel run \
  -e example_entity \
  --enable-chunk-writer
```

## Design notes

- This stage is interpretive.
- It should generally run after transcription, slicing, and normalization.
- It is useful after the deterministic artifacts are already working.
- Deterministic extracts can later help validate whether summaries missed key items.

---

# Typical Full Pipeline

For a new YouTube-backed civic entity:

```bash
yatsee config entity add \
  --display-name "Example County Board" \
  --entity example_county_board \
  --base "country.US.state.EX." \
  --inputs youtube

yatsee config init \
  --entity example_county_board

nano data/example_county_board/config.toml

yatsee config validate \
  --entity example_county_board

yatsee config resolve \
  --entity example_county_board

yatsee source fetch \
  -e example_county_board \
  --make-playlist

yatsee source fetch \
  -e example_county_board

yatsee audio format \
  -e example_county_board

yatsee audio transcribe \
  -e example_county_board \
  --faster

yatsee transcript slice \
  -e example_county_board \
  --force

yatsee transcript normalize \
  -e example_county_board \
  --force

yatsee intel extract \
  -e example_county_board \
  --force
```

Optional LLM summary:

```bash
yatsee intel run \
  -e example_county_board
```

---

# Output Directory Overview

A typical entity directory may look like:

```text
data/example_county_board/
├── audio/
├── config.toml
├── downloads/
├── normalized/
├── record_extract/
├── summary/
├── summary_nemo/
├── transcripts_medium/
└── yatsee_db/
```

## `downloads/`

Raw fetched source media and source tracking files.

## `audio/`

Formatted transcription-ready audio.

## `transcripts_<model>/`

VTT transcripts and sliced TXT outputs.

Example:

```text
transcripts_medium/
```

## `normalized/`

Cleaned normalized transcript text.

## `record_extract/`

Rules-based Meeting Record Extract Markdown files.

## `summary/` or `summary_<model>/`

LLM-generated summaries.

## `yatsee_db/`

Optional local vector/index storage when retrieval workflows are enabled.

---

# Troubleshooting Workflow

## Entity does not appear

```bash
yatsee config entity list
```

If missing, add it:

```bash
yatsee config entity add \
  --display-name "Example Entity" \
  --entity example_entity \
  --base "country.US.state.EX." \
  --inputs youtube
```

## Local config missing

```bash
yatsee config init --entity example_entity
```

## Source fetch finds nothing

Check:

```bash
yatsee config resolve --entity example_entity
```

Verify:

```text
sources.youtube.youtube_path
sources.youtube.enabled
```

Then rebuild playlist:

```bash
yatsee source fetch -e example_entity --make-playlist
```

## Audio formatting finds nothing

Check downloads:

```bash
find data/example_entity/downloads -maxdepth 2 -type f
```

Run dry-run:

```bash
yatsee audio format -e example_entity --dry-run
```

## Transcription skips files

Check formatted audio:

```bash
find data/example_entity/audio -type f
```

If needed, use a separate output directory or intentionally remove stale output files before rerunning.

## Slice stage finds nothing

Check VTTs:

```bash
find data/example_entity/transcripts_medium -name '*.vtt'
```

Run:

```bash
yatsee transcript slice -e example_entity --force
```

## Normalize stage finds nothing

Check sliced TXT files:

```bash
find data/example_entity/transcripts_medium -name '*.txt'
```

Run:

```bash
yatsee transcript normalize -e example_entity --force
```

## Extract stage finds nothing

Check normalized TXT files:

```bash
find data/example_entity/normalized -name '*.txt'
```

Run:

```bash
yatsee intel extract -e example_entity --force
```

## Summaries are poor

Check in this order:

1. source audio quality
2. transcription quality
3. normalized transcript quality
4. entity-specific replacements
5. local people / titles / divisions
6. prompt profile
7. model choice

Do not tune prompts before confirming transcript quality.

---

# Final Guidance

The most useful YATSEE workflow is not “throw audio at an LLM.”

The durable workflow is:

```text
source media
→ formatted audio
→ VTT transcript
→ sliced text
→ normalized transcript
→ deterministic meeting record extract
→ optional LLM summary
→ optional search / publishing
```

The deterministic layers create value before any LLM runs.

Use LLM summaries as an enhancement, not as the spine of the system.