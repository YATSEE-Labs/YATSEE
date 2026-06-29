# YATSEE Configuration & Orchestration Guide

YATSEE uses layered configuration to resolve defaults, identify entities, select models, choose LLM providers, apply provider security policy, and process local media across the CLI.

Most configuration can stay at defaults. The parts that matter most are:

- entity registry
- entity-local settings
- media paths
- local divisions, titles, people, entities, and replacements
- model settings
- LLM provider settings
- provider security policy
- prompt profiles

YATSEE core is provider-neutral for source acquisition. It starts once compatible media exists locally.

---

# Table of Contents

- [Configuration Model](#configuration-model)
- [CLI Shape](#cli-shape)
- [New Entity Onboarding Workflow](#new-entity-onboarding-workflow)
- [Global System Configuration](#global-system-configuration)
- [Entity Registry](#entity-registry)
- [Entity-Local Configuration](#entity-local-configuration)
- [Entity Data Layout](#entity-data-layout)
- [Media Paths](#media-paths)
- [Divisions](#divisions)
- [Titles](#titles)
- [People](#people)
- [Entities](#entities)
- [Replacements](#replacements)
- [Validation and Resolution](#validation-and-resolution)
- [LLM Provider Configuration and Security](#llm-provider-configuration-and-security)
- [Pricing Reference Settings](#pricing-reference-settings)
- [Models](#models)
- [Prompt Profiles](#prompt-profiles)
- [Entity Configuration: What Usually Needs Editing](#entity-configuration-what-usually-needs-editing)
- [Operational Notes](#operational-notes)
- [Example: Add a County Board Entity](#example-add-a-county-board-entity)
- [Final Guidance](#final-guidance)

---

# Configuration Model

YATSEE follows this pattern:

1. Load global `yatsee.toml`
2. Load entity-local `data/<entity>/config.toml`
3. Merge entity-local settings over global defaults
4. Resolve paths, models, LLM providers, prompts, and stage behavior
5. Run the selected CLI stage

The two main configuration layers are:

```text
yatsee.toml
  global defaults
  entity registry
  LLM provider settings
  model settings
  pricing reference settings

data/<entity>/config.toml
  entity-specific processing settings
  local media path settings
  local people / titles / divisions / entities
  replacement rules
  notes and local metadata
```

The global file says what entities exist. The local file says how that entity should be processed.

Provider-specific source acquisition is outside YATSEE core. Tools such as `yatsee-fetch`, upload workflows, local recordings, or manual copies can place compatible media in the configured raw media directory.

---

# CLI Shape

YATSEE is organized as command families:

```text
yatsee config ...
yatsee audio ...
yatsee transcript ...
yatsee intel ...
```

The package also supports direct execution:

```bash
python -m yatsee --help
```

Current command families:

```bash
yatsee config entity list
yatsee config entity add
yatsee config entity remove
yatsee config entity purge
yatsee config init
yatsee config validate
yatsee config resolve

yatsee audio format
yatsee audio transcribe

yatsee transcript slice
yatsee transcript normalize

yatsee intel summarize
yatsee intel signals
```

The CLI modules are split by command group:

```text
src/yatsee/cli/
  main.py
  config.py
  audio.py
  transcript.py
  intel.py
```

`cli/main.py` owns root parser setup and top-level error handling. The group modules own their command registration and handlers.

---

# New Entity Onboarding Workflow

Creating a new entity is a two-step process:

1. Register the entity in global config
2. Scaffold the local entity directory and `config.toml`

Manual editing should happen after scaffolding.

## Step 1: List existing entities

```bash
yatsee config entity list
```

## Step 2: Add the entity to the global registry

```bash
yatsee config entity add \
  --display-name "Example County Board" \
  --entity example_county_board \
  --base "country.US.state.EX."
```

This creates a global registry entry similar to:

```toml
[entities.example_county_board]
display_name = "Example County Board"
base = "country.US.state.EX."
entity = "example_county_board"
```

## Step 3: Scaffold local entity config

```bash
yatsee config init --entity example_county_board
```

This creates:

```text
data/example_county_board/
└── config.toml
```

The scaffold command does not overwrite an existing `config.toml`.

## Step 4: Edit local config

```bash
nano data/example_county_board/config.toml
```

At minimum, update:

```toml
[settings]
entity_type = "county_board"
entity_level = "county"
location = "Example County, Example State"
notes = "Example County Board public meeting recordings."

[media]
input_dir = "downloads"
audio_dir = "audio"
```

Then refine:

```text
[divisions]
[titles]
[people]
[entities]
[replacements]
```

## Scaffold caveat

Some generated scaffolds may need manual correction for non-city civic bodies.

For county boards, make sure these are correct:

```toml
entity_type = "county_board"
entity_level = "county"
```

## Step 5: Validate and resolve

```bash
yatsee config validate --entity example_county_board
yatsee config resolve --entity example_county_board
```

## Step 6: Add raw media and run the pipeline

Place compatible raw media in:

```text
data/example_county_board/downloads/
```

Then process:

```bash
yatsee audio format -e example_county_board
yatsee audio transcribe -e example_county_board --faster
yatsee transcript slice -e example_county_board --force
yatsee transcript normalize -e example_county_board --force
yatsee intel summarize -e example_county_board
yatsee intel signals -e example_county_board
```

---

# Global System Configuration

The global config file is usually:

```text
yatsee.toml
```

It defines:

- root data directory
- logging defaults
- default models
- LLM provider defaults
- LLM provider security settings
- pricing reference settings
- entity registry

Example:

```toml
[system]
root_data_dir = "./data"
log_level = "INFO"
log_format = "%(asctime)s %(levelname)s %(name)s: %(message)s"

default_summarization_model = "mistral-nemo:latest"
default_transcription_model = "medium"
default_sentence_model = "en_core_web_sm"
default_embedding_model = "all-MiniLM-L6-v2"

llm_provider = "ollama"
llm_provider_url = "http://localhost:11434"
llm_api_key = ""
# Prefer env-based secrets for hosted providers.
# llm_api_key_env = "OPENAI_API_KEY"

llm_allow_remote = false
llm_allow_insecure_http = false
llm_allow_loopback_http = true
llm_allow_custom_executable = false

show_pricing = false
pricing_provider = "openai"
pricing_model = "gpt-5.4"
```

---

# Entity Registry

Entities are registered under:

```toml
[entities.<entity_handle>]
```

Example:

```toml
[entities.example_city_council]
display_name = "Example City Council"
base = "country.US.state.EX."
entity = "example_city_council"

[entities.example_county_board]
display_name = "Example County Board"
base = "country.US.state.EX."
entity = "example_county_board"
```

The `entity` value should match the registry key and local data directory.

---

# Entity-Local Configuration

Each entity has a local config:

```text
data/<entity>/config.toml
```

Example:

```toml
[settings]
entity_type = "county_board"
entity_level = "county"
location = "Example County, Example State"
data_path = "./data/example_county_board"
notes = "Example County Board public meeting recordings."
```

Common settings:

| Field | Purpose |
|---|---|
| `entity_type` | Organization or meeting body type. |
| `entity_level` | Jurisdiction or organization level. |
| `location` | Human-readable location. |
| `data_path` | Local data directory for the entity. |
| `notes` | Human-readable context notes. |

---

# Entity Data Layout

YATSEE stores working artifacts under the entity directory.

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
| `downloads/` | Raw media input. Often produced by acquisition tools. |
| `audio/` | Formatted transcription-ready audio. |
| `transcripts_<model>/` | VTT transcripts, sliced text, and optional segment JSONL for a transcription model. |
| `normalized/` | Cleaned transcript text. |
| `summary_<model>/` | Summary outputs for a summarization model. |
| `meeting_signals/` | Deterministic signal artifacts from normalized transcripts. |
| `yatsee_db/` | Optional or experimental index/search data. |

Model-specific directory names intentionally preserve provenance.

Temporary comparison directories may exist during testing:

```text
transcripts_medium_en/
summary_nemo_bak/
transcripts_medium_old/
```

These are comparison artifacts for validating updated models, prompts, normalizers, or pipeline behavior.

---

# Media Paths

YATSEE core does not care which external provider produced the raw media. It only needs compatible files.

Example:

```toml
[media]
input_dir = "downloads"
audio_dir = "audio"
```

`input_dir = "downloads"` resolves to:

```text
data/<entity>/downloads/
```

`audio_dir = "audio"` resolves to:

```text
data/<entity>/audio/
```

The CLI can override input per run:

```bash
yatsee audio format \
  -e example_county_board \
  --input-dir /path/to/raw/media
```

---

# Divisions

Use `[divisions]` for wards, districts, precincts, or similar structures.

```toml
[divisions]
type = "districts"
names = []
```

Start empty if you do not know the divisions yet.

---

# Titles

`[titles]` defines recurring role/title fragments. Use it for civic roles, offices, and staff titles that help prompts understand the local meeting context.

```toml
[titles]
board = ["County Board", "Board Member", "Chair", "Vice Chair"]
county_clerk = ["County Clerk", "Clerk"]
administration = ["County Administrator", "County Manager"]
staff = ["Director", "Coordinator", "Treasurer", "Sheriff", "State's Attorney", "Engineer", "Assessor"]
```

Use `[people]` for names and `[entities]` for contractors, organizations, places, vendors, and other non-person terms.

Operational note: transcription hotwords should come from people aliases and entity terms, not from generic title buckets. Broad title words such as `Mayor`, `Clerk`, `Alderman`, `Director`, or `Chief` can over-bias weak audio spans.

---

# People

`[people]` groups recurring people by role. These aliases are high-value transcription and normalization context.

```toml
[people.board_members]
Jane_Doe = ["Jane", "Doe"]
Sam_Smith = ["Sam", "Smith", "Samuel"]

[people.staff]
County_Clerk = ["Dovie", "Anderson"]

[people.legacy]
Former_Member = ["Former", "Member"]
```

Use `[people.legacy]` for former officials, former staff, or recurring historical names that may appear in older recordings but should not be represented as current officeholders.

Guidelines:

- use underscores in identifiers
- include first names, last names, nicknames, and common variants
- avoid title-heavy aliases such as `Mayor Jane Doe` unless they solve a proven recurring problem
- keep the list useful, not exhaustive

---

# Entities

`[entities]` groups non-person terms that should remain available as local context, such as contractors, organizations, agencies, vendors, recurring places, project names, or local institutions.

```toml
[entities]
third_parties = ["Fehr Graham", "Example Engineering"]
organizations = ["ComEd", "Example Library"]
places = ["Hancock Bridge", "Shawnee Street"]
```

Use `[entities]` instead of `[titles]` for contractors, companies, organizations, places, and other local terms that are not people or role titles.

---

# Replacements

`[replacements]` maps recurring ASR mistakes to corrected forms.

```toml
[replacements]
"Example Bad Transcription" = "Example Correct Term"
"Misheard Organization" = "Correct Organization"
```

Use replacements for repeated transcription errors, not every one-off mistake.

---

# Validation and Resolution

Validate global and local config:

```bash
yatsee config validate --entity example_county_board
```

Print resolved config:

```bash
yatsee config resolve --entity example_county_board
```

List entities:

```bash
yatsee config entity list
```

Remove from registry only:

```bash
yatsee config entity remove --entity example_county_board
```

Preview purge:

```bash
yatsee config entity purge \
  --entity example_county_board \
  --dry-run
```

Run purge:

```bash
yatsee config entity purge \
  --entity example_county_board
```

Use purge carefully. It removes both the registry entry and local filesystem data. Entity handles and purge paths are validated before deletion so a malformed handle or path traversal cannot escape the configured root data directory.

---

# LLM Provider Configuration and Security

YATSEE supports provider-based intelligence processing. This is separate from source acquisition providers.

LLM provider config lives in `[system]`.

```toml
[system]
llm_provider = "ollama"
llm_provider_url = "http://localhost:11434"
llm_api_key = ""
# Prefer env-based secrets for hosted providers.
# llm_api_key_env = "OPENAI_API_KEY"

llm_allow_remote = false
llm_allow_insecure_http = false
llm_allow_loopback_http = true
llm_allow_custom_executable = false
```

Provider examples:

```text
ollama
llamacpp
openai
anthropic
codex_cli
```

Provider hardening defaults:

- loopback HTTP is allowed only through `llm_allow_loopback_http`
- off-box provider targets require `llm_allow_remote=true`
- non-loopback plain HTTP additionally requires `llm_allow_insecure_http=true`
- custom executable targets require `llm_allow_custom_executable=true`

Common provider postures:

| Use case | Provider URL | Required flags |
|---|---|---|
| Local Ollama | `http://localhost:11434` | `llm_allow_loopback_http=true` |
| LAN Ollama | `http://192.168.x.x:11434` | `llm_allow_remote=true`, `llm_allow_insecure_http=true` |
| Hosted API | `https://...` | `llm_allow_remote=true` |
| CLI-backed provider | `codex` | default executable allowed; custom executable requires `llm_allow_custom_executable=true` |

---

# Pricing Reference Settings

YATSEE can estimate what a local run might have cost through a hosted provider.

```toml
show_pricing = true
pricing_provider = "openai"
pricing_model = "gpt-5.4"
```

This does not change the runtime provider.

---

# Models

Model runtime settings live under `[models]`.

```toml
[models."mistral-nemo:latest"]
append_dir = "summary_nemo"
max_tokens = 2500
num_ctx = 16384
```

`append_dir` controls the model-associated output directory. `max_tokens` and `num_ctx` control chunking and context behavior depending on stage use.

---

# Prompt Profiles

Prompt profiles live outside the main config and are selected during intelligence runs. Profiles should use the shared profile/routing mechanism rather than one-off processing paths.

```bash
yatsee intel summarize \
  -e example_entity \
  --job-profile civic
```

Validate prompt/profile wiring with:

```bash
yatsee intel prompts validate
yatsee intel prompts validate --entity example_entity
yatsee intel prompts validate --profile civic
yatsee intel prompts validate --all
```

Use `--all` when you intentionally want to surface every discovered profile, including unfinished or broken bundles.

---

# Entity Configuration: What Usually Needs Editing

Required or near-required:

```text
[settings]
[media]
```

High-value refinements:

```text
[divisions]
[titles]
[people]
[entities]
[replacements]
```

Usually leave alone unless needed:

```text
model overrides
LLM provider settings
pricing settings
```

Practical order:

1. Add entity
2. Init local config
3. Place raw media in the configured media input directory
4. Correct `entity_type`, `entity_level`, and `location`
5. Validate and resolve
6. Run a limited data batch
7. Review transcripts and summaries
8. Add replacements and local names
9. Rerun normalization, summaries, and signals as needed

---

# Operational Notes

- Keep entity handles stable.
- Use lowercase handles with underscores.
- Keep media paths minimal and explicit.
- Start with empty people/division lists if needed.
- Add replacements only for recurring mistakes.
- Validate before long runs.
- Use `config resolve` when behavior does not match expectations.
- Treat entity config as part of output quality.
- Do not tune prompts or LLMs before confirming transcript quality.

---

# Example: Add a County Board Entity

```bash
yatsee config entity add \
  --display-name "Example County Board" \
  --entity example_county_board \
  --base "country.US.state.EX."

yatsee config init --entity example_county_board
nano data/example_county_board/config.toml
```

Minimal local config target:

```toml
[settings]
entity_type = "county_board"
entity_level = "county"
location = "Example County, Example State"
data_path = "./data/example_county_board"
notes = "Example County Board public meeting recordings."

[media]
input_dir = "downloads"
audio_dir = "audio"

[divisions]
type = "districts"
names = []

[titles]
board = ["County Board", "Board Member", "Chair", "Vice Chair"]
county_clerk = ["County Clerk", "Clerk"]
administration = ["County Administrator", "County Manager"]
staff = ["Director", "Coordinator", "Treasurer", "Sheriff", "State's Attorney", "Engineer", "Assessor"]

[people.board_members]

[people.staff]

[people.legacy]

[entities]
third_parties = ["Consultant", "Engineer", "Auditor", "Applicant"]

[replacements]
"Common Bad Transcription" = "Correct Local Term"
```

Process:

```bash
yatsee config validate --entity example_county_board
yatsee config resolve --entity example_county_board
yatsee audio format -e example_county_board
yatsee audio transcribe -e example_county_board --faster
yatsee transcript slice -e example_county_board --force
yatsee transcript normalize -e example_county_board --force
yatsee intel summarize -e example_county_board
yatsee intel signals -e example_county_board
```

Inspect outputs:

```bash
find data/example_county_board/transcripts_medium -name '*.vtt' | wc -l
find data/example_county_board/normalized -name '*.txt' | wc -l
find data/example_county_board/summary_nemo -name '*.md' | wc -l
find data/example_county_board/meeting_signals -name '*.signals.md' | wc -l
```

---

# Final Guidance

Most users do not need to tune every field.

The highest-value work is:

- define the entity clearly
- place raw media in the configured input directory
- validate before long runs
- keep local provider security defaults intact
- add real local names, legacy people, entities, and roles over time
- add replacements for recurring transcript errors
- confirm transcript quality before relying on summaries

YATSEE works best when the boring layers are solid:

```text
raw media
audio
VTT
plain text
normalized text
summary / signals
search / retrieval / publishing
```