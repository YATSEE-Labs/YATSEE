"""
Structured civic records extraction for YATSEE.

Records is an independent intelligence workflow. It classifies a normalized
transcript, selects one meeting-type-specific extraction prompt, generates one
structured response per transcript chunk, and consolidates validated records
with deterministic code. The final JSON artifact is self-describing and acts as
the cache identity for later runs and deterministic Markdown rendering.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Dict, Iterable, List

from yatsee.core.errors import ConfigError, ValidationError
from yatsee.core.format import format_markdown_inline, format_timestamp
from yatsee.core.hashing import compute_text_sha256
from yatsee.core.io import ensure_directory, read_text, write_text, write_text_atomic
from yatsee.core.paths import (
    resolve_contained_path,
    sanitize_path_component,
    serialize_project_path,
)
from yatsee.intel.classifier import (
    classify_transcript,
    resolve_single_classifier_label,
)
from yatsee.intel.llm import (
    GeneratedChunk,
    LLMRuntime,
    TokenUsage,
    create_llm_session,
    generate_chunk_outputs,
    resolve_llm_runtime,
)
from yatsee.intel.prompts import (
    load_classifier_bundle,
    load_records_prompt_bundle,
    load_summary_prompt_bundle,
    resolve_records_prompt_id,
)
from yatsee.intel.resolve import extract_context_from_filename, resolve_record_paths
from yatsee.intel.schema import ALLOWED_RECORD_TYPES
from yatsee.intel.speakers import build_name_permutations
from yatsee.providers.pricing import build_pricing_summary
from yatsee.providers.tokenization import estimate_token_count

logger = logging.getLogger(__name__)

ARTIFACT_KIND = "records"
ARTIFACT_SCHEMA_VERSION = "civic_records_v1"
EXTRACTION_VERSION = "records_v3"
FINAL_SUFFIX = ".records.json"
PARTIAL_SUFFIX = ".records.partial.json"
MARKDOWN_SUFFIX = ".records.md"
ALIGNMENT_SUFFIX = ".records.alignment.json"
PARTIAL_MARKDOWN_SUFFIX = ".records.partial.md"

RECORD_SECTION_ORDER = (
    "Attendance",
    "Motions and Votes",
    "Procedural Actions",
    "Ordinances and Resolutions",
    "Financial and Contract Records",
    "Projects",
    "Appointments",
    "Presentations and Reports",
    "Metrics",
    "Discussion",
    "Public Comment",
    "Proclamations and Recognitions",
    "Follow-ups",
    "Other",
)

RECORD_TYPE_SECTIONS = {
    "attendance": "Attendance",
    "motion": "Motions and Votes",
    "vote": "Motions and Votes",
    "procedural_action": "Procedural Actions",
    "ordinance": "Ordinances and Resolutions",
    "resolution": "Ordinances and Resolutions",
    "financial": "Financial and Contract Records",
    "contract": "Financial and Contract Records",
    "project": "Projects",
    "appointment": "Appointments",
    "presentation": "Presentations and Reports",
    "metric": "Metrics",
    "discussion": "Discussion",
    "public_comment": "Public Comment",
    "proclamation": "Proclamations and Recognitions",
    "recognition": "Proclamations and Recognitions",
    "follow_up": "Follow-ups",
    "other": "Other",
}

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass(frozen=True)
class RecordsRunConfig:
    """
    Hold records-specific runtime settings.

    :param chunk_style: Chunk boundary strategy
    :param max_tokens: Approximate source token budget per chunk
    :param disable_auto_classification: Whether to skip model classification
    :param meeting_type: Optional explicit meeting-type override
    :param show_pricing: Whether to calculate reference pricing
    :param pricing_provider: Optional reference pricing provider
    :param pricing_model: Optional reference pricing model
    """

    chunk_style: str
    max_tokens: int
    disable_auto_classification: bool
    meeting_type: str | None
    show_pricing: bool
    pricing_provider: str | None
    pricing_model: str | None


def _strip_json_fence(text: str) -> str:
    """
    Remove one surrounding Markdown fence from a provider response.

    :param text: Raw provider response
    :return: Unfenced response text
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()

    return stripped


def _clean_value(value: Any) -> Any:
    """
    Remove empty optional values and normalize whitespace recursively.

    :param value: Parsed JSON value
    :return: Cleaned JSON-compatible value
    """
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, list):
        cleaned = [_clean_value(item) for item in value]
        return [item for item in cleaned if item not in (None, "", [], {})]
    if isinstance(value, dict):
        cleaned = {
            str(key): _clean_value(item)
            for key, item in value.items()
        }
        return {
            key: item
            for key, item in cleaned.items()
            if item not in (None, "", [], {})
        }
    return value


def _normalize_record(
    raw_record: Any,
    record_index: int,
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Validate and normalize one model-produced record.

    :param raw_record: Parsed JSON value for one record
    :param record_index: One-based record position for diagnostics
    Unsupported non-empty model types are retained safely as ``other`` with the
    original value preserved in attributes. This keeps otherwise grounded
    records while making the deterministic schema authoritative.

    :return: Normalized record dictionary and non-fatal normalization warnings
    :raises ValidationError: If the record does not satisfy the schema
    """
    if not isinstance(raw_record, dict):
        raise ValidationError(f"Record {record_index} must be a JSON object")

    cleaned = _clean_value(raw_record)
    raw_record_type = cleaned.get("record_type")
    if not isinstance(raw_record_type, str) or not raw_record_type.strip():
        raise ValidationError(
            f"Record {record_index} must define a non-empty 'record_type' string"
        )
    record_type = raw_record_type.strip().lower().replace(" ", "_")

    for key in ("subject", "summary", "evidence"):
        if not isinstance(cleaned.get(key), str) or not cleaned[key].strip():
            raise ValidationError(
                f"Record {record_index} must define a non-empty '{key}' string"
            )

    attributes = cleaned.get("attributes", {})
    if attributes and not isinstance(attributes, dict):
        raise ValidationError(f"Record {record_index} attributes must be an object")
    attributes = dict(attributes or {})

    normalization_issues: List[Dict[str, Any]] = []
    if record_type not in ALLOWED_RECORD_TYPES:
        # Model-produced labels are untrusted. Preserve the useful record and
        # original label without allowing it to expand the stable schema.
        attributes.setdefault("model_record_type", record_type)
        normalization_issues.append(
            {
                "scope": "record",
                "record_index": record_index,
                "severity": "warning",
                "action": "normalized",
                "error": (
                    f"Record {record_index} has unsupported record_type "
                    f"'{record_type}'; normalized to 'other'"
                ),
                "original_record_type": record_type,
                "normalized_record_type": "other",
            }
        )
        record_type = "other"

    attribution = cleaned.get("attribution")
    if attribution is None:
        attribution = attributes.pop("attribution", None)
    if attribution is not None and not isinstance(attribution, str):
        raise ValidationError(
            f"Record {record_index} attribution must be a string"
        )

    required_keys = {"record_type", "subject", "summary", "evidence"}
    known_keys = required_keys | {"attribution", "attributes"}
    for key, value in cleaned.items():
        if key not in known_keys:
            attributes.setdefault(key, value)

    normalized = {
        "record_type": record_type,
        "subject": cleaned["subject"],
        "summary": cleaned["summary"],
        "evidence": cleaned["evidence"],
    }
    if attribution:
        normalized["attribution"] = attribution
    if attributes:
        normalized["attributes"] = _clean_value(attributes)

    return normalized, normalization_issues


def _parse_records_response_with_issues(
    raw_output: str,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Parse one chunk response as JSONL while isolating line-level failures.

    Each non-empty line should contain one complete JSON record object. Valid
    lines are retained even when a later line is malformed or truncated. The
    legacy direct-array and ``{"records": [...]}`` forms remain accepted for
    compatibility with existing tests and saved responses.

    :param raw_output: Raw provider response
    :return: Valid normalized records and validation issues
    :raises ValidationError: If the provider response is empty or has an invalid
        legacy root
    """
    candidate = _strip_json_fence(raw_output)
    if not candidate:
        raise ValidationError("Records provider returned an empty response")

    if candidate == "[]":
        return [], []

    normalized_records: List[Dict[str, Any]] = []
    validation_issues: List[Dict[str, Any]] = []
    raw_records: List[tuple[int | None, Any]] = []

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict) and "records" in parsed:
        records = parsed.get("records")
        if not isinstance(records, list):
            raise ValidationError(
                "Legacy records response must define a 'records' list"
            )
        raw_records.extend((None, record) for record in records)
    elif isinstance(parsed, list):
        raw_records.extend((None, record) for record in parsed)
    elif isinstance(parsed, dict):
        raw_records.append((1, parsed))
    else:
        for line_number, line in enumerate(candidate.splitlines(), start=1):
            stripped_line = line.strip()
            if not stripped_line:
                continue

            try:
                raw_record = json.loads(stripped_line)
            except json.JSONDecodeError as exc:
                validation_issues.append(
                    {
                        "scope": "line",
                        "line_number": line_number,
                        "severity": "warning",
                        "action": "rejected",
                        "error": (
                            "Records JSONL line is not valid JSON: "
                            f"{exc.msg} at column {exc.colno}"
                        ),
                        "rejected_line": stripped_line,
                    }
                )
                continue

            if not isinstance(raw_record, dict):
                validation_issues.append(
                    {
                        "scope": "line",
                        "line_number": line_number,
                        "severity": "warning",
                        "action": "rejected",
                        "error": "Records JSONL line must be a JSON object",
                        "rejected_line": _clean_value(raw_record),
                    }
                )
                continue

            raw_records.append((line_number, raw_record))

    for record_index, (line_number, raw_record) in enumerate(
        raw_records,
        start=1,
    ):
        try:
            normalized_record, normalization_issues = _normalize_record(
                raw_record,
                record_index,
            )
            normalized_records.append(normalized_record)
            for issue in normalization_issues:
                if line_number is not None:
                    issue["line_number"] = line_number
                validation_issues.append(issue)
        except ValidationError as exc:
            issue = {
                "scope": "record",
                "record_index": record_index,
                "severity": "warning",
                "action": "rejected",
                "error": str(exc),
                "rejected_record": _clean_value(raw_record),
            }
            if line_number is not None:
                issue["line_number"] = line_number
            validation_issues.append(issue)

    return normalized_records, validation_issues


def parse_records_response(raw_output: str) -> List[Dict[str, Any]]:
    """
    Parse and strictly validate one chunk-level records response.

    The preferred format is JSONL with one complete record object per line.
    Legacy ``{"records": [...]}`` and direct-array responses remain accepted.
    This public helper preserves strict behavior for callers and tests. The
    records workflow uses the tolerant internal parser so one malformed or
    truncated line does not discard valid sibling records.

    :param raw_output: Raw provider response
    :return: Normalized record dictionaries
    :raises ValidationError: If JSON or any record field is invalid
    """
    records, validation_issues = _parse_records_response_with_issues(raw_output)
    rejected_issues = [
        issue
        for issue in validation_issues
        if issue.get("action") == "rejected"
    ]
    if rejected_issues:
        raise ValidationError(rejected_issues[0]["error"])
    return records


def consolidate_records(
    chunk_records: Iterable[tuple[int, List[Dict[str, Any]]]],
) -> List[Dict[str, Any]]:
    """
    Deterministically combine chunk records while preserving source order.

    Only exact normalized duplicates are merged. Ambiguous, conflicting, or
    merely similar records remain separate rather than being reconciled with
    probabilistic or fuzzy logic.

    :param chunk_records: Pairs of chunk IDs and validated record lists
    :return: Consolidated records with source chunk references
    """
    consolidated: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}

    for chunk_id, records in chunk_records:
        for record in records:
            identity = json.dumps(record, sort_keys=True, separators=(",", ":"))
            existing_index = seen.get(identity)

            if existing_index is not None:
                source_chunks = consolidated[existing_index]["source_chunks"]
                if chunk_id not in source_chunks:
                    source_chunks.append(chunk_id)
                continue

            output_record = dict(record)
            output_record["source_chunks"] = [chunk_id]
            seen[identity] = len(consolidated)
            consolidated.append(output_record)

    return consolidated


def _artifact_paths(output_dir: str, base_name: str) -> Dict[str, str]:
    """
    Resolve contained output paths for one transcript records run.

    :param output_dir: Root records output directory
    :param base_name: Transcript basename
    :return: Transcript directory and artifact paths
    """
    safe_base_name = sanitize_path_component(base_name)
    transcript_dir = resolve_contained_path(output_dir, safe_base_name)
    return {
        "transcript_dir": transcript_dir,
        "raw_dir": resolve_contained_path(transcript_dir, "raw"),
        "chunks_dir": resolve_contained_path(transcript_dir, "chunks"),
        "errors_dir": resolve_contained_path(transcript_dir, "errors"),
        "final_path": resolve_contained_path(
            transcript_dir,
            f"{safe_base_name}{FINAL_SUFFIX}",
        ),
        "partial_path": resolve_contained_path(
            transcript_dir,
            f"{safe_base_name}{PARTIAL_SUFFIX}",
        ),
        "markdown_path": resolve_contained_path(
            transcript_dir,
            f"{safe_base_name}{MARKDOWN_SUFFIX}",
        ),
        "partial_markdown_path": resolve_contained_path(
            transcript_dir,
            f"{safe_base_name}{PARTIAL_MARKDOWN_SUFFIX}",
        ),
        "alignment_path": resolve_contained_path(
            transcript_dir,
            f"{safe_base_name}{ALIGNMENT_SUFFIX}",
        ),
    }


def _prepare_artifact_directory(paths: Dict[str, str]) -> None:
    """
    Reset mutable chunk artifacts while preserving prior final outputs.

    A stale but complete final records artifact remains available until a new
    complete artifact is atomically written. Raw, chunk, and validation
    directories are rebuilt for the current attempt.

    :param paths: Resolved transcript artifact paths
    :return: None
    """
    ensure_directory(paths["transcript_dir"])

    for directory_key in ("raw_dir", "chunks_dir", "errors_dir"):
        directory = paths[directory_key]
        try:
            if os.path.lexists(directory):
                if os.path.isdir(directory) and not os.path.islink(directory):
                    shutil.rmtree(directory)
                else:
                    os.unlink(directory)
        except OSError as exc:
            raise ConfigError(
                f"Failed to reset records artifact directory '{directory}': {exc}"
            ) from exc
        ensure_directory(directory)


def _resolve_record_segments_path(
    *,
    entity_cfg: Dict[str, Any],
    transcript_path: str,
    base_name: str,
) -> str | None:
    """
    Resolve an optional sliced segment artifact for one normalized transcript.

    Entity runs prefer the configured transcription directory. Direct-path runs
    may place the segment JSONL beside the supplied transcript. Missing segment
    files are normal because timestamp alignment is optional enrichment.

    :param entity_cfg: Resolved entity configuration
    :param transcript_path: Normalized transcript path
    :param base_name: Transcript basename
    :return: Existing segment JSONL path or ``None``
    """
    filename = f"{base_name}.segments.jsonl"
    candidates: List[str] = []

    data_path = entity_cfg.get("data_path")
    transcription_model = (
        entity_cfg.get("transcription_model")
        or entity_cfg.get("default_transcription_model")
    )
    if isinstance(data_path, str) and data_path and transcription_model:
        candidates.append(
            os.path.join(
                data_path,
                f"transcripts_{transcription_model}",
                filename,
            )
        )

    candidates.append(os.path.join(os.path.dirname(transcript_path), filename))

    seen: set[str] = set()
    for candidate in candidates:
        resolved = os.path.abspath(candidate)
        if resolved in seen:
            continue
        seen.add(resolved)
        if os.path.isfile(resolved):
            return resolved
    return None


def _timestamp_result_metadata(
    *,
    status: str,
    message: str,
    alignment_path: str | None = None,
    metadata: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    Build consistent per-meeting timestamp alignment result metadata.

    :param status: Alignment execution status
    :param message: Human-readable status detail
    :param alignment_path: Optional diagnostic sidecar path
    :param metadata: Optional compact alignment metadata
    :return: Result fields for CLI reporting
    """
    metadata = metadata or {}
    return {
        "timestamp_alignment_status": status,
        "timestamp_alignment_message": message,
        "timestamp_alignment_path": alignment_path,
        "timestamp_matched_record_count": metadata.get(
            "matched_record_count",
            0,
        ),
        "timestamp_ambiguous_record_count": metadata.get(
            "ambiguous_record_count",
            0,
        ),
        "timestamp_unmatched_record_count": metadata.get(
            "unmatched_record_count",
            0,
        ),
    }


def _enrich_records_with_timestamps(
    *,
    artifact: Dict[str, Any],
    paths: Dict[str, str],
    transcript_path: str,
    base_name: str,
    entity_cfg: Dict[str, Any],
    embedding_model: str,
    project_root: str = ".",
    force_realign: bool = False,
) -> tuple[Dict[str, Any], Dict[str, Any], bool]:
    """
    Best-effort enrich one complete records artifact with source timestamps.

    Missing or invalid segment artifacts do not downgrade records extraction.
    The source records remain usable and the caller receives a compact skip or
    failure status. Matching details are written to a separate diagnostic
    sidecar only after alignment succeeds.

    :param artifact: Complete records artifact
    :param paths: Resolved records artifact paths
    :param transcript_path: Normalized transcript path
    :param base_name: Transcript basename
    :param entity_cfg: Resolved entity configuration
    :param embedding_model: Configured SentenceTransformer model
    :param project_root: Directory containing the active ``yatsee.toml``
    :param force_realign: Whether to bypass compatible alignment reuse
    :return: Artifact, result metadata, and whether the artifact changed
    """
    segments_path = _resolve_record_segments_path(
        entity_cfg=entity_cfg,
        transcript_path=transcript_path,
        base_name=base_name,
    )
    if segments_path is None:
        return (
            artifact,
            _timestamp_result_metadata(
                status="skipped",
                message="matching segments JSONL not found",
            ),
            False,
        )

    try:
        from yatsee.intel.timestamp import (
            RecordTimestampConfig,
            align_records_artifact,
            alignment_is_current,
        )

        config = RecordTimestampConfig(model_name=embedding_model)
        segment_path_obj = Path(segments_path)

        if (
            not force_realign
            and os.path.isfile(paths["alignment_path"])
            and alignment_is_current(
                artifact=artifact,
                segments_path=segment_path_obj,
                config=config,
            )
        ):
            try:
                report = json.loads(read_text(paths["alignment_path"]))
                if not isinstance(report, dict):
                    raise ValueError("alignment report must be a JSON object")
                expected_inputs = {
                    "segments": serialize_project_path(
                        segments_path,
                        project_root,
                    ),
                    "records": serialize_project_path(
                        paths["final_path"],
                        project_root,
                    ),
                }
                if report.get("inputs") != expected_inputs:
                    report["inputs"] = expected_inputs
                    write_text_atomic(
                        paths["alignment_path"],
                        json.dumps(
                            report,
                            indent=2,
                            ensure_ascii=False,
                        ) + "\n",
                    )
                metadata = artifact.get("timestamp_alignment", {})
                return (
                    artifact,
                    _timestamp_result_metadata(
                        status="reused",
                        message="compatible timestamp alignment reused",
                        alignment_path=paths["alignment_path"],
                        metadata=metadata,
                    ),
                    False,
                )
            except (OSError, ValueError, TypeError) as exc:
                logger.warning(
                    "Rebuilding unreadable timestamp alignment report '%s': %s",
                    paths["alignment_path"],
                    exc,
                )

        outcome = align_records_artifact(
            artifact=artifact,
            segments_path=segment_path_obj,
            config=config,
            segments_label=serialize_project_path(
                segments_path,
                project_root,
            ),
            records_label=serialize_project_path(
                paths["final_path"],
                project_root,
            ),
        )
        write_text_atomic(
            paths["alignment_path"],
            json.dumps(outcome.report, indent=2, ensure_ascii=False) + "\n",
        )
        return (
            outcome.artifact,
            _timestamp_result_metadata(
                status="complete",
                message="record evidence aligned to timestamped segments",
                alignment_path=paths["alignment_path"],
                metadata=outcome.metadata,
            ),
            True,
        )
    except Exception as exc:
        logger.warning(
            "Timestamp alignment skipped for '%s': %s",
            base_name,
            exc,
        )
        return (
            artifact,
            _timestamp_result_metadata(
                status="failed",
                message=str(exc),
            ),
            False,
        )


def load_reusable_records_artifact(
    path: str,
    *,
    source_sha256: str,
) -> Dict[str, Any] | None:
    """
    Load an existing records artifact when its normalized source is unchanged.

    Prompt, classifier, model, profile, schema, extraction version, status, and
    chunk metadata are provenance only. Operators explicitly request a rebuild
    with ``--force`` when those inputs change. A source hash mismatch is the only
    automatic cache invalidator.

    Existing files that cannot support deterministic reuse fail clearly instead
    of silently triggering expensive model work.

    :param path: Final records JSON path
    :param source_sha256: Hash of the current normalized transcript text
    :return: Reusable artifact, or ``None`` when missing or source-changed
    :raises ConfigError: When an existing artifact is unreadable or unusable
    """
    if not os.path.isfile(path):
        return None

    try:
        artifact = json.loads(read_text(path))
    except (ConfigError, json.JSONDecodeError) as exc:
        raise ConfigError(
            f"Existing records artifact '{path}' is unreadable; "
            "rerun with --force to replace it"
        ) from exc

    if not isinstance(artifact, dict):
        raise ConfigError(
            f"Existing records artifact '{path}' is not a JSON object; "
            "rerun with --force to replace it"
        )
    if not isinstance(artifact.get("records"), list):
        raise ConfigError(
            f"Existing records artifact '{path}' lacks a records list; "
            "rerun with --force to replace it"
        )

    stored_source_sha256 = artifact.get("source_sha256")
    if not isinstance(stored_source_sha256, str) or not stored_source_sha256:
        raise ConfigError(
            f"Existing records artifact '{path}' lacks source_sha256; "
            "rerun with --force to replace it"
        )
    if stored_source_sha256 != source_sha256:
        return None

    return artifact


def _artifact_count(
    artifact: Dict[str, Any],
    key: str,
    default: int = 0,
) -> int:
    """
    Read a non-negative integer artifact field without making it a reuse gate.

    :param artifact: Existing records artifact
    :param key: Artifact field name
    :param default: Value returned for missing or invalid fields
    :return: Non-negative integer value
    """
    value = artifact.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return default



def load_timestamp_reusable_records_artifact(
    path: str,
) -> Dict[str, Any] | None:
    """
    Load an existing records artifact for timestamp-only alignment.

    Timestamp alignment is intentionally independent from extraction cache
    validity. Historical schema versions, partial extraction metadata, source
    hashes, prompt fingerprints, and chunk counters must not trigger an LLM
    rebuild. The only required contract is a JSON object containing a records
    list whose entries are objects with non-empty evidence strings.

    :param path: Final records JSON path
    :return: Timestamp-alignable artifact or ``None``
    """
    if not os.path.isfile(path):
        return None

    try:
        artifact = json.loads(read_text(path))
    except (ConfigError, json.JSONDecodeError) as exc:
        logger.warning(
            "Ignoring unreadable records artifact '%s' during timestamp alignment: %s",
            path,
            exc,
        )
        return None

    if not isinstance(artifact, dict):
        return None
    records = artifact.get("records")
    if not isinstance(records, list):
        return None

    for record_index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            logger.warning(
                "Ignoring records artifact '%s': record %d is not an object",
                path,
                record_index,
            )
            return None
        evidence = record.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            logger.warning(
                "Ignoring records artifact '%s': record %d lacks evidence",
                path,
                record_index,
            )
            return None

    return artifact


def _record_section(record_type: str) -> str:
    """
    Resolve the stable Markdown section for a record type.

    :param record_type: Normalized records schema type
    :return: Human-readable section heading
    """
    return RECORD_TYPE_SECTIONS.get(record_type, "Other")



def _record_timestamp_markdown(record: Dict[str, Any]) -> str | None:
    """
    Build a timestamp display or timestamped YouTube link for one record.

    :param record: Structured record
    :return: Markdown value or ``None`` when no valid timestamp exists
    """
    timestamp = record.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        return None

    display = format_timestamp(float(timestamp))
    video_id = record.get("video_id")
    if isinstance(video_id, str) and VIDEO_ID_RE.fullmatch(video_id):
        seconds = max(0, int(float(timestamp)))
        url = f"https://www.youtube.com/watch?v={video_id}&t={seconds}s"
        return f"[{display}]({url})"
    return display


def render_records_markdown(artifact: Dict[str, Any]) -> str:
    """
    Render one records JSON artifact as deterministic Markdown.

    Rendering is deliberately mechanical. It groups existing records, preserves
    record order within each section, and exposes evidence without performing
    additional summarization, deduplication, or interpretation.

    :param artifact: Complete or partial records artifact
    :return: Markdown document
    :raises ValidationError: If the artifact does not contain a records list
    """
    records = artifact.get("records")
    if not isinstance(records, list):
        raise ValidationError("Records artifact must contain a records list")

    grouped: Dict[str, List[Dict[str, Any]]] = {
        section: [] for section in RECORD_SECTION_ORDER
    }
    for record in records:
        if not isinstance(record, dict):
            continue
        section = _record_section(str(record.get("record_type", "other")))
        grouped[section].append(record)

    lines = [
        "# Meeting Records",
        "",
        f"Source transcript: `{artifact.get('source_transcript', 'unknown')}`",
        "",
        f"Meeting type: `{artifact.get('meeting_type', 'unknown')}`",
        "",
        f"Status: `{artifact.get('status', 'unknown')}`",
        "",
        "This is a derived review artifact, not official meeting minutes. "
        "Each item is rendered directly from the structured records JSON.",
        "",
    ]

    for section in RECORD_SECTION_ORDER:
        section_records = grouped[section]
        if not section_records:
            continue

        lines.extend([f"## {section}", ""])
        for record in section_records:
            subject = format_markdown_inline(record.get("subject", "Untitled record"))
            summary = format_markdown_inline(record.get("summary", ""))
            evidence = format_markdown_inline(record.get("evidence", ""))
            attribution = format_markdown_inline(record.get("attribution", ""))
            source_chunks = record.get("source_chunks", [])

            lines.append(f"- **{subject}**")
            if summary:
                lines.append(f"  - {summary}")
            if attribution and attribution.lower() != "unknown":
                lines.append(f"  - Attribution: {attribution}")
            if evidence:
                lines.append(f"  - Evidence: “{evidence}”")
            timestamp_markdown = _record_timestamp_markdown(record)
            if timestamp_markdown:
                lines.append(f"  - Source timestamp: {timestamp_markdown}")
            if isinstance(source_chunks, list) and source_chunks:
                rendered_chunks = ", ".join(str(chunk) for chunk in source_chunks)
                lines.append(f"  - Source chunks: {rendered_chunks}")
            lines.append("")

    if not any(grouped.values()):
        lines.extend(["No records were extracted.", ""])

    return "\n".join(lines).strip() + "\n"


def write_records_markdown(
    artifact: Dict[str, Any],
    path: str,
) -> None:
    """
    Atomically write a deterministic Markdown view of a records artifact.

    :param artifact: Records artifact to render
    :param path: Markdown destination path
    :return: None
    """
    write_text_atomic(path, render_records_markdown(artifact))


def _count_validation_actions(
    validation_issues: Iterable[Dict[str, Any]],
) -> Dict[str, int]:
    """
    Count deterministic handling outcomes for model validation issues.

    :param validation_issues: Validation and normalization diagnostics
    :return: Warning, normalization, rejected-record, and rejected-line counts
    """
    issues = list(validation_issues)
    return {
        "validation_warning_count": sum(
            issue.get("severity") == "warning" for issue in issues
        ),
        "normalized_record_count": sum(
            issue.get("scope") == "record"
            and issue.get("action") == "normalized"
            for issue in issues
        ),
        "rejected_record_count": sum(
            issue.get("scope") == "record"
            and issue.get("action") == "rejected"
            for issue in issues
        ),
        "rejected_line_count": sum(
            issue.get("scope") == "line"
            and issue.get("action") == "rejected"
            for issue in issues
        ),
    }


def _write_generated_record_chunk(
    generated_chunk: GeneratedChunk,
    *,
    paths: Dict[str, str],
    prompt_id: str,
    parsed_chunks: Dict[int, List[Dict[str, Any]]],
    chunk_statuses: Dict[int, str],
    chunk_validation_issues: Dict[int, List[Dict[str, Any]]],
) -> None:
    """
    Persist raw, validated, and diagnostic artifacts for one generated chunk.

    Chunk-level parse failures and record-level validation failures are retained
    as diagnostics without raising through the generation loop. This preserves
    valid sibling records and allows later chunks and transcripts to continue.

    :param generated_chunk: Completed generated chunk
    :param paths: Resolved transcript artifact paths
    :param prompt_id: Records prompt identifier
    :param parsed_chunks: Mutable collection receiving parsed chunk records
    :param chunk_statuses: Mutable collection receiving chunk completion states
    :param chunk_validation_issues: Mutable collection receiving validation issues
    :return: None
    """
    part_name = f"part_{generated_chunk.chunk_id:03d}"
    raw_path = resolve_contained_path(paths["raw_dir"], f"{part_name}.response.jsonl")
    chunk_path = resolve_contained_path(
        paths["chunks_dir"],
        f"{part_name}.records.json",
    )
    error_path = resolve_contained_path(
        paths["errors_dir"],
        f"{part_name}.validation.json",
    )

    write_text(raw_path, generated_chunk.output.strip() + "\n")

    try:
        records, validation_issues = _parse_records_response_with_issues(
            generated_chunk.output
        )
        # Isolated record or line defects are warnings when usable sibling
        # records remain. A chunk fails only when the response yields no usable
        # records and was not the explicit empty-record response.
        if validation_issues and not records:
            status = "failed"
        else:
            status = "complete"
    except ValidationError as exc:
        records = []
        status = "failed"
        validation_issues = [
            {
                "scope": "chunk",
                "severity": "error",
                "action": "rejected",
                "error": str(exc),
            }
        ]

    parsed_chunks[generated_chunk.chunk_id] = records
    chunk_statuses[generated_chunk.chunk_id] = status
    chunk_validation_issues[generated_chunk.chunk_id] = validation_issues
    validation_counts = _count_validation_actions(validation_issues)

    if validation_issues:
        if status == "complete":
            logger.warning(
                "Records chunk %d/%d completed with %d validation warning(s) "
                "(%d normalized, %d records rejected, %d lines rejected)",
                generated_chunk.chunk_id,
                generated_chunk.chunk_count,
                validation_counts["validation_warning_count"],
                validation_counts["normalized_record_count"],
                validation_counts["rejected_record_count"],
                validation_counts["rejected_line_count"],
            )
        else:
            logger.warning(
                "Records chunk %d/%d failed with %d validation issue(s)",
                generated_chunk.chunk_id,
                generated_chunk.chunk_count,
                len(validation_issues),
            )
        write_text(
            error_path,
            json.dumps(
                {
                    "schema_version": ARTIFACT_SCHEMA_VERSION,
                    "chunk_id": generated_chunk.chunk_id,
                    "prompt_id": prompt_id,
                    "status": status,
                    "validation_issue_count": len(validation_issues),
                    **validation_counts,
                    "validation_issues": validation_issues,
                },
                indent=2,
                ensure_ascii=False,
            ) + "\n",
        )

    write_text(
        chunk_path,
        json.dumps(
            {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "chunk_id": generated_chunk.chunk_id,
                "chunk_count": generated_chunk.chunk_count,
                "prompt_id": prompt_id,
                "status": status,
                "validation_issue_count": len(validation_issues),
                **validation_counts,
                "validation_issues": validation_issues,
                "source_text": generated_chunk.source_text,
                "records": records,
            },
            indent=2,
            ensure_ascii=False,
        ) + "\n",
    )


def extract_records_from_transcript(
    *,
    session: Any,
    llm_runtime: LLMRuntime,
    transcript_path: str,
    transcript: str,
    base_name: str,
    context: str,
    classifier_bundle: Dict[str, Any],
    records_bundle: Dict[str, Any],
    density_keywords: set[str],
    entity_cfg: Dict[str, Any],
    run_cfg: RecordsRunConfig,
    output_dir: str,
    job_profile: str,
    source_sha256: str,
    align_timestamps: bool = False,
    embedding_model: str = "BAAI/bge-small-en-v1.5",
    project_root: str = ".",
) -> Dict[str, Any]:
    """
    Extract and deterministically consolidate records from one transcript.

    :param session: Shared provider session
    :param llm_runtime: Resolved provider runtime
    :param transcript_path: Source transcript path
    :param transcript: Source transcript text
    :param base_name: Transcript basename
    :param context: Human-readable meeting context
    :param classifier_bundle: Shared classifier prompt and allowed labels
    :param records_bundle: Records sidecar prompts and routing
    :param density_keywords: Profile density keywords shared with summary pass one
    :param entity_cfg: Resolved entity configuration
    :param run_cfg: Records-specific runtime configuration
    :param output_dir: Root records output directory
    :param job_profile: Prompt profile name
    :param source_sha256: Hash of the normalized transcript text
    :param align_timestamps: Whether to run optional timestamp enrichment
    :param embedding_model: Configured SentenceTransformer model
    :param project_root: Directory containing the active ``yatsee.toml``
    :return: Records result metadata
    """
    usage = TokenUsage()
    records_router = records_bundle["records_router"]

    if run_cfg.meeting_type:
        if run_cfg.meeting_type not in records_router:
            valid = ", ".join(sorted(records_router))
            raise ValidationError(
                f"Unknown records meeting type '{run_cfg.meeting_type}'. "
                f"Available routes: {valid}"
            )
        meeting_type = run_cfg.meeting_type
    elif run_cfg.disable_auto_classification:
        meeting_type = "general"
    else:
        allowed_meeting_types = classifier_bundle["allowed_meeting_types"]
        allowed_labels = set(allowed_meeting_types)
        single_label = resolve_single_classifier_label(allowed_labels)

        if single_label:
            meeting_type = single_label
            logger.info(
                "Selected sole configured meeting type without classification: %s",
                meeting_type,
            )
        else:
            classifier_prompt_template = classifier_bundle["classifier_prompt"]
            classifier_prompt = classifier_prompt_template.format(
                context=context,
                text=transcript[:2000],
            )
            meeting_type = classify_transcript(
                session=session,
                runtime=llm_runtime,
                prompt=classifier_prompt,
                allowed_labels=allowed_labels,
            )
            usage.add(
                input_tokens=estimate_token_count(classifier_prompt),
                output_tokens=estimate_token_count(meeting_type),
            )

    prompt_id = resolve_records_prompt_id(meeting_type, records_router)
    prompt_template = records_bundle["prompts"][prompt_id]
    paths = _artifact_paths(output_dir, base_name)
    _prepare_artifact_directory(paths)

    parsed_chunks: Dict[int, List[Dict[str, Any]]] = {}
    chunk_statuses: Dict[int, str] = {}
    chunk_validation_issues: Dict[int, List[Dict[str, Any]]] = {}
    chunk_writer = partial(
        _write_generated_record_chunk,
        paths=paths,
        prompt_id=prompt_id,
        parsed_chunks=parsed_chunks,
        chunk_statuses=chunk_statuses,
        chunk_validation_issues=chunk_validation_issues,
    )

    generated_chunks, generation_usage = generate_chunk_outputs(
        session=session,
        runtime=llm_runtime,
        text=transcript,
        prompt_id=prompt_id,
        prompt_template=prompt_template,
        context=context,
        chunk_style=run_cfg.chunk_style,
        max_tokens=run_cfg.max_tokens,
        density_keywords=density_keywords,
        known_name_permutations=build_name_permutations(entity_cfg),
        stage_label="Records extraction",
        on_chunk=chunk_writer,
    )
    usage.add(
        input_tokens=generation_usage.input_tokens,
        output_tokens=generation_usage.output_tokens,
    )

    consolidated = consolidate_records(
        (chunk_id, parsed_chunks[chunk_id])
        for chunk_id in sorted(parsed_chunks)
    )
    chunk_count = len(generated_chunks)
    successful_chunk_count = sum(
        status == "complete" for status in chunk_statuses.values()
    )
    partial_chunk_count = sum(
        status == "partial" for status in chunk_statuses.values()
    )
    failed_chunk_count = sum(
        status == "failed" for status in chunk_statuses.values()
    )
    validation_issue_count = sum(
        len(issues) for issues in chunk_validation_issues.values()
    )
    validation_counts = _count_validation_actions(
        issue
        for issues in chunk_validation_issues.values()
        for issue in issues
    )
    status = (
        "complete"
        if chunk_count > 0
        and successful_chunk_count == chunk_count
        and partial_chunk_count == 0
        and failed_chunk_count == 0
        else "partial"
    )

    final_artifact = {
        "artifact_kind": ARTIFACT_KIND,
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "extraction_version": EXTRACTION_VERSION,
        "status": status,
        "source_transcript": serialize_project_path(
            transcript_path,
            project_root,
        ),
        "source_sha256": source_sha256,
        "meeting_type": meeting_type,
        "job_profile": job_profile,
        "prompt_id": prompt_id,
        "classifier_file": serialize_project_path(
            classifier_bundle["path"],
            project_root,
        ),
        "prompt_file": serialize_project_path(
            records_bundle["path"],
            project_root,
        ),
        "provider": llm_runtime.provider_name,
        "model": llm_runtime.model,
        "num_ctx": llm_runtime.num_ctx,
        "records_max_tokens": run_cfg.max_tokens,
        "chunk_style": run_cfg.chunk_style,
        "chunk_max_tokens": run_cfg.max_tokens,
        "chunk_count": chunk_count,
        "successful_chunk_count": successful_chunk_count,
        "partial_chunk_count": partial_chunk_count,
        "failed_chunk_count": failed_chunk_count,
        "validation_issue_count": validation_issue_count,
        **validation_counts,
        "record_count": len(consolidated),
        "records": consolidated,
    }

    timestamp_result = _timestamp_result_metadata(
        status=("skipped" if align_timestamps else "not_requested"),
        message=(
            "records extraction was not complete"
            if align_timestamps
            else "timestamp alignment was not requested"
        ),
    )
    if status == "complete" and align_timestamps:
        (
            final_artifact,
            timestamp_result,
            _,
        ) = _enrich_records_with_timestamps(
            artifact=final_artifact,
            paths=paths,
            transcript_path=transcript_path,
            base_name=base_name,
            entity_cfg=entity_cfg,
            embedding_model=embedding_model,
            project_root=project_root,
        )

    if status == "complete":
        output_path = paths["final_path"]
        markdown_path = paths["markdown_path"]
        write_text_atomic(
            output_path,
            json.dumps(final_artifact, indent=2, ensure_ascii=False) + "\n",
        )
        write_records_markdown(final_artifact, markdown_path)

        for stale_partial_path in (
            paths["partial_path"],
            paths["partial_markdown_path"],
        ):
            try:
                if os.path.isfile(stale_partial_path):
                    os.unlink(stale_partial_path)
            except OSError as exc:
                logger.warning(
                    "Failed to remove stale partial records artifact '%s': %s",
                    stale_partial_path,
                    exc,
                )
        if not align_timestamps or timestamp_result["timestamp_alignment_status"] in {
            "skipped",
            "failed",
        }:
            try:
                if os.path.isfile(paths["alignment_path"]):
                    os.unlink(paths["alignment_path"])
            except OSError as exc:
                logger.warning(
                    "Failed to remove stale timestamp alignment artifact '%s': %s",
                    paths["alignment_path"],
                    exc,
                )
        cache_updated = True
    else:
        output_path = paths["partial_path"]
        markdown_path = paths["partial_markdown_path"]
        write_text_atomic(
            output_path,
            json.dumps(final_artifact, indent=2, ensure_ascii=False) + "\n",
        )
        write_records_markdown(final_artifact, markdown_path)
        cache_updated = False

    pricing = build_pricing_summary(
        show_pricing=run_cfg.show_pricing,
        actual_provider=llm_runtime.provider_name,
        actual_model=llm_runtime.model,
        pricing_provider=run_cfg.pricing_provider,
        pricing_model=run_cfg.pricing_model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )

    return {
        "base_name": base_name,
        "meeting_type": meeting_type,
        "prompt_id": prompt_id,
        "output_path": output_path,
        "markdown_path": markdown_path,
        "status": status,
        "chunk_count": chunk_count,
        "successful_chunk_count": successful_chunk_count,
        "partial_chunk_count": partial_chunk_count,
        "failed_chunk_count": failed_chunk_count,
        "validation_issue_count": validation_issue_count,
        **validation_counts,
        "record_count": len(consolidated),
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "pricing": pricing,
        "cache_updated": cache_updated,
        "reused": False,
        "skipped": False,
        **timestamp_result,
    }


def _run_timestamp_alignment_only(
    *,
    resolved: Dict[str, Any],
    entity_cfg: Dict[str, Any],
    embedding_model: str,
    project_root: str,
) -> Dict[str, Any]:
    """
    Recalculate timestamps for existing records without running extraction.

    This stage boundary is deliberate. Timestamp enrichment is deterministic
    downstream work over already-generated records. Missing, unreadable, or
    historically incompatible records artifacts are reported and skipped rather
    than routed back into prompt loading, provider resolution, chunk generation,
    or LLM execution.

    :param resolved: Resolved records input and output paths
    :param entity_cfg: Resolved entity configuration
    :param embedding_model: Configured SentenceTransformer model
    :param project_root: Directory containing the active ``yatsee.toml``
    :return: Forced timestamp-realignment-only result metadata
    """
    results: List[Dict[str, Any]] = []
    existing_count = 0
    aligned_count = 0
    reused_alignment_count = 0
    skipped_count = 0
    failed_count = 0

    for file_path in resolved["file_list"]:
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        paths = _artifact_paths(resolved["output_dir"], base_name)
        artifact_exists = os.path.isfile(paths["final_path"])
        artifact = load_timestamp_reusable_records_artifact(
            paths["final_path"],
        )

        if artifact is None:
            skipped_count += 1
            message = (
                "records artifact is unreadable or lacks a usable records/evidence structure"
                if artifact_exists
                else "records artifact not found"
            )
            results.append(
                {
                    "base_name": base_name,
                    "output_path": paths["final_path"],
                    "markdown_path": paths["markdown_path"],
                    "records_artifact_status": "unusable" if artifact_exists else "missing",
                    "record_count": 0,
                    "timestamp_alignment_status": "skipped",
                    "timestamp_alignment_message": message,
                    "timestamp_alignment_path": None,
                    "timestamp_matched_record_count": 0,
                    "timestamp_ambiguous_record_count": 0,
                    "timestamp_unmatched_record_count": 0,
                }
            )
            continue

        existing_count += 1

        # Keep serialized paths portable without rewriting extraction identity
        # or consulting current prompts, models, hashes, and chunk settings.
        current_path_metadata = {
            "source_transcript": serialize_project_path(
                file_path,
                project_root,
            ),
        }
        for key in ("classifier_file", "prompt_file"):
            value = artifact.get(key)
            if isinstance(value, str) and value.strip():
                current_path_metadata[key] = serialize_project_path(
                    value,
                    project_root,
                )

        artifact_changed = any(
            artifact.get(key) != value
            for key, value in current_path_metadata.items()
        )
        if artifact_changed:
            artifact.update(current_path_metadata)

        artifact, timestamp_result, timestamp_changed = (
            _enrich_records_with_timestamps(
                artifact=artifact,
                paths=paths,
                transcript_path=file_path,
                base_name=base_name,
                entity_cfg=entity_cfg,
                embedding_model=embedding_model,
                project_root=project_root,
                force_realign=True,
            )
        )
        artifact_changed = artifact_changed or timestamp_changed

        if artifact_changed:
            write_text_atomic(
                paths["final_path"],
                json.dumps(
                    artifact,
                    indent=2,
                    ensure_ascii=False,
                ) + "\n",
            )
        write_records_markdown(artifact, paths["markdown_path"])

        alignment_status = timestamp_result["timestamp_alignment_status"]
        if alignment_status == "complete":
            aligned_count += 1
        elif alignment_status == "reused":
            reused_alignment_count += 1
        elif alignment_status == "failed":
            failed_count += 1
        else:
            skipped_count += 1

        results.append(
            {
                "base_name": base_name,
                "output_path": paths["final_path"],
                "markdown_path": paths["markdown_path"],
                "records_artifact_status": "existing",
                "record_count": len(artifact["records"]),
                **timestamp_result,
            }
        )

    return {
        "mode": "timestamp_only",
        "artifact_kind": ARTIFACT_KIND,
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "input_dir": resolved["input_dir"],
        "output_dir": resolved["output_dir"],
        "discovered": len(resolved["file_list"]),
        "existing_records_artifacts": existing_count,
        "extracted": 0,
        "reused": existing_count,
        "aligned": aligned_count,
        "alignment_reused": reused_alignment_count,
        "skipped": skipped_count,
        "failed": failed_count,
        "timestamp_alignment_requested": True,
        "embedding_model": embedding_model,
        "results": results,
    }


def run_records_stage(
    *,
    global_config_path: str,
    entity: str | None = None,
    input_path: str | None = None,
    output_dir: str | None = None,
    model: str | None = None,
    llm_provider: str | None = None,
    llm_provider_url: str | None = None,
    llm_api_key: str | None = None,
    show_pricing: bool = False,
    no_show_pricing: bool = False,
    pricing_provider: str | None = None,
    pricing_model: str | None = None,
    job_profile: str = "civic",
    chunk_style: str = "density",
    max_words: int | None = None,
    max_tokens: int | None = None,
    disable_auto_classification: bool = False,
    meeting_type: str | None = None,
    context: str = "",
    print_prompts: bool = False,
    force: bool = False,
    align_timestamps: bool = False,
    timestamp_only: bool = False,
) -> Dict[str, Any]:
    """
    Run structured records extraction across normalized transcript files.

    :param global_config_path: Path to global ``yatsee.toml``
    :param entity: Optional entity handle
    :param input_path: Optional normalized transcript file or directory
    :param output_dir: Optional records output directory
    :param model: Optional records model override
    :param llm_provider: Optional provider override
    :param llm_provider_url: Optional provider URL or executable override
    :param llm_api_key: Optional provider API key override
    :param show_pricing: Whether to enable reference pricing
    :param no_show_pricing: Whether to disable configured pricing
    :param pricing_provider: Optional reference pricing provider
    :param pricing_model: Optional reference pricing model
    :param job_profile: Prompt profile name
    :param chunk_style: Chunk boundary strategy
    :param max_words: Optional approximate words per source chunk
    :param max_tokens: Optional approximate tokens per source chunk
    :param disable_auto_classification: Whether to use the general route directly
    :param meeting_type: Optional explicit records route
    :param context: Optional additional meeting context
    :param print_prompts: Whether to return records prompts without execution
    :param force: Whether to rebuild records regardless of normalized source hash
    :param align_timestamps: Whether to align reused and newly extracted records
    :param timestamp_only: Whether to force-realign existing records without extraction
    :return: Records-stage result metadata
    """
    if align_timestamps and timestamp_only:
        raise ConfigError(
            "--align-timestamps cannot be combined with --timestamp-only"
        )
    if force and timestamp_only:
        raise ConfigError(
            "--force cannot be combined with --timestamp-only"
        )
    if print_prompts and (align_timestamps or timestamp_only):
        raise ConfigError(
            "--print-prompts cannot be combined with timestamp alignment options"
        )

    resolved = resolve_record_paths(
        global_config_path,
        entity=entity,
        input_path=input_path,
        output_dir=output_dir,
        model=model,
        max_words=max_words,
        max_tokens=max_tokens,
    )
    project_root = str(
        Path(global_config_path).expanduser().resolve().parent
    )
    global_cfg = resolved["global_cfg"]
    entity_cfg = resolved["entity_cfg"]
    system_cfg = global_cfg.get("system", {})
    embedding_model = (
        entity_cfg.get("embedding_model")
        or system_cfg.get(
            "default_embedding_model",
            "BAAI/bge-small-en-v1.5",
        )
    )

    if timestamp_only:
        return _run_timestamp_alignment_only(
            resolved=resolved,
            entity_cfg=entity_cfg,
            embedding_model=embedding_model,
            project_root=project_root,
        )

    classifier_bundle = load_classifier_bundle(entity_cfg, job_profile)
    summary_bundle = load_summary_prompt_bundle(entity_cfg, job_profile)
    records_bundle = load_records_prompt_bundle(entity_cfg, job_profile)
    density_keywords = set(
        summary_bundle["density_keywords"].get("keywords", [])
    )

    if print_prompts:
        return {
            "mode": "print_prompts",
            "job_profile": job_profile,
            "classifier_file": serialize_project_path(
                classifier_bundle["path"],
                project_root,
            ),
            "used_fallback_classifier": classifier_bundle["fallback"],
            "classifier_prompt": classifier_bundle["classifier_prompt"],
            "prompt_file": serialize_project_path(
                records_bundle["path"],
                project_root,
            ),
            "density_prompt_file": serialize_project_path(
                summary_bundle["path"],
                project_root,
            ),
            "density_keywords": sorted(density_keywords),
            "records_router": records_bundle["records_router"],
            "prompts": records_bundle["prompts"],
        }

    llm_runtime = resolve_llm_runtime(
        system_cfg=system_cfg,
        model=resolved["model"],
        num_ctx=resolved["num_ctx"],
        provider_name=llm_provider,
        provider_target=llm_provider_url,
        api_key=llm_api_key,
    )

    resolved_show_pricing = bool(system_cfg.get("show_pricing", False))
    if show_pricing:
        resolved_show_pricing = True
    if no_show_pricing:
        resolved_show_pricing = False

    resolved_pricing_provider = (
        pricing_provider
        if pricing_provider is not None
        else system_cfg.get("pricing_provider")
    )
    resolved_pricing_model = (
        pricing_model
        if pricing_model is not None
        else system_cfg.get("pricing_model")
    )

    run_cfg = RecordsRunConfig(
        chunk_style=chunk_style,
        max_tokens=resolved["max_tokens"],
        disable_auto_classification=disable_auto_classification,
        meeting_type=meeting_type,
        show_pricing=resolved_show_pricing,
        pricing_provider=resolved_pricing_provider,
        pricing_model=resolved_pricing_model,
    )

    results_by_index: Dict[int, Dict[str, Any]] = {}
    pending_jobs: List[Dict[str, Any]] = []
    reused_count = 0

    for file_index, file_path in enumerate(resolved["file_list"]):
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        transcript = read_text(file_path)
        resolved_context = extract_context_from_filename(file_path, context)
        source_sha256 = compute_text_sha256(transcript)
        paths = _artifact_paths(resolved["output_dir"], base_name)

        reusable_artifact = None
        reuse_mode = None
        if not force:
            reusable_artifact = load_reusable_records_artifact(
                paths["final_path"],
                source_sha256=source_sha256,
            )
            if reusable_artifact is not None:
                reuse_mode = "source_match"

        if reusable_artifact is not None:
            # Preserve extraction provenance while keeping the source path portable.
            current_path_metadata = {
                "source_transcript": serialize_project_path(
                    file_path,
                    project_root,
                ),
            }
            artifact_changed = any(
                reusable_artifact.get(key) != value
                for key, value in current_path_metadata.items()
            )
            if artifact_changed:
                reusable_artifact.update(current_path_metadata)

            timestamp_result = _timestamp_result_metadata(
                status="not_requested",
                message="timestamp alignment was not requested",
            )
            if align_timestamps:
                reusable_artifact, timestamp_result, timestamp_changed = (
                    _enrich_records_with_timestamps(
                        artifact=reusable_artifact,
                        paths=paths,
                        transcript_path=file_path,
                        base_name=base_name,
                        entity_cfg=entity_cfg,
                        embedding_model=embedding_model,
                        project_root=project_root,
                    )
                )
                artifact_changed = artifact_changed or timestamp_changed

            if artifact_changed:
                write_text_atomic(
                    paths["final_path"],
                    json.dumps(
                        reusable_artifact,
                        indent=2,
                        ensure_ascii=False,
                    ) + "\n",
                )
            write_records_markdown(reusable_artifact, paths["markdown_path"])
            reused_count += 1
            validation_issue_count = _artifact_count(
                reusable_artifact,
                "validation_issue_count",
            )
            results_by_index[file_index] = {
                "base_name": base_name,
                "meeting_type": reusable_artifact.get(
                    "meeting_type",
                    "unknown",
                ),
                "prompt_id": reusable_artifact.get(
                    "prompt_id",
                    "unknown",
                ),
                "output_path": paths["final_path"],
                "markdown_path": paths["markdown_path"],
                "status": reusable_artifact.get("status", "unknown"),
                "chunk_count": _artifact_count(
                    reusable_artifact,
                    "chunk_count",
                ),
                "successful_chunk_count": _artifact_count(
                    reusable_artifact,
                    "successful_chunk_count",
                ),
                "partial_chunk_count": _artifact_count(
                    reusable_artifact,
                    "partial_chunk_count",
                ),
                "failed_chunk_count": _artifact_count(
                    reusable_artifact,
                    "failed_chunk_count",
                ),
                "validation_issue_count": validation_issue_count,
                "validation_warning_count": _artifact_count(
                    reusable_artifact,
                    "validation_warning_count",
                    validation_issue_count,
                ),
                "normalized_record_count": _artifact_count(
                    reusable_artifact,
                    "normalized_record_count",
                ),
                "rejected_record_count": _artifact_count(
                    reusable_artifact,
                    "rejected_record_count",
                ),
                "rejected_line_count": _artifact_count(
                    reusable_artifact,
                    "rejected_line_count",
                ),
                "record_count": len(reusable_artifact["records"]),
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "pricing": {"enabled": False},
                "cache_updated": False,
                "reused": True,
                "reuse_mode": reuse_mode,
                "skipped": True,
                **timestamp_result,
            }
            continue

        pending_jobs.append(
            {
                "file_index": file_index,
                "file_path": file_path,
                "base_name": base_name,
                "transcript": transcript,
                "context": resolved_context,
                "source_sha256": source_sha256,
            }
        )

    if pending_jobs:
        with create_llm_session() as session:
            for job in pending_jobs:
                result = extract_records_from_transcript(
                    session=session,
                    llm_runtime=llm_runtime,
                    transcript_path=job["file_path"],
                    transcript=job["transcript"],
                    base_name=job["base_name"],
                    context=job["context"],
                    classifier_bundle=classifier_bundle,
                    records_bundle=records_bundle,
                    density_keywords=density_keywords,
                    entity_cfg=entity_cfg,
                    run_cfg=run_cfg,
                    output_dir=resolved["output_dir"],
                    job_profile=job_profile,
                    source_sha256=job["source_sha256"],
                    align_timestamps=align_timestamps,
                    embedding_model=embedding_model,
                    project_root=project_root,
                )
                results_by_index[job["file_index"]] = result

    results = [
        results_by_index[file_index]
        for file_index in range(len(resolved["file_list"]))
    ]

    return {
        "mode": "run",
        "artifact_kind": ARTIFACT_KIND,
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "input_dir": resolved["input_dir"],
        "output_dir": resolved["output_dir"],
        "llm_provider": llm_runtime.provider_name,
        "model": llm_runtime.model,
        "classifier_file": serialize_project_path(
            classifier_bundle["path"],
            project_root,
        ),
        "used_fallback_classifier": classifier_bundle["fallback"],
        "prompt_file": serialize_project_path(
            records_bundle["path"],
            project_root,
        ),
        "density_prompt_file": serialize_project_path(
            summary_bundle["path"],
            project_root,
        ),
        "density_keyword_count": len(density_keywords),
        "discovered": len(results),
        "extracted": len(pending_jobs),
        "reused": reused_count,
        "timestamp_alignment_requested": align_timestamps,
        "embedding_model": embedding_model if align_timestamps else None,
        "results": results,
    }
