# YATSEE Prompt Orchestration

YATSEE separates shared meeting classification from artifact-specific prompt routing.

## Profile layout

```text
prompts/<profile>/
├── classifier.toml
├── prompts.toml
└── records_prompt.toml
```

| File | Used by | Contract |
|---|---|---|
| `classifier.toml` | Summary and records | Allowed labels plus a classifier prompt when multiple labels exist |
| `prompts.toml` | Summary | Meeting type to `first`, `multi`, and `final` prompt IDs |
| `records_prompt.toml` | Records | Meeting type to one extraction prompt ID |

## Resolution order

For each profile file, YATSEE checks:

1. `data/<entity>/prompts/<profile>/<filename>`
2. `prompts/<profile>/<filename>`
3. a built-in fallback only where the loader supports one

The files resolve independently. An entity can override records prompts without copying the classifier or summary bundle.

Profile names are restricted to simple filesystem-safe components.

## Classifier contract

`classifier.toml` owns formal meeting-type classification:

```toml
[classifier_types]
allowed = [
  "city_council",
  "finance_committee",
  "committee_of_the_whole",
  "zoning_committee",
  "general",
]

[classifier_prompt]
text = """
Context: {context}
Transcript Snippet: {text}
...return one allowed label...
"""
```

The classifier should identify the meeting body, not the subject matter. A Committee of the Whole meeting remains `committee_of_the_whole` when it discusses budgets or zoning.

A profile with exactly one allowed type is deterministic and may omit `[classifier_prompt]` entirely:

```toml
[classifier_types]
allowed = ["general"]
```

YATSEE selects that sole type without calling the model or charging classifier token usage. Profiles with multiple allowed types must provide a classifier prompt containing both `{context}` and `{text}`.

Multi-label classification uses filename-derived context plus an initial transcript snippet. Disable or override it when the meeting type is already known:

```bash
yatsee intel summarize -e <entity> --disable-auto-classification
yatsee intel records -e <entity> --meeting-type finance_committee
```

## Summary routing

`prompts.toml` maps each meeting type to three roles:

```toml
[prompt_router]
city_council = {
  first = "city_council_first_pass",
  multi = "structure_preserving",
  final = "civic_scan",
}

general = {
  first = "general_first_pass",
  multi = "structure_preserving",
  final = "final_pass_detailed",
}
```

Prompt text lives under `[prompts.<prompt_id>]`.

Summary flow:

```text
classify
→ first-pass extraction over transcript chunks
→ multi-pass reduction only while intermediate text remains too large
→ final LLM synthesis
```

`--max-pass` caps the summary workflow. `--first-prompt`, `--second-prompt`, and `--final-prompt` override route selections for one run.

Optional `[density_keywords]` configuration is shared by summary pass one and records. Both workflows use it when `--chunk-style density` is selected.

## Records routing

`records_prompt.toml` maps each meeting type directly to one prompt:

```toml
[records_router]
city_council = "city_council_records"
finance_committee = "finance_committee_records"
committee_of_the_whole = "committee_of_the_whole_records"
zoning_committee = "zoning_committee_records"
general = "general_records"
```

Records flow:

```text
classify
→ select one extraction prompt
→ run it once per transcript chunk
→ parse compact JSONL and validate each record
→ deterministically consolidate exact duplicates
```

There is no records reduction prompt and no LLM final pass.

### Records JSONL contract

The preferred chunk response is compact JSONL with one complete record object per line. Do not wrap the records in an array or add commas between lines. Return exactly `[]` when the chunk contains no supported records.

```json
{"record_type":"motion","subject":"Approval of agenda","summary":"A motion was made to approve the agenda.","evidence":"Motion made by Alderman Sellers, seconded by Alderman Shadle.","attribution":"Alderman Sellers","attributes":{"seconded_by":"Alderman Shadle"}}
```

The parser still accepts a direct JSON array or a legacy `{"records": [...]}` wrapper, but prompt authors should target JSONL because complete lines survive a truncated final response.

Allowed `record_type` values:

```text
attendance
procedural_action
presentation
discussion
motion
vote
public_comment
financial
metric
ordinance
resolution
contract
project
appointment
proclamation
recognition
follow_up
other
```

Unexpected record fields are retained under `attributes`. Empty optional values are removed. A malformed JSONL line or invalid individual record is reported without discarding valid sibling lines, and that chunk is marked partial. An empty response, provider failure, or invalid legacy response root marks the chunk failed. Later chunks still run, and any partial or failed chunk causes the transcript attempt to be written as partial.

### Deterministic consolidation

The consolidator:

- preserves chunk and record order;
- merges only exact normalized duplicate records;
- records every contributing `source_chunks` value;
- leaves similar, contradictory, or uncertain records separate;
- does not infer missing data or resolve conflicts.

This boundary keeps probabilistic extraction separate from deterministic artifact handling.

## Chunking

Summary and records support:

```text
word
sentence
density
```

Records defaults to density-aware chunking. The source token budget resolves from `--max-tokens`, then model `records_max_tokens`, then model `max_tokens`, with a final 2,500-token fallback. The included `gemma4:e4b` configuration uses a 1,500-token records budget. Density mode uses the same profile density keywords and pass-one threshold as summary.

## Providers

The prompt workflow is provider-neutral. Supported provider adapters include Ollama, llama.cpp, OpenAI, Anthropic, and a controlled CLI provider. Provider security policy remains config-driven even when provider or model values are overridden on the command line.

## Validation and inspection

Validate filesystem-backed classifier and summary bundles:

```bash
yatsee intel prompts validate -j civic
yatsee intel prompts validate -e <entity> -j civic
yatsee intel prompts validate --all
```

This command currently validates classifier and summary routing. Load and inspect records routing separately:

```bash
yatsee intel records -e <entity> -j civic --print-prompts
```

Inspect summary prompts and intermediate output:

```bash
yatsee intel summarize -e <entity> --print-prompts
yatsee intel summarize -e <entity> --enable-chunk-writer
```

Inspect records artifacts under the transcript-specific `raw/`, `chunks/`, and `errors/` directories.

## Built-in profile status

| Profile | Classifier | Summary | Records |
|---|---:|---:|---:|
| `civic` | Deterministic `general` | Yes | Yes |
| `research` | Multi-label | Yes | No sidecar yet |

A missing records sidecar fails clearly rather than silently falling back to summary prompts.
