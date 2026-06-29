# YATSEE

## Yet Another Tool for Speech Extraction & Enrichment

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

YATSEE is a local-first, source-agnostic pipeline for turning raw meeting media into clean, auditable artifacts: formatted audio, transcripts, normalized text, civic summaries, deterministic meeting signals, and optional downstream search/index data.

The system is intentionally staged:

```text
supply source media
  ↓
format → transcribe → optional QA → slice → normalize → summarize / extract signals
  ↓
index → search / investigate
```

YATSEE core owns the processing middle of the workflow. Source acquisition, hosted upload flows, publication, and downstream investigation/search are adjacent concerns connected through local files or explicit handoff artifacts.

## Why This Exists

Public records are often public in name only. Civic business can be buried in long recordings, inconsistent transcripts, and procedural jargon. YATSEE creates durable local artifacts that make those records easier to inspect, summarize, search, and audit.

## Current Scope

YATSEE uses profile-driven intelligence workflows. The profile system is meant to support multiple artifact types through shared routing and validation rather than one-off processing paths. Individual profile quality and maturity are scoped deliberately; unfinished profiles should not block core pipeline, security, or packaging work.

The durable contract is:

- compatible media exists locally or is handed off as a local artifact
- each pipeline stage has explicit input and output artifacts
- entity-local config supplies names, titles, divisions, replacements, and context
- local or hosted LLM providers are selected through a hardened provider boundary
- generated summaries are useful review artifacts, not official records


## Demo

![Demo video](./docs/assets/yatsee_demo.gif)

---

## Documentation

- [YATSEE Pipeline Overview](./docs/yatsee_overview.md)
- [YATSEE User Guide](./docs/yatsee_user_guide.md)
- [YATSEE Configuration Guide](./docs/yatsee_config_guide.md)
- [YATSEE Prompt Orchestration](./docs/yatsee_prompt_orchestration.md)
- [YATSEE Troubleshooting](./docs/yatsee_troubleshooting.md)

---

## Command Families

YATSEE provides these command families:

```text
yatsee config ...
yatsee audio ...
yatsee transcript ...
yatsee intel ...
```

Common commands:

```bash
yatsee config entity list
yatsee config validate
yatsee config resolve --entity <entity>

yatsee audio format -e <entity>
yatsee audio transcribe -e <entity>

yatsee transcript slice -e <entity>
yatsee transcript normalize -e <entity>

yatsee intel summarize -e <entity>
yatsee intel signals -e <entity>
```


`yatsee intel summarize` is the canonical summary command. The older `yatsee intel run` command remains available as a compatibility alias.

The package can also be executed directly:

```bash
python -m yatsee --help
```

---

## Installation

### System tools

Required or commonly needed:

- `ffmpeg`

### Clone the repository

```bash
git clone https://github.com/YATSEE-Labs/YATSEE.git
cd yatsee
```

### Bootstrap a local environment

Linux/macOS:

```bash
./scripts/setup.sh
```

Windows PowerShell:

```powershell
.\scripts\setup.ps1
```

These setup scripts are convenience helpers for local development.

### Install directly from `pyproject.toml`

Create and activate a virtual environment.

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install in editable mode:

```bash
pip install --upgrade pip
pip install -e .
```

Install common optional functionality:

```bash
pip install -e .[full]
```

Or install only the extras you need:

```bash
pip install -e .[pipeline]
pip install -e .[index]
pip install -e .[ui]
pip install -e .[llamacpp]
```

---

## Requirements

Minimum practical requirements:

- **CPU:** modern 64-bit multi-core processor
- **RAM:** 16 GB recommended
- **Storage:** enough free disk space for raw media, derived audio, transcripts, summaries, and index data
- **Python:** 3.11 or newer
- **OS:** Linux or macOS recommended
- **Required tools:** `ffmpeg`

A GPU is not required, but transcription and local model execution are much slower on CPU-only systems.

Windows support is not a primary tested platform at this time.

---

## Quick Start

### 1. Create a local runtime config

Copy the example config to your local runtime config:

```bash
cp yatsee.conf yatsee.toml
```

Edit `yatsee.toml` for your environment and entities.

For the intelligence stage, configure at least:

```text
llm_provider
llm_provider_url
```

Provider hardening settings make trust boundaries explicit:

```text
llm_allow_loopback_http     # allow localhost/127.0.0.1 HTTP for local model servers
llm_allow_remote            # allow off-box provider targets
llm_allow_insecure_http     # allow non-loopback HTTP targets
llm_allow_custom_executable # allow non-default CLI provider executables
```

For hosted API keys, prefer an environment variable over config files or command-line flags:

```toml
llm_api_key_env = "OPENAI_API_KEY"
```

If omitted, provider hardening defaults to safer local-first behavior.

### 2. Inspect the CLI

```bash
yatsee --help
python -m yatsee --help
yatsee config --help
yatsee audio --help
yatsee transcript --help
yatsee intel --help
```

### 3. Validate configuration

```bash
yatsee config entity list
yatsee config validate
yatsee config resolve --entity <entity>
```

### 4. Add raw media

Place compatible audio or video files in the entity raw media directory:

```text
data/<entity>/downloads/
```

YATSEE core does not care how the files arrived. They may come from `yatsee-fetch`, recorder output, upload workflows, archival copies, or manual imports.

### 5. Process pipeline stages

```bash
yatsee audio format -e example_entity --dry-run
yatsee audio format -e example_entity
yatsee audio transcribe -e example_entity --faster
yatsee transcript slice -e example_entity --force
yatsee transcript normalize -e example_entity --force
yatsee intel summarize -e example_entity
yatsee intel signals -e example_entity
```

---

## Experimental Transcript QA Helpers

The normal quick-start pipeline does not require transcript QA. For long transcription batches, use the experimental helpers as a separate review/recovery workflow before slicing or summarization.

Inspect VTT quality and write a machine-readable report:

```bash
python scripts/qa_transcript_report.py --details
python scripts/qa_transcript_report.py --json qa_report.json
```

If transcripts are marked for rebuild, reset only those VTT artifacts and tracker entries. Start with a dry run:

```bash
python scripts/qa_reset_vtt_for_rebuild.py \
  --entity example_entity \
  --qa-report qa_report.json
```

Apply only after reviewing the matched files and tracker changes, then rebuild with the standard transcription defaults:

```bash
python scripts/qa_reset_vtt_for_rebuild.py \
  --entity example_entity \
  --qa-report qa_report.json \
  --apply

yatsee audio transcribe \
  -e example_entity \
  --faster \
  --get-chunks
```

The default transcription profile is the QA-safe baseline. These helpers remain experimental while transcript QA rules are being validated.

---

## Pipeline Model

YATSEE core owns the processing middle of the workflow:

```text
downloads/
  ↓
audio/
  ↓
transcripts_<model>/
  ↓
normalized/
  ↓
summary_<model>/ and meeting_signals/
```

Optional downstream tools can consume these artifacts for indexing, search, investigation, publication, or review.

## Configuration Model

YATSEE follows a layered configuration strategy:

1. load global `yatsee.toml`
2. load entity-local `data/<entity>/config.toml`
3. merge entity-local settings over global defaults
4. resolve paths, models, providers, prompts, and stage behavior
5. run the selected CLI stage

For intelligence-stage providers, YATSEE applies security policy by default:

- loopback HTTP is controlled by the narrow `llm_allow_loopback_http` flag
- off-box provider targets require `llm_allow_remote=true`
- non-loopback plain HTTP additionally requires `llm_allow_insecure_http=true`
- custom CLI executable targets require `llm_allow_custom_executable=true`

## Search and Indexing

YATSEE can be used with downstream search and indexing workflows built on top of normalized transcripts, summaries, segment artifacts, and signal artifacts.

Vector indexing and investigation/search are separate from the core pipeline. They consume YATSEE outputs rather than redefining the pipeline.

## License

YATSEE is open-source software licensed under the **GNU Affero General Public License v3.0 (AGPLv3)**.

Commercial licensing is available for proprietary or closed-source use. Contact admin <at> alias454 <dot> com.