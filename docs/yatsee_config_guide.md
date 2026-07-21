# YATSEE Configuration Guide

YATSEE uses layered TOML configuration for entities, paths, models, providers, security policy, and output behavior.

## Load order

```text
yatsee.toml
  + data/<entity>/config.toml
  → merged runtime configuration
  → stage-specific resolution
```

Entity-local values override global defaults. Prompt files use a separate profile lookup described below.

## Configuration commands

```bash
yatsee config entity list
yatsee config entity add --display-name "Example Board" --entity example_board
yatsee config init -e example_board
yatsee config validate -e example_board
yatsee config resolve -e example_board
```

Resolved config is redacted by default. `--show-secrets` should be used only when the terminal output is safe to expose.

Registry-only removal and full purge are different operations:

```bash
yatsee config entity remove -e example_board
yatsee config entity purge -e example_board --dry-run
yatsee config entity purge -e example_board
```

Always preview purge operations.

## Global configuration

The global file is normally `yatsee.toml`.

```toml
[system]
root_data_dir = "./data"
log_level = "INFO"

default_transcription_model = "medium"
default_summarization_model = "gemma4:e4b"
default_records_model = "gemma4:e4b"
default_sentence_model = "en_core_web_sm"
default_embedding_model = "all-MiniLM-L6-v2"

llm_provider = "ollama"
llm_provider_url = "http://localhost:11434"
llm_api_key_env = "OPENAI_API_KEY"

llm_allow_loopback_http = true
llm_allow_remote = false
llm_allow_insecure_http = false
llm_allow_custom_executable = false

show_pricing = false
pricing_provider = "openai"
pricing_model = "gpt-5.4"
```

Only configure keys relevant to the providers and stages you use.

## Entity registry

```toml
[entities.example_board]
display_name = "Example County Board"
base = "country.US.state.EX."
entity = "example_board"
```

The registry key, `entity` value, and data directory should stay aligned. Use lowercase handles with underscores.

## Entity-local configuration

Expected location:

```text
data/<entity>/config.toml
```

Representative configuration:

```toml
[settings]
entity_type = "county_board"
entity_level = "county"
location = "Example County, Example State"
data_path = "./data/example_board"
notes = "Public meeting recordings."

[media]
input_dir = "downloads"
audio_dir = "audio"

[divisions]
type = "districts"
names = []

[titles]
board = ["County Board", "Board Member", "Chair"]
staff = ["Administrator", "Clerk", "Director"]

[people.board_members]
Jane_Doe = ["Jane", "Doe"]

[people.legacy]
Former_Member = ["Former", "Member"]

[entities]
organizations = ["Example Library", "Example Engineering"]
places = ["Main Street", "Hancock Bridge"]

[replacements]
"Recurring ASR Error" = "Correct Local Term"
```

Use these sections deliberately:

| Section | Purpose |
|---|---|
| `[settings]` | Entity identity, location, data path, notes |
| `[media]` | Raw media and audio directory names |
| `[divisions]` | Wards, districts, precincts, or similar structures |
| `[titles]` | Recurring roles, not people or organizations |
| `[people]` | Current known participants and aliases |
| `[people.legacy]` | Former or historical participants |
| `[entities]` | Organizations, vendors, projects, and places |
| `[replacements]` | Proven recurring ASR corrections |

Avoid bloated hotword and replacement lists. Broad titles and one-off errors can reduce transcription quality.

## Model configuration

Models are declared by exact configured name:

```toml
[models."gemma4:e4b"]
append_dir = "summary_gemma4_e4b"
max_tokens = 2500
records_append_dir = "records/gemma4_e4b"
records_max_tokens = 1500
num_ctx = 16384
```

Relevant fields:

| Field | Used by | Meaning |
|---|---|---|
| `append_dir` | Summary | Entity-relative summary output directory |
| `max_tokens` | Summary | Default source chunk token budget |
| `records_append_dir` | Records | Optional entity-relative records output directory |
| `records_max_tokens` | Records | Default records source chunk budget |
| `num_ctx` | Both | Provider model context setting and chunk-size guard |

If `records_append_dir` is absent, records default to `records/<sanitized-model-name>`.

### Summary model precedence

```text
--model
→ entity summarization_model
→ system default_summarization_model
```

### Records model precedence

```text
--model
→ entity records_model
→ entity summarization_model
→ system default_records_model
→ system default_summarization_model
```

Records can therefore start with the summary model configuration and later move to a dedicated model without changing the command contract.

## Provider configuration and security

Supported provider names include:

```text
ollama
llamacpp
openai
anthropic
codex_cli
```

Provider access is a trust boundary:

| Target | Required setting |
|---|---|
| Local `http://localhost` or `127.0.0.1` | `llm_allow_loopback_http=true` |
| Remote HTTPS | `llm_allow_remote=true` |
| Remote plain HTTP | `llm_allow_remote=true` and `llm_allow_insecure_http=true` |
| Custom CLI executable | `llm_allow_custom_executable=true` |

Do not relax provider policy only to suppress an error. Confirm the target and transport first.

Prefer environment variables for secrets:

```toml
[system]
llm_api_key_env = "OPENAI_API_KEY"
```

The CLI accepts `--llm-api-key`, but shell history and process listings make it a weaker choice.

## Pricing reference

```toml
[system]
show_pricing = true
pricing_provider = "openai"
pricing_model = "gpt-5.4"
```

Reference pricing estimates hosted-provider cost from reported token usage. It does not measure local electricity, hardware, or runtime cost and does not change the active provider.

## Prompt profiles

Project profiles:

```text
prompts/<profile>/
├── classifier.toml
├── prompts.toml
└── records_prompt.toml
```

Entity-local overrides:

```text
data/<entity>/prompts/<profile>/
├── classifier.toml
├── prompts.toml
└── records_prompt.toml
```

Each file resolves independently using entity-local then project precedence. An entity may override only the file it needs.

- `classifier.toml` is shared by summary and records.
- `prompts.toml` owns summary routing and optional density keywords.
- `records_prompt.toml` owns records extraction routing.

The summary and classifier loaders have built-in fallbacks for non-strict runtime use. Records intentionally has no fallback and fails if its sidecar is missing.

Validate classifier and summary wiring:

```bash
yatsee intel prompts validate -j civic
yatsee intel prompts validate -e example_board -j civic
yatsee intel prompts validate --all
```

Load and inspect records routing:

```bash
yatsee intel records -e example_board -j civic --print-prompts
```

## Path behavior

Entity-backed intelligence runs default to:

```text
input:  data/<entity>/normalized/
summary output: configured model append_dir
records output: configured records_append_dir or records/<model>/
```

Without an entity, commands require explicit input and output paths. Paths used for destructive or generated operations are resolved and checked through shared path helpers.

## Practical configuration workflow

1. Register and initialize the entity.
2. Correct identity, `data_path`, and media paths.
3. Validate and inspect resolved configuration.
4. Process a small sample.
5. Review audio and transcripts.
6. Add only proven local names, entities, and replacements.
7. Configure summary and records models.
8. Validate prompt profiles.
9. Run larger batches.

Configuration should clarify behavior, not become a catalog of every observed person or transcription mistake.
