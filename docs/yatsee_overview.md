# YATSEE Audio Pipeline Overview

A modular, local-first pipeline for processing, transcribing, normalizing, and analyzing long-form civic meeting media.

## Process Flow

![process_flow](./assets/yatsee_process_flow.png)

**Pipeline Flow (Top-Level)**  
`downloads/` → `audio/` → `transcripts_<model>/` → `normalized/` → `summary/`

Optional downstream workflows can build on those artifacts, including vector indexing and search.

---

## Pipeline Flow Overview

1. `downloads/` → provider-neutral raw media input  
2. `audio/` → converted 16 kHz mono transcription-ready audio  
3. `transcripts_<model>/` → `.vtt` transcripts and transcript-derived text artifacts  
4. `normalized/` → cleaned, structured `.txt`  
5. `summary/` → `.md` or `.yaml` summaries  
6. `yatsee_db/` → optional vector database files for retrieval workflows  

YATSEE is designed as a staged pipeline with explicit boundaries between raw media intake, audio preparation, transcript generation, transcript cleanup, and higher-level intelligence. Each stage can be run independently as long as the input artifacts match the expected contract.

Provider-specific acquisition is not part of the core pipeline. External acquisition tools, upload jobs, recording workflows, or manual copies can place compatible media into the raw media directory before YATSEE begins processing.

---

## 1. Raw Media Intake

- **Input:** compatible media files supplied by an external tool, recorder, upload, copy, or manual import
- **Default location:** `downloads/`
- **Config:** `[media].input_dir`
- **CLI override:** `yatsee audio format --input-dir <path>`

### Purpose

Provide a stable, provider-neutral location for raw media files that will be normalized by the audio formatting stage.

### Notes

- YATSEE does not need to know whether raw media came from a video platform, recorder, upload workflow, archive, or local filesystem.
- `downloads/` is retained as the current default directory name for compatibility.
- A future layout may rename this directory to `incoming/`, but the core contract is simply “raw compatible media exists here.”

---

## 2. Audio Formatting

- **Command:** `yatsee audio format`
- **Input:** raw media from `downloads/`, `[media].input_dir`, or a direct CLI path
- **Output:** normalized `.wav` or `.flac` in `audio/`
- **Tooling:** `ffmpeg`, `ffprobe`

### Purpose

Convert raw media into a consistent, transcription-ready audio format.

### Format Settings

Typical output is normalized to:

- mono audio
- 16 kHz sample rate
- `.wav` or `.flac`

### Notes

- Supports chunked output for long recordings.
- Supports overlap between chunks to reduce boundary loss.
- Supports dry-run and force modes.
- Intended to produce stable audio artifacts for downstream ASR.

### Example

```bash
yatsee audio format --entity example_entity
yatsee audio format --entity example_entity --create-chunks --chunk-duration 300
yatsee audio format --input-dir ./raw_media --output-dir ./audio --format wav
```

---

## 3. Transcribe Audio

- **Command:** `yatsee audio transcribe`
- **Input:** formatted audio from `audio/`
- **Output:** `.vtt` transcripts in `transcripts_<model>/`
- **Tooling:** `openai-whisper` or `faster-whisper`

### Purpose

Transform normalized audio into time-aligned transcript artifacts suitable for slicing, normalization, and summarization.

### Notes

- Supports Whisper and faster-whisper execution paths.
- Supports CUDA, CPU, and Apple MPS selection.
- Supports chunked audio workflows.
- Produces VTT as the primary transcript artifact.

### Example

```bash
yatsee audio transcribe --entity example_entity
yatsee audio transcribe --audio-input ./audio --model medium --faster
yatsee audio transcribe --audio-input ./single_file.mp3 --device cpu --lang es
```

---

## 4. Transcript Preparation

### 4a. Slice VTT into TXT and JSONL Segments

- **Command:** `yatsee transcript slice`
- **Input:** `.vtt` from `transcripts_<model>/`
- **Output:** plain `.txt` plus optional `.segments.jsonl`
- **Tooling:** sentence-aware transcript slicing, optional embedding generation

### Purpose

Convert VTT transcripts into cleaner transcript text and structured, timestamp-aligned segment records.

### Notes

- Produces plain text transcript artifacts for later stages.
- Produces JSONL segments for retrieval-oriented workflows.
- Optional embedding generation supports segment-level semantic indexing.
- Keeps timestamps aligned with segment content.

### Example

```bash
yatsee transcript slice --entity example_entity
yatsee transcript slice --entity example_entity --gen-embed
yatsee transcript slice --vtt-input ./transcripts --max-window 30 --force
```

### 4b. Normalize Transcript Text

- **Command:** `yatsee transcript normalize`
- **Input:** `.txt` transcript artifacts, typically derived from `transcripts_<model>/`
- **Output:** cleaned `.txt` in `normalized/`
- **Tooling:** `spaCy` plus configured replacement rules

### Purpose

Convert transcript text into cleaner, more consistent, AI-ready artifacts for summarization, embeddings, or semantic search.

### Notes

- Supports sentence splitting with spaCy.
- Supports optional deep cleaning.
- Supports paragraph preservation.
- Applies configured replacement rules for recurring ASR mistakes.
- Produces stable normalized text artifacts for later stages.

### Example

```bash
yatsee transcript normalize --entity example_entity
yatsee transcript normalize --input-path ./transcripts_medium --output-dir ./normalized_out
yatsee transcript normalize --entity example_entity --deep-clean
```

---

## 5. Intelligence, Summarization & Provider-Based LLM Processing

- **Command:** `yatsee intel run`
- **Input:** normalized `.txt` from `normalized/` or a user-specified transcript file/directory
- **Output:** `.md` or `.yaml` summaries in `summary/`
- **Tooling:** provider-based LLM execution through local runtimes, hosted APIs, or CLI-backed integrations

### Purpose

Generate structured meeting summaries and other higher-level intelligence artifacts from transcript text using a configurable provider layer instead of a single hardcoded model runtime.

### Notes

- Supports multi-pass summarization.
- Supports automatic meeting classification.
- Supports prompt routing by job type.
- Supports Markdown or YAML output.
- Supports manual prompt overrides.
- Supports chunk styles including word, sentence, and density-aware chunking.
- Supports provider-based execution through backends such as Ollama, llama.cpp-compatible endpoints, OpenAI, Anthropic, and CLI-backed providers.
- Supports optional reference pricing so local runs can report an estimated hosted API-equivalent cost.
- Applies provider-target hardening so local-first execution remains the default security posture.

### How It Works

YATSEE classifies transcript content when appropriate, resolves prompt routing, and processes transcripts through a multi-pass summarization workflow. Transcripts are chunked based on word, sentence, or density-aware strategies depending on configuration and input size. Chunk-level summaries are refined across passes until a final structured report is produced.

The intelligence stage uses a provider abstraction. The summarization workflow stays the same even when the backend changes. The runtime provider used for generation can be local or remote, and the reference provider used for pricing can be different from the actual execution backend.

Before execution, YATSEE validates the configured provider target against its security policy. By default:

- remote non-local targets for local HTTP providers are blocked
- insecure HTTP for hosted providers is blocked
- custom CLI executable targets are blocked unless explicitly allowed in config

### Example

```bash
yatsee intel run -e example_entity

yatsee intel run -e example_entity --model llama3:latest --llm-provider ollama --llm-provider-url http://localhost:11434

yatsee intel run --txt-input ./normalized/council_meeting.txt --model mistral-nemo:latest --llm-provider llamacpp --llm-provider-url http://localhost:8080 --context "City Council - June 2025"
```

### Design Notes

- Summarization is intentionally multi-pass because long civic transcripts routinely exceed comfortable single-pass context windows.
- Prompt orchestration, provider execution, pricing estimation, chunking, and output writing are separate concerns internally.
- This stage is designed to extract durable, structured intelligence rather than produce a generic free-form recap.
- Reference pricing is only an estimate. It depends on the configured pricing table and estimated token counts unless exact provider usage metadata is available.
- Provider hardening settings are intentionally config-driven so security-sensitive behavior is not casually weakened through one-off CLI flags.

---

## Optional Downstream Workflows

### Index Data

- **Current script:** `yatsee_index_data.py`
- **Input:** normalized transcript text, summaries, and optional segment artifacts
- **Output:** vector database files in `yatsee_db/`
- **Tooling:** `ChromaDB`, embedding models such as `BAAI/bge-small-en-v1.5`

### Purpose

Generate embeddings and store retrieval-oriented artifacts for semantic search and downstream query workflows.

### Notes

- Built on top of core pipeline outputs.
- Not required for the main audio-to-summary workflow.
- Intended for retrieval, semantic search, and exploration workflows.

### Search

- **Current interface:** `yatsee_search_demo.py`
- **Input:** normalized text, summaries, and indexed retrieval artifacts
- **Tooling:** Streamlit plus ChromaDB-backed retrieval

### Purpose

Provide a simple queryable surface over processed YATSEE artifacts.

### Notes

- Acts as a consumer of pipeline outputs.
- Separate from the core audio processing stages.
- Suitable for exploration, review, and retrieval workflows.

---

## Filesystem Layout

```text
data/
└── <entity_handle>/
    ├── downloads/                ← Raw media input
    ├── audio/                    ← Converted 16 kHz mono audio files
    ├── transcripts_<model>/      ← VTT transcripts and transcript-derived text artifacts
    ├── normalized/               ← Cleaned and structured text output
    ├── summary/                  ← Generated meeting summaries (.md/.yaml)
    ├── yatsee_db/                ← Optional vector database files (ChromaDB)
    ├── prompts/                  ← Optional entity-specific prompt overrides
    └── config.toml               ← Localized entity configuration
```

---

## Config File Routing / Load Order

```text
Global yatsee.toml
    |
    +--> Entity handle
            |
            +--> Local config.toml
                    |
                    +--> Merged runtime configuration
                            |
                            +--> Pipeline stage execution
```

---

## Prompt Override Layout Example

```text
./prompts/
  └── research/
      └── prompts.toml          # default prompts for the 'research' job type

./data/
  └── example_entity/
      └── prompts/
          └── research/
              └── prompts.toml  # entity-specific override for 'research'
```

**Behavior:**

- YATSEE first checks `data/<entity>/prompts/<job_profile>/prompts.toml`.
- If found, that file overrides the default job prompts.
- If not found, YATSEE falls back to `prompts/<job_profile>/prompts.toml`.
- If no prompt file exists, inline fallback prompts may be used depending on the stage.

---

## Running the Pipeline

Run each stage in sequence or independently as needed.

All major stages accept the entity handle so work is routed to the correct data layout defined by the configuration.

### Command Summary

| Command | Purpose |
|---|---|
| `yatsee audio format --entity <entity>` | Convert raw media to normalized audio |
| `yatsee audio transcribe --entity <entity>` | Transcribe audio files to VTT |
| `yatsee transcript slice --entity <entity>` | Slice VTT into transcript text and structured segments |
| `yatsee transcript normalize --entity <entity>` | Clean and normalize transcript text |
| `yatsee intel run -e <entity>` | Generate summaries from transcript text through the configured provider |
| `python yatsee_index_data.py -e <entity>` | Build optional retrieval/index artifacts |
| `streamlit run yatsee_search_demo.py -- -e <entity>` | Explore processed outputs through the demo search UI |

---

## Design Principles

- **Modular stages**  
  Each stage has a clear contract and can be run independently when inputs are valid.

- **Local-first processing**  
  The pipeline is designed to run on local hardware without requiring cloud APIs for its core workflow, while still allowing hosted providers when needed.

- **Deterministic artifacts**  
  Output paths and stage boundaries are explicit so artifacts remain inspectable and reusable.

- **Entity-specific context**  
  Local configuration improves name recognition, replacements, and downstream summary quality.

- **Provider abstraction**  
  Intelligence-stage logic is decoupled from any single LLM runtime so provider changes do not require rewriting the workflow.

- **Downstream extensibility**  
  Retrieval, indexing, and search can be layered on top of the core audio pipeline without redefining the pipeline itself.