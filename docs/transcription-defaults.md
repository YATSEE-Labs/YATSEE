# Transcription Defaults

YATSEE uses conservative transcription defaults for public-meeting audio because
transcript artifacts can contaminate downstream normalization, summarization,
search, and record extraction.

The `default` transcription profile is the supported baseline for normal runs and
QA rebuilds.

## Current defaults

- Previous-text conditioning is disabled.
- VAD filtering is enabled for Faster-Whisper runs.
- Entity hotwords remain enabled.
- Repeated n-gram suppression is disabled.
- High-confidence hotword/name-list artifacts may be suppressed before VTT output.
- Lower-confidence transcript issues should be handled by QA review or downstream
  validation, not aggressive ASR filtering.

These defaults were selected after comparing a small Freeport Township corpus
against older generated transcripts. The current defaults reduced obvious
hotword bleed and repeated ASR loops while preserving important procedural
content such as roll-call and vote fragments.

## Examples

Example of an isolated hotword/name-list artifact that may be suppressed:

```text
Altensey, Barb, Barbara, Billie, Altensey, Pat, Patrick, Liz, Mcllwain, Pat, Patty,
```

Example of procedural meeting content that should be preserved:

```text
Wilken, Mcllwain, Altensey, Odendahl, Sellers,
All abstained.
```

The first example is an isolated hotword/name-list hallucination. The second is
meeting procedure split across adjacent transcript segments.

## Rebuilding transcripts

To rebuild transcripts with the current defaults:

```bash
yatsee audio transcribe -e <entity> --faster --get-chunks
```

After rebuilding VTT files, regenerate downstream transcript artifacts:

```bash
yatsee transcript slice -e <entity> --force
yatsee transcript normalize -e <entity> --force
```

The safe transcription behavior is the default. Separate QA transcription
profiles should only be added when they materially change behavior and are
documented with their intended tradeoffs.
