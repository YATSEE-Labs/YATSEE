# YATSEE User Guide

This guide covers the shortest reliable path from local media to transcripts, Records, Events, summaries, and artifact audits. Use command `--help` output for the complete option reference.

## 1. Preflight

```bash
ffmpeg -version
yatsee --help
yatsee config --help
yatsee audio --help
yatsee transcript --help
yatsee audit --help
yatsee intel --help
```

Create a local config if needed:

```bash
cp yatsee.conf yatsee.toml
```

## 2. Create an entity

An entity represents a recurring body such as a council, board, committee, or agency.

```bash
yatsee config entity add \
  --display-name "Example County Board" \
  --entity example_county_board \
  --base "country.US.state.EX."

yatsee config init -e example_county_board
```

Edit:

```text
data/example_county_board/config.toml
```

At minimum, confirm the entity identity, `data_path`, and media directories. Add people, organizations, local terms, and recurring replacements as they become useful.

Validate before processing:

```bash
yatsee config validate -e example_county_board
yatsee config resolve -e example_county_board
```

## 3. Supply media

Place compatible media in the configured input directory, normally:

```text
data/example_county_board/downloads/
```

YATSEE does not care whether the files came from a fetcher, recorder, upload workflow, archive, or manual copy.

## 4. Format audio

```bash
yatsee audio format -e example_county_board --dry-run
yatsee audio format -e example_county_board
```

Default output is mono 16 kHz FLAC under the entity audio directory. Optional chunking is available for long recordings:

```bash
yatsee audio format \
  -e example_county_board \
  --create-chunks \
  --chunk-duration 600 \
  --chunk-overlap 2
```

Use `--force` only when existing formatted outputs should be replaced.

## 5. Transcribe

```bash
yatsee audio transcribe -e example_county_board --faster
```

Common overrides:

```bash
yatsee audio transcribe -e example_county_board --model medium --device cuda
yatsee audio transcribe -e example_county_board --faster --get-chunks
```

The output is VTT under `transcripts_<model>/`. Existing transcripts are normally skipped.

## 6. Optional transcript audit

For large batches, inspect VTTs before generating downstream artifacts:

```bash
yatsee audit transcript \
  -e example_county_board \
  --model medium
```

The command writes deterministic reports to:

```text
data/example_county_board/audit/transcript/medium/report.json
data/example_county_board/audit/transcript/medium/report.md
```

Reset only `block` and `rerun` transcripts by default, starting with a dry run:

```bash
yatsee audit transcript reset \
  -e example_county_board \
  --model medium
```

Add `--apply`, then rerun transcription, only after confirming the matched VTT
files and tracker updates. Timing review candidates remain informational unless
a file also receives an ASR-loop status requiring review or retranscription.

Suppressed cues can be reviewed through `yatsee audit transcript restore`. The
restore workflow writes a CSV first and inserts only rows explicitly marked
`action=restore`; file changes still require `--apply`.

## 7. Slice VTT into transcript artifacts

```bash
yatsee transcript slice -e example_county_board --force
```

This writes plain TXT beside the VTT. Optional segment JSONL and embeddings can be generated with the following command. The segment JSONL and its stored embeddings are the source artifacts used by records timestamp alignment:

```bash
yatsee transcript slice \
  -e example_county_board \
  --gen-embed \
  --device cuda \
  --force
```

## 8. Normalize transcript text

```bash
yatsee transcript normalize -e example_county_board --force
```

Normalized text is written under:

```text
data/example_county_board/normalized/
```

Useful overrides:

```bash
yatsee transcript normalize -e example_county_board --preserve-paragraphs --force
yatsee transcript normalize -e example_county_board --no-spacy --force
```

Inspect normalized text before tuning prompts or models.

The normal end-to-end path can proceed directly to `yatsee intel summarize`,
which ensures Records and refreshes Events before writing summaries. The next
three sections document those stages independently for inspection, targeted runs,
and corpus QA.

## 9. Extract structured records

```bash
yatsee intel records \
  -e example_county_board \
  -j civic \
  -m gemma4:e4b \
  --max-tokens 1500
```

Records processing:

1. classifies the formal meeting type unless overridden;
2. selects one meeting-specific extraction prompt;
3. runs that prompt once per transcript chunk using compact JSONL;
4. preserves valid record lines while isolating malformed lines or records;
5. deterministically removes only exact duplicates;
6. optionally aligns reused or newly extracted records through `--align-timestamps`;
7. writes a self-describing JSON artifact containing the normalized source hash and human-readable extraction provenance;
8. renders the itemized Markdown view directly from the JSON artifact.

Useful controls:

```bash
# Inspect records routing and prompt text without inference.
yatsee intel records -e example_county_board -j civic --print-prompts

# Bypass the classifier and select a route explicitly.
yatsee intel records \
  -e example_county_board \
  -j civic \
  --meeting-type finance_committee

# Use the general route without model classification.
yatsee intel records \
  -e example_county_board \
  -j civic \
  --disable-auto-classification

# Enrich accepted records with source timestamps when segment JSONL exists.
yatsee intel records \
  -e example_county_board \
  -j civic \
  --align-timestamps
```

Default output:

```text
data/example_county_board/intel_<model>/records/<transcript>/
├── <transcript>.records.json
├── <transcript>.records.md
├── <transcript>.records.alignment.json   # optional diagnostics
├── raw/
├── chunks/
└── errors/
```

Behavior to know:

- Normal records runs reuse existing artifacts whenever their normalized transcript hash matches. Prompt, model, profile, schema/version, status, and chunk metadata do not invalidate reuse.
- The Markdown view is regenerated from the final JSON without invoking the model.
- Records defaults to density-aware chunking; the token budget comes from CLI or model configuration.
- `--force` bypasses source-hash reuse and reruns extraction.
- `--align-timestamps` is strictly opt-in. It uses the entity `embedding_model` override or `[system].default_embedding_model`.
- Hybrid alignment embeds only record evidence/context, compares those vectors against the embeddings already stored in segment JSONL, and lexically validates a bounded set of nearby windows. Exact repeated phrases are retained as competing candidates so the shortlist does not suppress ambiguity.
- The sliced segment embeddings and timestamp alignment must use the same embedding model. Regenerate segment JSONL with `--gen-embed --force` after changing that model.
- `--align-timestamps` performs the normal records workflow and then aligns every successful artifact. Source-matching records are reused; missing records and records whose normalized source changed are extracted.
- `--force --align-timestamps` explicitly rebuilds every selected records artifact and then aligns each successful result. A failed or partial rebuild does not replace the previous final artifact.
- `--timestamp-only` returns before prompt loading, provider resolution, chunk generation, or LLM session creation. It force-recalculates timestamps for any usable existing records JSON regardless of schema version, status, normalized transcript hash, prompt/model/profile metadata, or chunk counters, and does not reuse a compatible alignment.
- In timestamp-only mode, missing or unusable records artifacts and missing segment JSONL files are reported as skipped, existing `raw/`, `chunks/`, and `errors/` contents are untouched, and successful alignments rewrite the records JSON, Markdown, and alignment report.
- Evidence absent from the current segment artifact is left unmatched.
- Accepted matches add numeric `timestamp` seconds and case-preserved `video_id` when available. The Markdown view converts that pair into a timestamped YouTube link. Ambiguous or unsupported evidence receives neither field.
- Timestamp alignment has its own fingerprint and diagnostic sidecar, so matcher changes do not invalidate the records extraction cache.
- Records runs are not resumable at the chunk level yet.
- Invalid records are isolated and reported while valid sibling records continue. A malformed JSONL line marks its chunk partial; an empty or unusable response marks the chunk failed. Later chunks continue, and the attempt is written as `.records.partial.json` and `.records.partial.md` without replacing a prior complete artifact.
- The civic profile supports Records. Any profile without `records_prompt.toml` fails clearly.

## 10. Audit structured Records

Run the deterministic Records audit after extraction when a corpus-level QA
report is useful:

```bash
yatsee audit records \
  -e example_county_board \
  --model gemma4:e4b \
  --profile balanced
```

The default audit is deterministic and does not invoke an LLM. It writes CSV,
JSON, and Markdown reports under:

```text
data/example_county_board/audit/records/gemma4_e4b/
```

Use `--profile strict` for more sensitive review. Rebuild planning is explicit:
`--rebuild` writes a manifest and stops without invoking an LLM, while
`--rebuild-auto` invokes Records generation only for structurally justified
candidates before rerunning the audit.

## 11. Generate deterministic civic events

```bash
yatsee intel events \
  -e example_county_board \
  -m gemma4:e4b
```

The events workflow reads complete `*.records.json` artifacts and performs no model inference. It:

1. preserves every source record with one-based record indices;
2. collapses only strong nearby duplicates with compatible record types;
3. groups local motion, second, request, vote, and explicit outcome sequences;
4. leaves uncertain or unrelated records separate;
5. writes deterministic JSON and Markdown for each meeting;
6. writes aggregate `events.jsonl` and `meetings.json` indexes for publication or search.

Default output:

```text
data/example_county_board/intel_<model>/events/
├── events.jsonl
├── meetings.json
└── <transcript>/
    ├── <transcript>.events.json
    └── <transcript>.events.md
```

Use `--meeting <text>` to process a matching subset or `--output-dir` to place the derived event tree elsewhere. Event generation atomically replaces its own derived files but never changes records JSON or Markdown. An unattached motion request or seconder remains present with a review flag rather than being silently discarded.


## 12. Generate summaries

The summary command orchestrates the complete Records-first workflow:

```text
source text
→ create or reuse Records
→ refresh deterministic Events
→ consolidate only when the structured facts exceed the configured payload
→ apply the selected final summary prompt
→ write the canonical summary
```

Process an entity corpus:

```bash
yatsee intel summarize -e example_county_board
```

Process one full source filename inside the entity workspace:

```bash
yatsee intel summarize \
  -e example_county_board \
  -i data/example_county_board/normalized/example_meeting.txt
```

Standalone source files require an explicit prompt profile:

```bash
yatsee intel summarize -i ./document.txt -j <profile>
```

The command delegates extraction to the same implementation used by `yatsee intel records`. Existing Records are reused when their normalized-source hash matches; missing or changed Records are extracted. Events are then regenerated deterministically from the selected Records artifacts. The meeting type stored in those artifacts selects the configured `first / multi / final` summary route.

Summary detail may vary with the selected profile and model. These artifacts are
reader-oriented summaries, not official minutes. Publication systems may transform
the canonical artifacts separately without changing YATSEE core output.

Useful controls:

```bash
# Inspect summary prompt text without invoking the provider.
yatsee intel summarize -e example_county_board --print-prompts

# Persist intermediate consolidation chunks for debugging.
yatsee intel summarize -e example_county_board --enable-chunk-writer

# Override summary consolidation chunking and token budget.
yatsee intel summarize -e example_county_board --chunk-style sentence --max-tokens 2500
```

The `--model` option selects both Records extraction and summary generation for the current implementation. `yatsee intel run` remains a compatibility alias. Run `yatsee intel records` directly when only structured extraction is wanted without Events or a final summary.

## 13. Inspect outputs

Typical layout:

```text
data/example_county_board/
├── downloads/
├── audio/
├── transcripts_medium/
├── normalized/
├── audit/
│   ├── transcript/<transcription-model>/
│   └── records/<records-model>/
└── intel_<model>/
    ├── summaries/
    ├── records/
    └── events/
```

Quick counts:

```bash
find data/example_county_board/transcripts_medium -name '*.vtt' | wc -l
find data/example_county_board/normalized -name '*.txt' | wc -l
find data/example_county_board/intel_* \( -name '*.summary.md' -o -name '*.summary.yaml' \)
find data/example_county_board/intel_*/records -name '*.records.json'
find data/example_county_board/intel_*/records -name '*.records.md'
find data/example_county_board/intel_*/events -name '*.events.json'
find data/example_county_board/intel_*/events -name '*.events.md'
find data/example_county_board/audit -name 'report.json' -o -name 'report.md' -o -name 'report.csv'
```

## Direct-path runs

An entity is optional for intelligence stages. Standalone Records extraction still requires an explicit output directory:

```bash
yatsee intel records \
  --txt-input ./normalized \
  --output-dir ./records_out \
  --model gemma4:e4b \
  -j civic
```

Standalone summary orchestration derives a model-specific `intel_<model>` workspace beside the supplied file or directory, but requires an explicit `-j/--job-profile`.

## Recommended operating order

```text
validate configuration
→ inspect source media
→ format audio
→ transcribe
→ audit VTT quality when needed
→ reset and retranscribe selected failures when required
→ slice
→ normalize
→ inspect normalized text
→ run summarize to ensure Records, refresh Events, and write summaries
→ audit Records when a corpus-level review is useful
→ review derived artifacts against source evidence
```

Do not compensate for weak audio or transcript quality by immediately changing prompts. Fix the earliest broken artifact first.
