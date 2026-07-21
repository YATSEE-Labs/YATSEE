# YATSEE

## Yet Another Tool for Speech Extraction & Enrichment

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

YATSEE is a local-first pipeline for turning long-form meeting media into clean, searchable, and auditable intelligence artifacts. Starting from compatible local media, it normalizes audio, transcribes speech, normalizes transcript text, and produces useful derived artifacts from otherwise unmanageable conversations.

Its outputs include normalized audio, timestamped transcripts, normalized text, high-signal summaries, and structured civic records. Each stage produces inspectable artifacts that can be reviewed, reused, or rerun without depending on a hosted AI service.

```text
local media
  → audio normalization
  → transcription
  → transcript slicing and normalization
  → summaries / records
  → optional indexing, search, or publication
```

YATSEE core begins with compatible local media. Source acquisition is handled separately by [`yatsee-fetch`](https://github.com/YATSEE-Labs/yatsee-fetch), which downloads media and prepares it for the YATSEE pipeline. Hosted uploads, publication, indexing, and search interfaces are also separate systems that exchange explicit files or artifact references with the core pipeline.

## Why This Exists

Public records are often public in name only. Important civic decisions are buried in multi-hour livestreams, inconsistent recordings, and jargon-heavy transcripts that may be technically accessible but remain functionally opaque.

Understanding what happened can require hours of listening just to identify a vote, contract, expenditure, appointment, project update, or policy debate.

YATSEE reduces that barrier. It combines local speech recognition, deterministic processing, and constrained language-model workflows to transform raw meeting media into searchable, evidence-backed information. Citizens, journalists, researchers, and watchdogs can review what happened, trace extracted records back to the source, and inspect the underlying artifacts for themselves.

YATSEE is built to make public proceedings meaningfully accessible, with or without assistance from the institutions that produced them.

![YATSEE demo](./docs/assets/yatsee_demo.gif)

## Intelligence outputs

| Command | Output | Processing model |
|---|---|---|
| `yatsee intel summarize` | Readable Markdown or YAML summary | LLM extraction, optional reductions, final synthesis |
| `yatsee intel records` | Structured JSON records and an itemized Markdown view | LLM extraction per chunk, deterministic consolidation and rendering |

Records and summaries are derived review artifacts, not official minutes. Records preserve transcript evidence and avoid an LLM consolidation pass, but the extracted facts still require source review when accuracy matters.

## Installation

Requirements:

- Python 3.11+
- `ffmpeg` and `ffprobe`
- enough storage for media and derived artifacts
- a configured local or hosted LLM provider for summaries and structured records
- an optional GPU for faster transcription and local model inference

```bash
git clone https://github.com/YATSEE-Labs/YATSEE.git
cd YATSEE

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .[full]
```

Convenience setup scripts are also available:

```bash
./scripts/setup.sh
```

```powershell
.\scripts\setup.ps1
```

## Quick start

Copy the example runtime configuration:

```bash
cp yatsee.conf yatsee.toml
```

Create and initialize an entity:

```bash
yatsee config entity add \
  --display-name "Example County Board" \
  --entity example_county_board \
  --base "country.US.state.EX."

yatsee config init -e example_county_board
yatsee config validate -e example_county_board
yatsee config resolve -e example_county_board
```

Place compatible media in the configured input directory, normally:

```text
data/example_county_board/downloads/
```

Summary and records generation require a configured LLM provider. Ollama provides the default local-first path, while compatible hosted providers may be configured explicitly.

Run the pipeline:

```bash
yatsee audio format -e example_county_board
yatsee audio transcribe -e example_county_board --faster
yatsee transcript slice -e example_county_board --force
yatsee transcript normalize -e example_county_board --force

yatsee intel summarize -e example_county_board
yatsee intel records -e example_county_board -j civic
```

Records artifacts are reused when the normalized transcript and extraction fingerprint have not changed. The deterministic Markdown view is regenerated from the cached JSON without invoking the model again.

Use `yatsee intel run` only for compatibility; it is an alias for `yatsee intel summarize`.

## Common command families

```text
yatsee config ...
yatsee audio ...
yatsee transcript ...
yatsee intel ...
```

Useful inspection commands:

```bash
yatsee --help
yatsee config resolve -e <entity>
yatsee intel prompts validate -j civic
yatsee intel summarize -e <entity> --print-prompts
yatsee intel records -e <entity> -j civic --print-prompts
```

## Prompt profiles

A profile keeps shared classification separate from artifact-specific routing:

```text
prompts/civic/
├── classifier.toml
├── prompts.toml
└── records_prompt.toml
```

- `classifier.toml` identifies the formal meeting type.
- `prompts.toml` routes summary `first`, `multi`, and `final` prompts.
- `records_prompt.toml` routes each meeting type to one extraction prompt.

Entity-local files under `data/<entity>/prompts/<profile>/` override project defaults.

The built-in civic profile supports summaries and records. The research profile currently supports summaries but does not include a records sidecar.

## Provider trust boundary

Local and hosted model providers are supported through one provider layer. Network and executable access remain explicit:

- loopback HTTP requires `llm_allow_loopback_http=true`
- off-box targets require `llm_allow_remote=true`
- non-loopback plain HTTP additionally requires `llm_allow_insecure_http=true`
- custom CLI executables require `llm_allow_custom_executable=true`

Prefer environment variables for API keys:

```toml
[system]
llm_api_key_env = "OPENAI_API_KEY"
```

## Documentation

Operator documentation:

- [Pipeline overview](./docs/yatsee_overview.md)
- [User guide](./docs/yatsee_user_guide.md)
- [Configuration guide](./docs/yatsee_config_guide.md)
- [Prompt orchestration](./docs/yatsee_prompt_orchestration.md)
- [Troubleshooting](./docs/yatsee_troubleshooting.md)
- [Transcription defaults](./docs/transcription-defaults.md)

Project direction:

- [Product touchstone](./docs/touchstone.md)
- [Input and distribution roadmap](./docs/input-and-distribution-roadmap.md)
- [Hosted architecture notes](./docs/saas_architecture.md)
- [Commercial strategy](./docs/commercial_strategy.md)
- [Traction notes](./docs/traction.md)

## Optional QA and downstream tools

Transcript QA helpers live under `scripts/`. Index and search prototypes live under `examples/`. These tools consume pipeline artifacts but are not required for the core media-to-artifact workflow.

## License

YATSEE is licensed under the GNU Affero General Public License v3.0. Commercial licensing is available for proprietary or closed-source use. Contact `admin <at> alias454 <dot> com`.