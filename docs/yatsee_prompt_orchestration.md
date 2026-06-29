# YATSEE Prompt Orchestration Overview

This guide explains how YATSEE manages the LLM-backed intelligence layer.

Instead of sending a full transcript to a model and hoping for a usable result, YATSEE uses layered prompt resolution, optional meeting classification, chunking, and multi-pass summarization.

Prompt orchestration applies to:

```bash
yatsee intel summarize
```

`yatsee intel run` remains available as a compatibility alias.

It does not apply to deterministic signal extraction:

```bash
yatsee intel signals
```

Signals are mechanical extraction artifacts and do not require LLM prompt routing.

---

## 1. Prompt Hierarchy

YATSEE resolves prompt instructions in layered order.

Priority order:

1. **Entity-specific prompts**  
   `data/<entity>/prompts/<job_profile>/prompts.toml`

2. **Global job defaults**  
   `prompts/<job_profile>/prompts.toml`

3. **System fallbacks**  
   Inline fallback prompts in the Python pipeline, when available

This allows global defaults, job-level customization, and entity-specific overrides to coexist.

## Prompt override layout

```text
./prompts/
  └── civic/
      └── prompts.toml

./data/
  └── defined_entity/
      └── prompts/
          └── civic/
              └── prompts.toml

./data/
  └── generic_entity/
      └── prompts/
          └── civic/
              # no file, falls back to global prompts
```

Behavior:

- YATSEE first checks `data/<entity>/prompts/<job_profile>/prompts.toml`
- if found, that file overrides the global job prompts
- if not found, YATSEE falls back to `prompts/<job_profile>/prompts.toml`
- if no prompt file exists, inline fallback prompts may be used depending on the stage

---

## 2. Job Profiles

The intelligence stage is driven by the `--job-profile` argument. A profile defines the prompt bundle and routing used for a particular artifact workflow.

Profiles are intentionally extensible. New artifact workflows should be added through profile directories and shared routing/validation mechanics rather than custom code paths.

Example:

```bash
yatsee intel summarize \
  -e example_entity \
  --job-profile civic
```

A job profile defines:

- prompt file location
- extraction expectations
- routing behavior
- final output shape
- task-specific style and structure

New specialized tasks can be introduced by adding a new prompt profile directory with a `prompts.toml`.

---

## 3. Automated Classification

Before summarization begins, YATSEE may classify the meeting type.

Classification uses transcript cues and context to identify likely meeting categories such as:

```text
city_council
finance_committee
committee_of_the_whole
zoning_committee
general
```

The selected meeting type is then used to resolve prompt routing.

Disable auto-classification:

```bash
yatsee intel summarize \
  -e example_entity \
  --disable-auto-classification
```

Manual prompt overrides:

```bash
yatsee intel summarize \
  -e example_entity \
  --first-prompt <prompt_id> \
  --second-prompt <prompt_id> \
  --final-prompt <prompt_id>
```

---

## 4. Multi-Pass Summarization Workflow

Long civic transcripts often exceed comfortable single-pass context windows. YATSEE processes them through a staged summarization workflow.

### Pass 1: Chunk Extraction

The transcript is split into chunks. Each chunk is summarized using detailed extraction prompts intended to preserve:

- actions
- motions
- votes
- dollar amounts
- named civic objects
- public comments
- speaker-specific context when available

### Pass 2: Consolidation

If the first-pass notes are still too large, YATSEE consolidates them. The goal is to reduce volume while preserving structure and important civic details.

### Pass 3: Final Summary

The final pass produces a polished structured report. Typical sections may include:

- decisions
- motions and votes
- contracts or spending
- ordinances or resolutions
- property and development items
- appointments
- public comments
- follow-up items
- other discussion

Exact sections depend on the selected prompt profile and meeting type.

---

## 5. Chunking Modes

YATSEE supports multiple chunking styles.

```bash
yatsee intel summarize -e example_entity --chunk-style word
yatsee intel summarize -e example_entity --chunk-style sentence
yatsee intel summarize -e example_entity --chunk-style density
```

### Word chunking

Simple chunking by approximate word count.

### Sentence chunking

Uses sentence boundaries where possible.

### Density-aware chunking

Uses semantic indicators to avoid splitting dense civic action sequences.

Density keywords may include terms related to:

- motions
- seconds
- votes
- ordinances
- resolutions
- dollar amounts
- approvals
- public comments

The goal is to keep high-value meeting actions together so the model sees enough context.

---

## 6. Provider-Based LLM Execution

Prompt orchestration is independent of the runtime provider.

The same prompt workflow can run through:

```text
ollama
llamacpp
openai
anthropic
codex_cli
```

Provider settings include:

```text
llm_provider
llm_provider_url
llm_api_key
```

Provider hardening settings include:

```text
llm_allow_remote
llm_allow_insecure_http
llm_allow_loopback_http
llm_allow_custom_executable
```

By default, YATSEE preserves local-first safety assumptions:

- loopback HTTP is controlled separately through `llm_allow_loopback_http`
- off-box provider targets require `llm_allow_remote=true`
- non-loopback plain HTTP additionally requires `llm_allow_insecure_http=true`
- custom executable targets require `llm_allow_custom_executable=true`

---

## 7. Prompt Validation

Validate prompt/profile bundle wiring without running inference:

```bash
yatsee intel prompts validate
yatsee intel prompts validate --entity example_entity
yatsee intel prompts validate --profile civic
yatsee intel prompts validate --all
```

Use `--all` when you intentionally want to surface every discovered profile, including unfinished or broken bundles.

Validation checks that prompt TOML loads, required routes exist, and router entries reference existing prompt IDs. It does not judge prompt wording, output quality, or whether a profile is mature enough for production use.

---

## 8. Prompt Inspection

Print resolved prompts without running inference:

```bash
yatsee intel summarize \
  -e example_entity \
  --print-prompts
```

This is useful for verifying:

- selected job profile
- prompt file path
- fallback behavior
- prompt IDs
- prompt text

---

## 9. Chunk Output Inspection

Write intermediate chunk outputs:

```bash
yatsee intel summarize \
  -e example_entity \
  --enable-chunk-writer
```

This helps isolate whether problems begin at:

- chunking
- prompt routing
- first-pass extraction
- consolidation
- final synthesis

---

## 10. Prompt Orchestration vs Meeting Signals

`yatsee intel summarize` is LLM-backed and uses prompt orchestration.

```bash
yatsee intel summarize -e example_entity
```

`yatsee intel signals` is deterministic and does not use prompt routing or LLM inference.

```bash
yatsee intel signals -e example_entity
```

Signals can be used as a QA artifact alongside summaries. They can help identify mechanical evidence candidates such as motions, money references, people mentions, and low-confidence lines.

---

## Summary: How the Intelligence Layer Works

1. **Resolve prompts**  
   Load entity-specific, global, or fallback prompts.

2. **Classify**  
   Identify meeting type when auto-classification is enabled.

3. **Chunk**  
   Split transcript text while preserving important context.

4. **Extract**  
   Generate detailed chunk-level notes.

5. **Refine**  
   Consolidate notes into structured intermediate summaries.

6. **Synthesize**  
   Produce final Markdown or YAML output.

7. **Inspect**  
   Use prompt printing, chunk writing, and deterministic signals to debug quality.

---

## Final Notes

Prompt orchestration exists because long civic transcripts require more structure than a single generic prompt.

Good prompts cannot fix bad transcripts. Confirm audio, transcription, and normalization quality before tuning prompt behavior.