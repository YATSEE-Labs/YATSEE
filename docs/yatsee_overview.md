# YATSEE Pipeline Overview

YATSEE is a modular, local-first pipeline for processing long-form meeting media into auditable local artifacts.

The current system is organized around three broad layers:

```text
supply media
  ↓
format → transcribe → optional QA → slice → normalize → summarize / extract signals
  ↓
index → search / investigate
```

YATSEE core owns the middle processing layer. Acquisition, hosted upload handling, publication, and downstream search can be handled by adjacent tools or applications that hand off local files or explicit artifact references.


## Current Operating Scope

The supported local workflow is staged media processing: raw media is supplied locally, processed through staged audio/transcript/intelligence commands, and written back as inspectable filesystem artifacts.

Current first-class intelligence commands are:

```bash
yatsee intel summarize -e <entity>
yatsee intel signals -e <entity>
yatsee intel prompts validate
```

`yatsee intel run` remains a compatibility alias for `yatsee intel summarize`. Intelligence workflows are profile-driven; additional profiles should extend the shared profile/routing mechanism rather than creating one-off processing paths.

---

## Process Flow

![process_flow](./assets/yatsee_process_flow.png)

## Top-Level Flow

```text
downloads/
  ↓
audio/
  ↓
transcripts_<model>/
  ↓
optional transcript QA
  ↓
normalized/
  ↓
summary_<model>/ and meeting_signals/
  ↓
yatsee_db/ or external search/index tooling
```

Each stage can be run independently when the expected input artifacts exist.

---

## Package and CLI Shape

YATSEE is packaged as a Python CLI application.

Current CLI organization:

```text
src/yatsee/
  __main__.py
  cli/
    main.py
    config.py
    audio.py
    transcript.py
    intel.py
  audio/
  transcript/
  intel/
  providers/
  core/
  config_tools/
```

Supported entrypoints:

```bash
yatsee --help
python -m yatsee --help
```

Command families:

```text
yatsee config ...
yatsee audio ...
yatsee transcript ...
yatsee intel ...
```

The CLI layer translates terminal arguments into stage function calls. Stage logic lives in package modules, not in the parser files.

---

## Entity Data Layout

YATSEE stores artifacts under one directory per configured entity.

General shape:

```text
data/<entity>/
  config.toml
  downloads/
  audio/
  transcripts_<model>/
  normalized/
  summary_<model>/
  meeting_signals/
  yatsee_db/
```

Directory roles:

| Directory | Purpose |
|---|---|
| `config.toml` | Entity-local configuration. |
| `downloads/` | Provider-neutral raw media input. Often produced by acquisition tools. |
| `audio/` | Formatted 16 kHz mono transcription-ready audio. |
| `transcripts_<model>/` | Transcription outputs for a specific transcription model. |
| `normalized/` | Cleaned transcript text for intelligence and indexing stages. |
| `summary_<model>/` | Summary outputs for a specific summarization model. |
| `meeting_signals/` | Deterministic signal artifacts extracted from normalized transcripts. |
| `yatsee_db/` | Optional or experimental index/search storage. |

Model-specific directories intentionally encode provenance. For example, `transcripts_medium/` and `summary_nemo/` identify the model family used to produce those artifacts.

Temporary comparison directories may also exist during development, such as:

```text
transcripts_medium_en/
summary_nemo_bak/
transcripts_medium_old/
```

These are useful for comparing models, prompt changes, or processing changes against prior outputs.

---

## 1. Raw Media Intake

- **Input:** compatible media files supplied by an external tool, recorder, upload, copy, or manual import
- **Default location:** `downloads/`
- **Config:** `[media].input_dir`
- **CLI override:** `yatsee audio format --input-dir <path>`

YATSEE core does not need to know whether raw media came from YouTube, a recorder, an upload, an archive, or a local filesystem.

---

## 2. Audio Formatting

- **Command:** `yatsee audio format`
- **Input:** raw media from `downloads/`, `[media].input_dir`, or direct CLI path
- **Output:** normalized `.wav` or `.flac` in `audio/`
- **Tooling:** `ffmpeg`, `ffprobe`

Typical output:

```text
mono
16 kHz
.flac or .wav
```

The stage supports chunked output, overlap, dry-run, and force modes.

---

## 3. Transcription

- **Command:** `yatsee audio transcribe`
- **Input:** formatted audio from `audio/`
- **Output:** `.vtt` transcripts in `transcripts_<model>/`
- **Tooling:** `openai-whisper` or `faster-whisper`

VTT is the primary transcript artifact because it preserves timing.

---

## Optional Transcript QA

Experimental helper scripts can inspect VTT outputs before they are sliced, normalized, summarized, or indexed. This is useful for long batch runs where ASR loops or timestamp artifacts can otherwise propagate into downstream artifacts.

This is a separate recovery workflow, not part of the normal happy-path command sequence.

Current helpers:

```bash
python scripts/qa_transcript_report.py --details
python scripts/qa_transcript_report.py --json qa_report.json
python scripts/qa_reset_vtt_for_rebuild.py --entity <entity> --qa-report qa_report.json
```

If the reset helper selects transcripts for rebuild, rerun transcription with the standard defaults:

```bash
yatsee audio transcribe \
  -e <entity> \
  --faster \
  --get-chunks
```

The QA boundary is intentionally strict: ASR loop/content corruption findings are rebuild signals, not fixer targets. Timestamp-only issues may be reported or later repaired by conservative QA fix tooling.

---

## 4. Transcript Preparation

### 4a. Slice VTT into TXT and JSONL Segments

- **Command:** `yatsee transcript slice`
- **Input:** `.vtt` from `transcripts_<model>/`
- **Output:** `.txt` and optional `.segments.jsonl`

Plain text output feeds normalization. JSONL segments can support retrieval workflows.

### 4b. Normalize Transcript Text

- **Command:** `yatsee transcript normalize`
- **Input:** `.txt` transcript artifacts
- **Output:** cleaned `.txt` in `normalized/`

Normalized text is the preferred input for summarization, signal extraction, and search indexing.

---

## 5. Intelligence and Summarization

- **Command:** `yatsee intel summarize`
- **Input:** normalized `.txt`
- **Output:** `.md` or `.yaml` summaries in `summary_<model>/` or a configured output directory
- **Tooling:** provider-based LLM execution

The intelligence stage supports multi-pass summarization, automatic meeting classification, prompt routing, local and hosted LLM providers, optional reference pricing, and provider-target hardening.

Provider backends may include:

```text
ollama
llamacpp
openai
anthropic
codex_cli
```

Provider trust boundaries are enforced before model execution:

| Target type | Required setting | Notes |
|---|---|---|
| Loopback HTTP, such as `http://localhost:11434` | `llm_allow_loopback_http=true` | Intended for local Ollama/llama.cpp. |
| Remote HTTPS | `llm_allow_remote=true` | Intended for hosted APIs or explicitly trusted remote services. |
| Remote/LAN HTTP | `llm_allow_remote=true` and `llm_allow_insecure_http=true` | Use only for trusted LAN/VPN-style model servers. |
| Custom CLI executable | `llm_allow_custom_executable=true` | Applies to CLI-backed providers such as `codex_cli`. |


---

## 6. Deterministic Meeting Signals

- **Command:** `yatsee intel signals`
- **Input:** normalized `.txt`
- **Output:** `.signals.md` artifacts in `meeting_signals/`
- **Tooling:** deterministic text extraction

Signals are mechanical evidence candidates. They are not official minutes and not final meeting records.

Typical signal categories include:

```text
actions
roll calls
money references
civic objects
questions
people mentions
low-confidence lines
```

---

## Optional Downstream Workflows

### Index Data

- **Current script/prototype:** `examples/yatsee_index_data.py`
- **Input:** normalized transcript text, summaries, and optional segment artifacts
- **Output:** vector database files, often under `yatsee_db/`

### Search / Investigation

- **Current script/prototype:** `examples/yatsee_search_demo.py`
- **Input:** normalized text, summaries, and indexed retrieval artifacts
- **Tooling:** Streamlit plus ChromaDB-backed retrieval

Indexing and search consume core pipeline outputs. They are not required for the main audio-to-summary workflow and may eventually become a separate package or application.

---

## Running the Pipeline

```bash
yatsee audio format -e <entity>
yatsee audio transcribe -e <entity>
yatsee transcript slice -e <entity>
yatsee transcript normalize -e <entity>
yatsee intel summarize -e <entity>
yatsee intel signals -e <entity>
```

Command summary:

| Command | Purpose |
|---|---|
| `yatsee config ...` | Manage and inspect configuration. |
| `yatsee audio format -e <entity>` | Convert raw media to normalized audio. |
| `yatsee audio transcribe -e <entity>` | Transcribe audio files to VTT. |
| `yatsee transcript slice -e <entity>` | Slice VTT into transcript text and structured segments. |
| `yatsee transcript normalize -e <entity>` | Clean and normalize transcript text. |
| `yatsee intel summarize -e <entity>` | Generate summaries through the configured provider. |
| `yatsee intel signals -e <entity>` | Generate deterministic meeting signal artifacts. |

---

## Design Principles

- **Modular stages:** each stage has a clear input/output contract.
- **Local-first processing:** cloud APIs are optional, not required for the core workflow.
- **Inspectable artifacts:** filesystem outputs are explicit and reusable.
- **Entity-specific context:** local config improves names, replacements, and output quality.
- **Provider abstraction:** LLM runtime choice does not redefine the summarization workflow.
- **Loose coupling:** acquisition, processing, and search/indexing are connected by files.