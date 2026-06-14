"""
Transcript normalization stage for YATSEE.

This module normalizes transcript text artifacts produced by the transcript
slicing stage. It preserves the behavior of the original standalone
yatsee_normalize_structure.py script while using the packaged YATSEE
configuration and CLI architecture.

Default behavior:
- input from transcripts_<model>/ TXT artifacts or direct override
- output to normalized/ or direct override
- mechanical cleanup and sentence shaping
- optional spaCy sentence splitting
- optional paragraph preservation
- optional deeper filler/bracket cleanup
- entity-specific replacement rules
- one sentence or utterance per line

This stage intentionally avoids semantic rewriting. It improves formatting,
spacing, capitalization, repetition artifacts, and sentence boundaries, but it
does not invent missing content or correct uncertain transcription meaning.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from yatsee.core.config import load_entity_config, load_global_config
from yatsee.core.discovery import discover_files
from yatsee.core.errors import ConfigError, ValidationError

SUPPORTED_INPUT_EXTENSIONS = (".txt",)


def load_text(path: str) -> str:
    """
    Read a UTF-8 transcript text file.

    :param path: Path to the input text file
    :return: Raw file contents
    :raises ConfigError: If the file cannot be read
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise ConfigError(f"Failed to read transcript '{path}': {exc}") from exc


def write_text(path: str, content: str) -> None:
    """
    Write normalized transcript text to disk.

    :param path: Output file path
    :param content: Normalized text content
    :return: None
    :raises ConfigError: If writing fails
    """
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
    except OSError as exc:
        raise ConfigError(f"Failed to write normalized transcript '{path}': {exc}") from exc


def normalize_text(
    text: str,
    deep: bool = False,
    preserve_entities: Optional[List[str]] = None,
) -> str:
    """
    Normalize transcript text while preserving factual content.

    This restores the original YATSEE normalization behavior. It intentionally
    collapses existing line breaks into spaces before sentence splitting so
    slice-stage segment boundaries do not leak into normalized output.

    Cleanup includes:
    - whitespace collapse
    - character and phrase repetition limiting
    - punctuation spacing cleanup
    - time normalization
    - numeric reassembly
    - currency and percent cleanup
    - U.S. normalization
    - optional filler/bracket cleanup

    :param text: Raw transcript text
    :param deep: Enable deeper filler and bracket cleanup
    :param preserve_entities: Entity names or phrases to protect during cleanup
    :return: Normalized text
    """
    preserve_entities = preserve_entities or []
    placeholders = {f"__ENTITY_{idx}__": name for idx, name in enumerate(preserve_entities)}

    # Protect known entity names so capitalization and case-insensitive cleanup
    # do not accidentally deform local names or configured terminology.
    for placeholder, name in placeholders.items():
        text = re.sub(
            r"\b" + re.escape(name) + r"\b",
            placeholder,
            text,
            flags=re.IGNORECASE,
        )

    # Collapse structural whitespace early. This is the key behavior from the
    # old script that prevents slice-stage blank lines from becoming output
    # paragraph breaks unless paragraph preservation is explicitly requested
    # later.
    text = re.sub(r"[\s\r\n\u00A0]+", " ", text)

    # Collapse long character stutters such as "sooooo" while preserving a
    # small amount of emphasis.
    text = re.sub(r"(.)\1{3,}", r"\1\1", text)

    # Collapse single-letter stutters such as "I I I".
    text = re.sub(r"\b([A-Za-z])(?:[\s,]+\1){2,}\b", r"\1", text)

    # Collapse repeated short phrases. This catches common ASR loops without
    # deleting normal repeated procedural language.
    text = re.sub(
        r"\b((?:\w+\s+){0,3}\w+)(?:\s+\1){2,}",
        r"\1",
        text,
        flags=re.IGNORECASE,
    )

    # Normalize quote and dash variants into simpler forms.
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("‘", "'").replace("’", "'")
    text = text.replace("—", "-").replace("–", "-")

    # Standardize punctuation spacing. This intentionally adds spaces after
    # punctuation first, then later repairs times and numeric forms that should
    # not contain spaces.
    text = re.sub(r",{2,}", ",", text)
    text = re.sub(r"([?!])\1+", r"\1", text)
    text = re.sub(r"\s*\.\s*\.\s*\.", " ... ", text)
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)
    text = re.sub(r"([.,!?;:])(?=\S)", r"\1 ", text)

    # Normalize common AM/PM time variants:
    # - "4: 16 p. M." -> "4:16 PM"
    # - "4.30 a. m." -> "4:30 AM"
    # - "10 a.m."    -> "10 AM"
    text = re.sub(
        r"\b(\d{1,2})(?:(?:\s*[:.]\s*|\s+)?(\d{2}))?\s*([AaPp])\.?\s*[Mm](?:\.(?!\w)|\b)",
        lambda match: (
            f"{match.group(1)}:{match.group(2)} {match.group(3).upper()}M"
            if match.group(2)
            else f"{match.group(1)} {match.group(3).upper()}M"
        ),
        text,
    )

    # Clean punctuation spacing again after time normalization.
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)

    # Re-glue decimals, ratios, and time-like numeric forms:
    # - "3 . 5" -> "3.5"
    # - "4: 16" -> "4:16"
    text = re.sub(r"(\d+)\s*([.:])\s*(\d+)", r"\1\2\3", text)

    # Re-glue large numbers:
    # - "2, 525, 000" -> "2,525,000"
    #
    # The negative lookahead avoids merging dates such as "March 21, 2025"
    # because the second group must be exactly three digits.
    large_number_pattern = r"(\d{1,3})(?:,\s+|\s+,|\s+)(\d{3})(?!\d)"
    text = re.sub(large_number_pattern, r"\1,\2", text)
    text = re.sub(large_number_pattern, r"\1,\2", text)

    # Common mechanical fixes.
    text = re.sub(r"\bi\b", "I", text)
    text = re.sub(r"\$\s+(\d)", r"$\1", text)
    text = re.sub(r"(\d)\s+%", r"\1%", text)
    text = re.sub(r"\b(u)\.\s*(s)\.\b", "U.S.", text, flags=re.IGNORECASE)

    if deep:
        # Deep cleaning should remain conservative. It removes obvious filler
        # and bracket noise but does not rewrite meaning.
        filler_words = r"\b(um|uh|erm|you know|like|ah|mm|hmm)\b"
        text = re.sub(filler_words, "", text, flags=re.IGNORECASE)
        text = re.sub(r"\[\[.*?\]\]", "", text)
        text = re.sub(r"\[(?:music|applause|laughter|noise)\]", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\((?:music|applause|laughter|noise)\)", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s{2,}", " ", text)

    # Restore protected entity placeholders.
    for placeholder, name in placeholders.items():
        text = text.replace(placeholder, name)

    return text.strip()


def capitalize_sentences(
    text: str,
    preserve_entities: Optional[List[str]] = None,
) -> str:
    """
    Capitalize the first alphabetical character of each sentence.

    This mirrors the old script behavior and fixes cases where sentence starts
    became lowercase after VTT slicing or transcription artifacts, such as
    "their decisions..." becoming "Their decisions...".

    :param text: Sentence-separated text
    :param preserve_entities: Entity names or phrases to avoid changing
    :return: Text with sentence starts capitalized
    """
    preserve_entities = preserve_entities or []
    sentences = re.split(r"(?<=[.!?])\s+", text)
    capitalized: List[str] = []

    for sentence in sentences:
        stripped = sentence.lstrip()
        if not stripped:
            continue

        if any(stripped.lower().startswith(entity.lower()) for entity in preserve_entities):
            capitalized.append(sentence)
            continue

        match = re.search(r"[A-Za-z]", stripped)
        if not match:
            capitalized.append(sentence)
            continue

        # Use the index in the original sentence so leading whitespace is
        # preserved if present.
        original_index = sentence.index(match.group(0))
        fixed = (
            sentence[:original_index]
            + sentence[original_index].upper()
            + sentence[original_index + 1:]
        )
        capitalized.append(fixed)

    return " ".join(capitalized)


def split_sentences_spacy(text: str, model: Any) -> List[str]:
    """
    Split text into sentences using a loaded spaCy model.

    :param text: Input text
    :param model: Loaded spaCy language model
    :return: Sentence list
    """
    doc = model(text)
    return [sentence.text.strip() for sentence in doc.sents if sentence.text.strip()]


def split_sentences_basic(text: str) -> List[str]:
    """
    Split text into sentence-like units using a regex fallback.

    This is used when spaCy is disabled or unavailable.

    :param text: Input text
    :return: Sentence-like line list
    """
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", text)
    return [part.strip() for part in parts if part.strip()]


def process_text_to_sentences(
    text: str,
    model: Any = None,
    use_spacy: bool = True,
    preserve_paragraphs: bool = False,
    trim_whitespace: bool = True,
) -> str:
    """
    Convert text into one sentence per line.

    When preserve_paragraphs is disabled, all existing paragraph and segment
    boundaries have already been collapsed by normalize_text(), so the output
    becomes the old script's sentence-per-line format.

    :param text: Input text
    :param model: Optional loaded spaCy model
    :param use_spacy: Enable spaCy sentence splitting
    :param preserve_paragraphs: Preserve blank lines between paragraph groups
    :param trim_whitespace: Trim each sentence line
    :return: Sentence-per-line text
    """
    text = text.strip()
    if not text:
        return ""

    if use_spacy and model:
        if preserve_paragraphs:
            paragraphs = re.split(r"\n\s*\n", text)
            processed_paragraphs: List[str] = []

            for paragraph in paragraphs:
                sentences = split_sentences_spacy(paragraph, model)
                if trim_whitespace:
                    sentences = [sentence.strip() for sentence in sentences]
                processed_paragraphs.append("\n".join(sentences))

            return "\n\n".join(processed_paragraphs).strip() + "\n"

        sentences = split_sentences_spacy(text, model)
        if trim_whitespace:
            sentences = [sentence.strip() for sentence in sentences]
        return "\n".join(sentences).strip() + "\n"

    # Fallback mode intentionally keeps one non-empty input line per output line.
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        lines = split_sentences_basic(text)

    return "\n".join(lines).strip() + "\n"


def merge_incomplete_sentences(text: str) -> str:
    """
    Merge consecutive lines that do not end with sentence-ending punctuation.

    This restores the old script behavior. It fixes over-splitting from spaCy
    or line artifacts by buffering lines until a sentence-ending punctuation
    mark appears.

    Paragraph breaks are preserved when present.

    :param text: Multi-line transcript text
    :return: Transcript with incomplete line fragments merged
    """
    end_punctuation = re.compile(r"[.!?…]$")
    paragraphs = re.split(r"\n\s*\n", text)
    merged_paragraphs: List[str] = []

    for paragraph in paragraphs:
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        buffer: List[str] = []
        merged_lines: List[str] = []

        for line in lines:
            buffer.append(line)
            if end_punctuation.search(line):
                merged_lines.append(" ".join(buffer).strip())
                buffer = []

        if buffer:
            merged_lines.append(" ".join(buffer).strip())

        if merged_lines:
            merged_paragraphs.append("\n".join(merged_lines))

    return "\n\n".join(merged_paragraphs).strip() + "\n"


def limit_repetitions(text: str, inline_max: int = 2, line_max: int = 1) -> str:
    """
    Collapse repeated lines and repeated inline phrases.

    This restores the original behavior that reduced ASR loops while retaining
    normal repeated procedural language.

    :param text: Multi-line input text
    :param inline_max: Maximum allowed inline phrase repetitions
    :param line_max: Maximum allowed consecutive identical lines
    :return: Text with repeated content limited
    """
    phrase_pattern = re.compile(
        r"\b((?:\w+\s+){0,4}\w+)(?:\s+\1){" + str(inline_max) + r",}",
        flags=re.IGNORECASE,
    )

    result_lines: List[str] = []
    previous_line_key: Optional[str] = None
    consecutive_count = 0

    for line in text.splitlines():
        processed = phrase_pattern.sub(
            lambda match: (" " + match.group(1)) * inline_max,
            line.strip(),
        ).strip()

        current_key = re.sub(r"[^a-z0-9]", "", processed.lower())

        if not current_key:
            result_lines.append("")
            previous_line_key = None
            consecutive_count = 0
            continue

        if current_key == previous_line_key:
            consecutive_count += 1
        else:
            consecutive_count = 1

        if consecutive_count <= line_max:
            result_lines.append(processed)

        previous_line_key = current_key

    return "\n".join(result_lines).strip() + "\n"


def apply_replacements(text: str, replacements: Dict[str, str]) -> str:
    """
    Apply entity-specific replacement rules.

    Replacements are applied longest-first so specific phrases win before
    shorter partial matches. Matching is whole-word and case-insensitive to
    mirror the original standalone script.

    :param text: Input transcript text
    :param replacements: Mapping of incorrect text to corrected text
    :return: Updated transcript text
    """
    if not replacements:
        return text

    updated = text

    for bad, good in sorted(replacements.items(), key=lambda item: -len(item[0])):
        if not bad:
            continue

        pattern = re.compile(r"\b" + re.escape(bad) + r"\b", re.IGNORECASE)
        updated = pattern.sub(good, updated)

    return updated


def load_spacy_model(model_name: str) -> Any:
    """
    Load a spaCy model once for the normalization run.

    :param model_name: spaCy model name
    :return: Loaded spaCy language model
    :raises RuntimeError: If spaCy or the requested model cannot be loaded
    """
    try:
        import spacy
    except ImportError as exc:
        raise RuntimeError("spaCy is not installed") from exc

    try:
        return spacy.load(model_name)
    except Exception as exc:
        raise RuntimeError(f"Failed to load spaCy model '{model_name}': {exc}") from exc


def resolve_normalize_paths(
    global_config_path: str,
    entity: str | None,
    input_path: str | None,
    output_dir: str | None,
    model_override: str | None,
) -> Dict[str, Any]:
    """
    Resolve config and filesystem paths for transcript normalization.

    With an entity, the default input is transcripts_<model>/ and the default
    output is normalized/. Without an entity, both input and output must be
    provided explicitly.

    :param global_config_path: Path to global yatsee.toml
    :param entity: Optional entity handle
    :param input_path: Optional transcript input override
    :param output_dir: Optional output override
    :param model_override: Optional transcription model override
    :return: Dictionary containing resolved config and paths
    :raises ValidationError: If required arguments are missing
    """
    entity_cfg: Dict[str, Any] = {}
    global_cfg = load_global_config(global_config_path)

    if entity:
        entity_cfg = load_entity_config(global_cfg, entity)
    else:
        if not input_path or not output_dir:
            raise ValidationError(
                "Without --entity, both --input-path and --output-dir must be defined"
            )

    data_path = entity_cfg.get("data_path")
    if entity and not data_path:
        raise ValidationError(f"Entity '{entity}' does not define data_path")

    transcription_model = (
        model_override
        or entity_cfg.get("transcription_model")
        or global_cfg.get("system", {}).get("default_transcription_model", "medium")
    )

    resolved_input = input_path or os.path.join(data_path, f"transcripts_{transcription_model}")
    resolved_output = output_dir or os.path.join(data_path, "normalized")

    sentence_model = entity_cfg.get(
        "sentence_model",
        global_cfg.get("system", {}).get("default_sentence_model", "en_core_web_md"),
    )

    return {
        "global_cfg": global_cfg,
        "entity_cfg": entity_cfg,
        "input_path": resolved_input,
        "output_dir": resolved_output,
        "sentence_model": sentence_model,
    }


def get_preserve_entities(entity_cfg: Dict[str, Any]) -> List[str]:
    """
    Resolve entity names that should be protected during normalization.

    This supports several likely config shapes without requiring a new hard
    config contract. Unknown or missing fields safely produce an empty list.

    :param entity_cfg: Merged entity configuration
    :return: List of entity names or phrases to preserve
    """
    candidates: List[str] = []

    for key in ("preserve_entities", "participants", "divisions"):
        value = entity_cfg.get(key)

        if isinstance(value, list):
            candidates.extend(str(item) for item in value if str(item).strip())

        elif isinstance(value, dict):
            candidates.extend(str(item) for item in value.keys() if str(item).strip())
            candidates.extend(str(item) for item in value.values() if str(item).strip())

    # Keep configured replacement outputs protected too, since these are often
    # canonical names or local terms.
    replacements = entity_cfg.get("replacements", {})
    if isinstance(replacements, dict):
        candidates.extend(str(value) for value in replacements.values() if str(value).strip())

    deduped: List[str] = []
    seen = set()

    for item in candidates:
        normalized = item.strip()
        key = normalized.lower()
        if normalized and key not in seen:
            deduped.append(normalized)
            seen.add(key)

    return deduped


def normalize_transcript_text(
    raw_text: str,
    spacy_model: Any = None,
    use_spacy: bool = True,
    deep_clean: bool = False,
    preserve_paragraphs: bool = False,
    replacements: Optional[Dict[str, str]] = None,
    preserve_entities: Optional[List[str]] = None,
) -> str:
    """
    Normalize raw transcript text using the original YATSEE pipeline order.

    :param raw_text: Raw transcript text
    :param spacy_model: Loaded spaCy model or None
    :param use_spacy: Enable spaCy sentence splitting
    :param deep_clean: Enable deeper filler/bracket cleanup
    :param preserve_paragraphs: Preserve paragraph breaks
    :param replacements: Entity-specific replacement rules
    :param preserve_entities: Entity names or phrases to protect
    :return: Final normalized transcript text
    """
    replacements = replacements or {}
    preserve_entities = preserve_entities or []

    normalized = normalize_text(
        raw_text,
        deep=deep_clean,
        preserve_entities=preserve_entities,
    )
    normalized = capitalize_sentences(
        normalized,
        preserve_entities=preserve_entities,
    )
    processed = process_text_to_sentences(
        normalized,
        model=spacy_model,
        use_spacy=use_spacy,
        preserve_paragraphs=preserve_paragraphs,
    )
    processed = merge_incomplete_sentences(processed)
    processed = limit_repetitions(processed, inline_max=2, line_max=1)
    processed = apply_replacements(processed, replacements)

    return processed.strip() + "\n"


def run_normalize_stage(
    global_config_path: str,
    entity: str | None = None,
    input_path: str | None = None,
    output_dir: str | None = None,
    model_override: str | None = None,
    no_spacy: bool = False,
    deep_clean: bool = False,
    preserve_paragraphs: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Run the transcript normalization stage.

    :param global_config_path: Path to global yatsee.toml
    :param entity: Optional entity handle
    :param input_path: Optional transcript input override
    :param output_dir: Optional output override
    :param model_override: Optional transcription model override
    :param no_spacy: Disable spaCy sentence splitting
    :param deep_clean: Enable deeper cleanup
    :param preserve_paragraphs: Preserve paragraph spacing
    :param force: Overwrite existing outputs
    :return: Summary dictionary describing stage results
    """
    resolved = resolve_normalize_paths(
        global_config_path=global_config_path,
        entity=entity,
        input_path=input_path,
        output_dir=output_dir,
        model_override=model_override,
    )

    entity_cfg = resolved["entity_cfg"]
    transcript_input = resolved["input_path"]
    output_directory = resolved["output_dir"]
    sentence_model = resolved["sentence_model"]

    input_files = discover_files(transcript_input, SUPPORTED_INPUT_EXTENSIONS)
    if not input_files:
        return {
            "input_path": transcript_input,
            "output_dir": output_directory,
            "sentence_model": sentence_model,
            "discovered": 0,
            "written": 0,
            "skipped": 0,
            "messages": [f"No transcript text files found at {transcript_input}"],
        }

    os.makedirs(output_directory, exist_ok=True)

    replacements = entity_cfg.get("replacements", {})
    if not isinstance(replacements, dict):
        replacements = {}

    preserve_entities = get_preserve_entities(entity_cfg)

    spacy_model = None
    use_spacy = not no_spacy

    messages: List[str] = []

    if use_spacy:
        try:
            spacy_model = load_spacy_model(sentence_model)
            messages.append(f"Using spaCy model: {sentence_model}")
        except RuntimeError as exc:
            use_spacy = False
            messages.append(f"spaCy unavailable, falling back to basic splitting: {exc}")

    written = 0
    skipped = 0

    for src_path in input_files:
        base_name = os.path.basename(src_path)
        out_path = os.path.join(output_directory, base_name)

        if os.path.abspath(src_path) == os.path.abspath(out_path) and not force:
            skipped += 1
            messages.append(f"Skipped in-place file without --force: {src_path}")
            continue

        if os.path.exists(out_path) and not force:
            skipped += 1
            messages.append(f"Skipped existing normalized transcript: {out_path}")
            continue

        raw_text = load_text(src_path)

        final_text = normalize_transcript_text(
            raw_text=raw_text,
            spacy_model=spacy_model,
            use_spacy=use_spacy,
            deep_clean=deep_clean,
            preserve_paragraphs=preserve_paragraphs,
            replacements=replacements,
            preserve_entities=preserve_entities,
        )

        write_text(out_path, final_text)
        written += 1
        messages.append(f"Wrote normalized transcript: {out_path}")

    return {
        "input_path": transcript_input,
        "output_dir": output_directory,
        "sentence_model": sentence_model,
        "spacy_enabled": use_spacy,
        "deep_clean": deep_clean,
        "preserve_paragraphs": preserve_paragraphs,
        "discovered": len(input_files),
        "written": written,
        "skipped": skipped,
        "messages": messages,
    }