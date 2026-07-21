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
import shutil
from dataclasses import dataclass
from functools import partial
from typing import Any, Dict, Iterable, List

from yatsee.core.errors import ConfigError, ValidationError
from yatsee.core.hashing import compute_json_sha256, compute_text_sha256
from yatsee.core.io import ensure_directory, read_text, write_text, write_text_atomic
from yatsee.core.paths import resolve_contained_path, sanitize_path_component
from yatsee.intel.classifier import classify_transcript
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
from yatsee.intel.speakers import build_name_permutations
from yatsee.providers.pricing import build_pricing_summary
from yatsee.providers.tokenization import estimate_token_count

logger = logging.getLogger(__name__)

ARTIFACT_KIND = "records"
ARTIFACT_SCHEMA_VERSION = "civic_records_v1"
EXTRACTION_VERSION = "records_v2"
FINAL_SUFFIX = ".records.json"
PARTIAL_SUFFIX = ".records.partial.json"
MARKDOWN_SUFFIX = ".records.md"
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

ALLOWED_RECORD_TYPES = {
    "attendance",
    "procedural_action",
    "presentation",
    "discussion",
    "motion",
    "vote",
    "public_comment",
    "financial",
    "metric",
    "ordinance",
    "resolution",
    "contract",
    "project",
    "appointment",
    "proclamation",
    "recognition",
    "follow_up",
    "other",
}


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


def build_records_extraction_metadata(
    *,
    llm_runtime: LLMRuntime,
    run_cfg: RecordsRunConfig,
    job_profile: str,
    context: str,
    classifier_bundle: Dict[str, Any],
    records_bundle: Dict[str, Any],
    density_keywords: set[str],
    entity_cfg: Dict[str, Any],
) -> Dict[str, str]:
    """
    Build stable metadata describing every records extraction input.

    The resulting fingerprint intentionally excludes output paths, credentials,
    pricing settings, logging options, and provider target URLs. Those values do
    not change the requested extraction semantics and should not force expensive
    model work to repeat.

    :param llm_runtime: Resolved provider runtime
    :param run_cfg: Records-specific runtime settings
    :param job_profile: Prompt profile name
    :param context: Resolved transcript context included in prompts
    :param classifier_bundle: Loaded classifier bundle
    :param records_bundle: Loaded records prompt bundle
    :param density_keywords: Density keywords passed to the chunker
    :param entity_cfg: Resolved entity configuration
    :return: Component hashes and the combined extraction fingerprint
    """
    classifier_payload = {
        "classifier_prompt": classifier_bundle["classifier_prompt"],
        "allowed_meeting_types": classifier_bundle["allowed_meeting_types"],
    }
    records_prompt_payload = {
        "records_router": records_bundle["records_router"],
        "prompts": records_bundle["prompts"],
    }
    density_payload = sorted(density_keywords)
    entity_context_payload = build_name_permutations(entity_cfg)

    classifier_sha256 = compute_json_sha256(classifier_payload)
    records_prompt_sha256 = compute_json_sha256(records_prompt_payload)
    density_keywords_sha256 = compute_json_sha256(density_payload)
    entity_context_sha256 = compute_json_sha256(entity_context_payload)
    context_sha256 = compute_text_sha256(context)

    fingerprint_payload = {
        "extraction_version": EXTRACTION_VERSION,
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "job_profile": job_profile,
        "provider": llm_runtime.provider_name,
        "model": llm_runtime.model,
        "num_ctx": llm_runtime.num_ctx,
        "max_output_tokens": llm_runtime.max_output_tokens,
        "temperature": llm_runtime.temperature,
        "chunk_style": run_cfg.chunk_style,
        "chunk_max_tokens": run_cfg.max_tokens,
        "disable_auto_classification": run_cfg.disable_auto_classification,
        "meeting_type_override": run_cfg.meeting_type,
        "context": context,
        "classifier_sha256": classifier_sha256,
        "records_prompt_sha256": records_prompt_sha256,
        "density_keywords_sha256": density_keywords_sha256,
        "entity_context_sha256": entity_context_sha256,
    }

    return {
        "extraction_version": EXTRACTION_VERSION,
        "extraction_fingerprint": compute_json_sha256(fingerprint_payload),
        "classifier_sha256": classifier_sha256,
        "records_prompt_sha256": records_prompt_sha256,
        "density_keywords_sha256": density_keywords_sha256,
        "entity_context_sha256": entity_context_sha256,
        "context_sha256": context_sha256,
    }


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


def _normalize_record(raw_record: Any, record_index: int) -> Dict[str, Any]:
    """
    Validate and normalize one model-produced record.

    :param raw_record: Parsed JSON value for one record
    :param record_index: One-based record position for diagnostics
    :return: Normalized record dictionary
    :raises ValidationError: If the record does not satisfy the schema
    """
    if not isinstance(raw_record, dict):
        raise ValidationError(f"Record {record_index} must be a JSON object")

    cleaned = _clean_value(raw_record)
    raw_record_type = cleaned.get("record_type")
    record_type = (
        raw_record_type.strip().lower().replace(" ", "_")
        if isinstance(raw_record_type, str)
        else raw_record_type
    )
    if record_type not in ALLOWED_RECORD_TYPES:
        allowed = ", ".join(sorted(ALLOWED_RECORD_TYPES))
        raise ValidationError(
            f"Record {record_index} has invalid record_type '{record_type}'. "
            f"Allowed values: {allowed}"
        )

    for key in ("subject", "summary", "evidence"):
        if not isinstance(cleaned.get(key), str) or not cleaned[key].strip():
            raise ValidationError(
                f"Record {record_index} must define a non-empty '{key}' string"
            )

    attributes = cleaned.get("attributes", {})
    if attributes and not isinstance(attributes, dict):
        raise ValidationError(f"Record {record_index} attributes must be an object")
    attributes = dict(attributes or {})

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

    return normalized


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
            normalized_records.append(_normalize_record(raw_record, record_index))
        except ValidationError as exc:
            issue = {
                "scope": "record",
                "record_index": record_index,
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
    if validation_issues:
        raise ValidationError(validation_issues[0]["error"])
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


def load_reusable_records_artifact(
    path: str,
    *,
    source_sha256: str,
    extraction_fingerprint: str,
) -> Dict[str, Any] | None:
    """
    Load a complete final artifact when it matches the current extraction inputs.

    Invalid, legacy, partial, or stale artifacts are treated as cache misses. A
    cache miss is not an error because the records workflow can rebuild the
    artifact from the normalized transcript.

    :param path: Final records JSON path
    :param source_sha256: Hash of the normalized transcript text
    :param extraction_fingerprint: Hash of extraction-relevant settings
    :return: Reusable artifact or ``None``
    """
    if not os.path.isfile(path):
        return None

    try:
        artifact = json.loads(read_text(path))
    except (ConfigError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable records cache '%s': %s", path, exc)
        return None

    if not isinstance(artifact, dict):
        return None
    if artifact.get("artifact_kind") != ARTIFACT_KIND:
        return None
    if artifact.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        return None
    if artifact.get("extraction_version") != EXTRACTION_VERSION:
        return None
    if artifact.get("status") != "complete":
        return None
    if artifact.get("source_sha256") != source_sha256:
        return None
    if artifact.get("extraction_fingerprint") != extraction_fingerprint:
        return None

    records = artifact.get("records")
    if not isinstance(records, list):
        return None
    if artifact.get("record_count") != len(records):
        return None
    for record in records:
        if not isinstance(record, dict):
            return None
        if record.get("record_type") not in ALLOWED_RECORD_TYPES:
            return None
        for key in ("subject", "summary", "evidence"):
            if not isinstance(record.get(key), str) or not record[key].strip():
                return None
        source_chunks = record.get("source_chunks")
        if not isinstance(source_chunks, list) or not source_chunks:
            return None
        if not all(isinstance(chunk_id, int) and chunk_id > 0 for chunk_id in source_chunks):
            return None

    chunk_count = artifact.get("chunk_count")
    successful_chunk_count = artifact.get("successful_chunk_count")
    if not isinstance(chunk_count, int) or chunk_count < 1:
        return None
    if successful_chunk_count != chunk_count:
        return None
    if artifact.get("partial_chunk_count") != 0:
        return None
    if artifact.get("failed_chunk_count") != 0:
        return None
    if artifact.get("validation_issue_count") != 0:
        return None

    return artifact


def _record_section(record_type: str) -> str:
    """
    Resolve the stable Markdown section for a record type.

    :param record_type: Normalized records schema type
    :return: Human-readable section heading
    """
    return RECORD_TYPE_SECTIONS.get(record_type, "Other")


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
            subject = str(record.get("subject", "Untitled record")).strip()
            summary = str(record.get("summary", "")).strip()
            evidence = str(record.get("evidence", "")).strip()
            attribution = str(record.get("attribution", "")).strip()
            source_chunks = record.get("source_chunks", [])

            lines.append(f"- **{subject}**")
            if summary:
                lines.append(f"  - {summary}")
            if attribution and attribution.lower() != "unknown":
                lines.append(f"  - Attribution: {attribution}")
            if evidence:
                lines.append(f"  - Evidence: “{evidence}”")
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
        if validation_issues and records:
            status = "partial"
        elif validation_issues:
            status = "failed"
        else:
            status = "complete"
    except ValidationError as exc:
        records = []
        status = "failed"
        validation_issues = [
            {
                "scope": "chunk",
                "error": str(exc),
            }
        ]

    parsed_chunks[generated_chunk.chunk_id] = records
    chunk_statuses[generated_chunk.chunk_id] = status
    chunk_validation_issues[generated_chunk.chunk_id] = validation_issues

    if validation_issues:
        logger.warning(
            "Records chunk %d/%d completed with status %s and %d validation issue(s)",
            generated_chunk.chunk_id,
            generated_chunk.chunk_count,
            status,
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
    extraction_metadata: Dict[str, str],
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
    :param extraction_metadata: Stable extraction hashes and fingerprint
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
        classifier_prompt_template = classifier_bundle["classifier_prompt"]
        allowed_meeting_types = classifier_bundle["allowed_meeting_types"]
        classifier_prompt = classifier_prompt_template.format(
            context=context,
            text=transcript[:2000],
        )
        meeting_type = classify_transcript(
            session=session,
            runtime=llm_runtime,
            prompt=classifier_prompt,
            allowed_labels=set(allowed_meeting_types),
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
    status = (
        "complete"
        if chunk_count > 0
        and successful_chunk_count == chunk_count
        and partial_chunk_count == 0
        and failed_chunk_count == 0
        and validation_issue_count == 0
        else "partial"
    )

    final_artifact = {
        "artifact_kind": ARTIFACT_KIND,
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        **extraction_metadata,
        "status": status,
        "source_transcript": transcript_path,
        "source_sha256": source_sha256,
        "meeting_type": meeting_type,
        "job_profile": job_profile,
        "prompt_id": prompt_id,
        "classifier_file": classifier_bundle["path"],
        "prompt_file": records_bundle["path"],
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
        "record_count": len(consolidated),
        "records": consolidated,
    }

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
        "record_count": len(consolidated),
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "pricing": pricing,
        "cache_updated": cache_updated,
        "reused": False,
        "skipped": False,
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
    :return: Records-stage result metadata
    """
    resolved = resolve_record_paths(
        global_config_path,
        entity=entity,
        input_path=input_path,
        output_dir=output_dir,
        model=model,
        max_words=max_words,
        max_tokens=max_tokens,
    )
    global_cfg = resolved["global_cfg"]
    entity_cfg = resolved["entity_cfg"]
    system_cfg = global_cfg.get("system", {})

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
            "classifier_file": classifier_bundle["path"],
            "used_fallback_classifier": classifier_bundle["fallback"],
            "classifier_prompt": classifier_bundle["classifier_prompt"],
            "prompt_file": records_bundle["path"],
            "density_prompt_file": summary_bundle["path"],
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
        extraction_metadata = build_records_extraction_metadata(
            llm_runtime=llm_runtime,
            run_cfg=run_cfg,
            job_profile=job_profile,
            context=resolved_context,
            classifier_bundle=classifier_bundle,
            records_bundle=records_bundle,
            density_keywords=density_keywords,
            entity_cfg=entity_cfg,
        )
        paths = _artifact_paths(resolved["output_dir"], base_name)

        reusable_artifact = None
        if not force:
            reusable_artifact = load_reusable_records_artifact(
                paths["final_path"],
                source_sha256=source_sha256,
                extraction_fingerprint=extraction_metadata[
                    "extraction_fingerprint"
                ],
            )

        if reusable_artifact is not None:
            # The content identity is unchanged, but paths may move between
            # equivalent direct-path runs. Refresh descriptive path metadata
            # without invalidating or rerunning the extraction.
            current_path_metadata = {
                "source_transcript": file_path,
                "classifier_file": classifier_bundle["path"],
                "prompt_file": records_bundle["path"],
            }
            if any(
                reusable_artifact.get(key) != value
                for key, value in current_path_metadata.items()
            ):
                reusable_artifact.update(current_path_metadata)
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
            results_by_index[file_index] = {
                "base_name": base_name,
                "meeting_type": reusable_artifact["meeting_type"],
                "prompt_id": reusable_artifact["prompt_id"],
                "output_path": paths["final_path"],
                "markdown_path": paths["markdown_path"],
                "status": reusable_artifact["status"],
                "chunk_count": reusable_artifact["chunk_count"],
                "successful_chunk_count": reusable_artifact[
                    "successful_chunk_count"
                ],
                "partial_chunk_count": reusable_artifact[
                    "partial_chunk_count"
                ],
                "failed_chunk_count": reusable_artifact["failed_chunk_count"],
                "validation_issue_count": reusable_artifact[
                    "validation_issue_count"
                ],
                "record_count": reusable_artifact["record_count"],
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "pricing": {"enabled": False},
                "cache_updated": False,
                "reused": True,
                "skipped": True,
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
                "extraction_metadata": extraction_metadata,
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
                    extraction_metadata=job["extraction_metadata"],
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
        "classifier_file": classifier_bundle["path"],
        "used_fallback_classifier": classifier_bundle["fallback"],
        "prompt_file": records_bundle["path"],
        "density_prompt_file": summary_bundle["path"],
        "density_keyword_count": len(density_keywords),
        "discovered": len(results),
        "extracted": len(pending_jobs),
        "reused": reused_count,
        "results": results,
    }
