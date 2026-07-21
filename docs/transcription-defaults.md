# Transcription Defaults

YATSEE uses conservative transcription defaults because ASR errors propagate into normalization, summaries, records, and search.

## Supported baseline

The `default` transcription profile currently uses:

- previous-text conditioning disabled;
- VAD filtering for Faster-Whisper;
- entity hotwords enabled;
- repeated n-gram suppression disabled;
- narrow suppression of high-confidence hotword or name-list artifacts;
- QA and rebuild workflows for lower-confidence failures.

These choices reduce repeated loops and isolated hotword bleed without aggressively deleting procedural language.

Example of a likely isolated hotword artifact:

```text
Altensey, Barb, Barbara, Billie, Altensey, Pat, Patrick, Liz...
```

Example of procedural content that must remain:

```text
Wilken, Mcllwain, Altensey, Odendahl, Sellers,
All abstained.
```

## Rebuild workflow

```bash
yatsee audio transcribe -e <entity> --faster --get-chunks
yatsee transcript slice -e <entity> --force
yatsee transcript normalize -e <entity> --force
```

Use the QA report and reset helpers for selected rebuilds. Do not repair repeated hallucinated content into plausible text. Retranscribe it.
