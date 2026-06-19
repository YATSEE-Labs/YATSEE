# YATSEE: Configuration & Orchestration Guide

YATSEE uses a layered configuration model to control how the system resolves defaults, identifies entities, selects models, chooses providers, applies provider security policy, and processes raw media across the CLI.

Most configuration can stay at sensible defaults. The parts that matter most are the sections that define:

- entities
- raw media locations
- local names and roles
- divisions
- provider settings
- model settings
- security policy
- recurring transcript cleanup rules

If these fields stay generic, YATSEE stays generic. When you fill them in with real local data, YATSEE has better context for identifying people, places, organizations, recurring topics, and transcript issues across downstream artifacts.

---

# Table of Contents

- [Configuration Model](#configuration-model)
- [New Entity Onboarding Workflow](#new-entity-onboarding-workflow)
- [Global System Configuration](#global-system-configuration)
- [Entity Registry](#entity-registry)
- [Entity-Local Configuration](#entity-local-configuration)
- [Media Paths](#media-paths)
- [Divisions](#divisions)
- [Titles](#titles)
- [People](#people)
- [Replacements](#replacements)
- [Validation and Resolution](#validation-and-resolution)
- [Provider Configuration and Security](#provider-configuration-and-security)
- [Pricing Reference Settings](#pricing-reference-settings)
- [Models](#models)
- [Prompt Profiles](#prompt-profiles)
- [Entity Configuration: What Usually Needs Editing](#entity-configuration-what-usually-needs-editing)
- [Operational Notes](#operational-notes)
- [Example: Add a County Board Entity](#example-add-a-county-board-entity)
- [Final Guidance](#final-guidance)

---

# Configuration Model

YATSEE follows this general pattern:

1. Load global `yatsee.toml`
2. Load entity-local `config.toml`
3. Merge entity-local settings over global defaults
4. Resolve paths, models, providers, and stage behavior
5. Run the selected CLI stage

The two main configuration layers are:

```text
yatsee.toml
  global defaults
  entity registry
  provider settings
  model settings

data/<entity>/config.toml
  entity-specific processing settings
  raw media path settings
  local people / titles / divisions
  replacement rules
  notes and local metadata
```

The global file says **what entities exist**.

The local file says **how that entity should be processed**.

YATSEE's primary command families are:

```bash
yatsee config ...
yatsee audio format ...
yatsee audio transcribe ...
yatsee transcript slice ...
yatsee transcript normalize ...
yatsee intel run ...
```

Provider-specific acquisition is outside the YATSEE core CLI. External tools, upload workflows, local recordings, or manual copies can place compatible media in the configured raw media directory before YATSEE begins processing.

---

# New Entity Onboarding Workflow

Creating a new entity is a two-step process:

1. Register the entity in the global config
2. Scaffold the local entity directory and `config.toml`

Manual editing should happen after scaffolding, not before.

## Step 1: List existing entities

```bash
yatsee config entity list
```

## Step 2: Add the entity to the global registry

Use `config entity add`:

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

## What `base` means

The `base` value is a logical classification path.

For example:

```toml
base = "country.US.state.EX."
```

means:

```text
country → US → state → EX
```

County-specific or organization-specific meaning belongs in the entity-local config:

```toml
[settings]
entity_type = "county_board"
entity_level = "county"
location = "Example County, Example State"
```

## Step 3: Scaffold the local entity config

After the entity is registered, initialize the entity structure:

```bash
yatsee config init --entity example_county_board
```

This creates:

```text
data/example_county_board/
└── config.toml
```

The scaffold command does not overwrite an existing `config.toml`.

It also does not modify the global registry. The registry is managed by `yatsee config entity add`, `remove`, and `purge`.

## Step 4: Edit the local config

Open the generated local config:

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

Then refine these sections as needed:

```text
[divisions]
[titles]
[people]
[replacements]
```

## Current scaffold caveat

The scaffold currently treats `city_council` and `county_board` entity handles as civic entities, but county-board scaffolds may still default to:

```toml
entity_type = "city_council"
entity_level = "city"
```

For county boards, manually correct those values:

```toml
entity_type = "county_board"
entity_level = "county"
```

This is a scaffold limitation, not a runtime requirement.

## Step 5: Validate and resolve

Run validation:

```bash
yatsee config validate --entity example_county_board
```

Inspect the merged runtime config:

```bash
yatsee config resolve --entity example_county_board
```

Use `resolve` before running pipeline stages. It shows the final merged configuration YATSEE will actually use.

## Step 6: Add raw media and run the pipeline

Place compatible raw media in:

```text
data/example_county_board/downloads/
```

or point the formatter at another directory:

```bash
yatsee audio format \
  -e example_county_board \
  --input-dir /path/to/raw/media
```

Then continue through the normal pipeline:

```bash
yatsee audio format -e example_county_board
yatsee audio transcribe -e example_county_board --faster
yatsee transcript slice -e example_county_board --force
yatsee transcript normalize -e example_county_board --force
yatsee intel run -e example_county_board
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
- provider defaults
- provider security settings
- pricing reference settings
- default models
- entity registry

## `[system]`

The `[system]` block controls global runtime behavior.

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

llm_allow_remote = false
llm_allow_insecure_http = false
llm_allow_custom_executable = false

show_pricing = false
pricing_provider = "openai"
pricing_model = "gpt-5.4"
```

## `root_data_dir`

Base directory where entity data is stored.

Typical value:

```toml
root_data_dir = "./data"
```

Generated entity data usually lands at:

```text
data/<entity>/
```

## `log_level`

Controls verbosity.

Typical values:

```text
DEBUG
INFO
WARNING
ERROR
```

Use `DEBUG` only while troubleshooting.

## Default model settings

These define fallback model choices:

```toml
default_summarization_model = "mistral-nemo:latest"
default_transcription_model = "medium"
default_sentence_model = "en_core_web_sm"
default_embedding_model = "all-MiniLM-L6-v2"
```

Entity-local configs may override these.

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

## `display_name`

Human-readable name.

Example:

```toml
display_name = "Example County Board"
```

## `base`

Logical classification namespace.

Example:

```toml
base = "country.US.state.EX."
```

## `entity`

Canonical handle.

Example:

```toml
entity = "example_county_board"
```

This should match the registry key and local data directory.

---

# Entity-Local Configuration

Each entity has a local config:

```text
data/<entity>/config.toml
```

This controls entity-specific behavior.

Example path:

```text
data/example_county_board/config.toml
```

## `[settings]`

Example:

```toml
[settings]
entity_type = "county_board"
entity_level = "county"
location = "Example County, Example State"
data_path = "./data/example_county_board"
notes = "Example County Board public meeting recordings."
```

## `entity_type`

Describes the organization or meeting body.

Examples:

```toml
entity_type = "city_council"
entity_type = "county_board"
entity_type = "school_board"
entity_type = "planning_commission"
entity_type = "library_board"
```

## `entity_level`

Describes jurisdiction or organizational level.

Examples:

```toml
entity_level = "city"
entity_level = "county"
entity_level = "district"
entity_level = "organization"
```

## `location`

Human-readable location.

Example:

```toml
location = "Example County, Example State"
```

## `data_path`

Output directory for that entity.

Example:

```toml
data_path = "./data/example_county_board"
```

Usually this should match the generated scaffold.

## Model overrides

The scaffold may include commented model override examples.

Uncomment only when needed:

```toml
summarization_model = "mistral-nemo:latest"
transcription_model = "medium"
sentence_model = "en_core_web_sm"
embedding_model = "all-MiniLM-L6-v2"
```

---

# Media Paths

YATSEE core does not care which external provider produced the raw media. It only needs to know where compatible files are located.

## `[media]`

Example:

```toml
[media]
input_dir = "downloads"
audio_dir = "audio"
```

## `input_dir`

Provider-neutral raw media input directory.

Relative paths are resolved below the entity `data_path`.

Example:

```toml
input_dir = "downloads"
```

means:

```text
data/<entity>/downloads/
```

An absolute path can be used when raw media is stored elsewhere:

```toml
input_dir = "/mnt/media/example_county_board"
```

The CLI can override this per run:

```bash
yatsee audio format \
  -e example_county_board \
  --input-dir /path/to/raw/media
```

## `audio_dir`

Normalized audio output directory.

Relative paths are resolved below the entity `data_path`.

Example:

```toml
audio_dir = "audio"
```

means:

```text
data/<entity>/audio/
```

---

# Divisions

Use `[divisions]` for wards, districts, precincts, or similar structures.

City example:

```toml
[divisions]
type = "wards"
names = [
  "1st Ward",
  "2nd Ward",
  "3rd Ward",
  "4th Ward",
  "5th Ward"
]
```

County example:

```toml
[divisions]
type = "districts"
names = []
```

For a new entity, start empty if you do not know the districts yet.

---

# Titles

`[titles]` defines role/title fragments that appear in transcripts.

These are role terms, not full names.

Good:

```toml
[titles]
board = ["County Board", "Board Member", "Chair", "Vice Chair"]
county_clerk = ["County Clerk", "Clerk"]
administration = ["County Administrator", "County Manager"]
staff = ["Director", "Coordinator", "Treasurer", "Sheriff", "State's Attorney", "Engineer", "Assessor"]
third_parties = ["Consultant", "Engineer", "Auditor", "Applicant"]
```

Avoid stuffing long full-name phrases into titles.

---

# People

`[people]` groups recurring people by role.

Use stable identifiers and name fragments.

Example:

```toml
[people.board_members]
Jane_Doe = ["Jane", "Doe"]
Sam_Smith = ["Sam", "Smith", "Samuel"]

[people.staff]
County_Clerk = ["Clerk", "County Clerk"]
```

Guidelines:

- use underscores in identifiers
- include first names, last names, nicknames, and common variants
- avoid long phrases unless they solve a specific recurring transcription problem
- keep the list useful, not exhaustive

---

# Replacements

`[replacements]` maps recurring ASR mistakes to corrected forms.

Example:

```toml
[replacements]
"Example Bad Transcription" = "Example Correct Term"
"Misheard Organization" = "Correct Organization"
```

Use replacements for repeated transcription errors. Do not try to fix every one-off mistake.

---

# Validation and Resolution

## Validate global and local config

```bash
yatsee config validate --entity example_county_board
```

This checks global config and the selected entity config.

## Print resolved config

```bash
yatsee config resolve --entity example_county_board
```

Use this when troubleshooting path, model, or provider behavior.

## List registered entities

```bash
yatsee config entity list
```

## Remove from registry only

```bash
yatsee config entity remove --entity example_county_board
```

This removes the registry entry but does not remove local files.

## Purge registry and filesystem

Preview first:

```bash
yatsee config entity purge \
  --entity example_county_board \
  --dry-run
```

Then purge:

```bash
yatsee config entity purge \
  --entity example_county_board
```

Use purge carefully. It is meant for removing an entity and its local filesystem data.

---

# Provider Configuration and Security

YATSEE supports provider-based intelligence processing.

Provider config lives in `[system]`.

Example:

```toml
[system]
llm_provider = "ollama"
llm_provider_url = "http://localhost:11434"
llm_api_key = ""

llm_allow_remote = false
llm_allow_insecure_http = false
llm_allow_custom_executable = false
```

## `llm_provider`

Provider backend.

Examples:

```text
ollama
llamacpp
openai
anthropic
codex_cli
```

## `llm_provider_url`

Provider target.

Examples:

```toml
llm_provider_url = "http://localhost:11434"
llm_provider_url = "http://localhost:8080"
llm_provider_url = "https://api.openai.com"
llm_provider_url = "codex"
```

## `llm_api_key`

API key for hosted providers.

Usually empty for local providers.

## Provider hardening defaults

These default to safe behavior when omitted:

```toml
llm_allow_remote = false
llm_allow_insecure_http = false
llm_allow_custom_executable = false
```

Meaning:

- non-local local-runtime targets are blocked unless explicitly allowed
- insecure hosted HTTP is blocked unless explicitly allowed
- custom executable targets are blocked unless explicitly allowed

This protects local-first workflows from accidentally drifting into remote or unsafe execution.

---

# Pricing Reference Settings

YATSEE can estimate what a local run might have cost through a hosted provider.

Example:

```toml
show_pricing = true
pricing_provider = "openai"
pricing_model = "gpt-5.4"
```

This does not change the runtime provider.

Example:

```toml
llm_provider = "ollama"
llm_provider_url = "http://localhost:11434"

show_pricing = true
pricing_provider = "openai"
pricing_model = "gpt-5.4"
```

In this setup:

- YATSEE runs locally through Ollama
- pricing is estimated using OpenAI reference pricing
- no hosted generation occurs unless the runtime provider is changed

---

# Models

Model runtime settings live under `[models]`.

Example:

```toml
[models."mistral-nemo:latest"]
append_dir = "summary_nemo"
max_tokens = 2500
num_ctx = 16384
```

Common fields:

## `append_dir`

Output directory suffix for model-associated outputs.

## `max_tokens`

Target chunk size or generation budget depending on stage behavior.

## `num_ctx`

Model context window.

Keep `max_tokens` below `num_ctx` to leave room for prompts and output.

---

# Prompt Profiles

Prompt profiles live outside the main config and are selected during intelligence runs.

Common profiles:

```text
civic
research
```

CLI flag:

```bash
yatsee intel run \
  -e example_entity \
  --job-profile civic
```

Prompt routing controls which prompt templates are used for different meeting types.

---

# Entity Configuration: What Usually Needs Editing

For a new civic entity, these sections usually need attention:

## Required or near-required

```text
[settings]
[media]
```

## High-value refinements

```text
[divisions]
[titles]
[people]
[replacements]
```

## Usually leave alone unless needed

```text
model overrides
provider settings
pricing settings
```

## Practical order

1. Add entity
2. Init local config
3. Place raw media in the configured media input directory
4. Correct `entity_type`, `entity_level`, and `location`
5. Validate
6. Run a limited data batch
7. Review transcripts and summaries
8. Add replacements and local names
9. Rerun normalization and summary stages

---

# Operational Notes

- Keep entity handles stable.
- Use lowercase handles with underscores.
- Keep media paths minimal and explicit.
- Start with empty people/division lists if you do not know them yet.
- Add replacements only for recurring mistakes.
- Validate before long runs.
- Use `config resolve` when behavior does not match expectations.
- Treat entity config as part of output quality.
- Do not tune prompts or LLMs before confirming transcript quality.

---

# Example: Add a County Board Entity

## Add registry entry

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

## Edit local config

```bash
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
third_parties = ["Consultant", "Engineer", "Auditor", "Applicant"]

[people.board_members]

[people.staff]

[people.third_parties]

[replacements]
"Common Bad Transcription" = "Correct Local Term"
```

## Add raw media

Place compatible media in:

```text
data/example_county_board/downloads/
```

or use `--input-dir` when formatting.

## Validate

```bash
yatsee config validate --entity example_county_board
yatsee config resolve --entity example_county_board
```

## Process

```bash
yatsee audio format -e example_county_board
yatsee audio transcribe -e example_county_board --faster
yatsee transcript slice -e example_county_board --force
yatsee transcript normalize -e example_county_board --force
yatsee intel run -e example_county_board
```

## Inspect outputs

```bash
find data/example_county_board/transcripts_medium -name '*.vtt' | wc -l
find data/example_county_board/normalized -name '*.txt' | wc -l
find data/example_county_board/summary -name '*.md' | wc -l
```

Spot-check one normalized transcript:

```bash
sed -n '1,220p' data/example_county_board/normalized/<file>.txt
```

---

# Final Guidance

Most users do not need to tune every field.

The highest-value work is:

- define the entity clearly
- place raw media in the configured input directory
- validate before long runs
- keep local provider security defaults intact
- add real local names and roles over time
- add replacements for recurring transcript errors
- confirm transcript quality before relying on summaries

YATSEE works best when the boring layers are solid:

```text
raw media
audio
VTT
plain text
normalized text
summary
search / retrieval / publishing
```

LLM summaries are optional enhancement layers. The transcript and normalized text artifacts should still be useful without them.