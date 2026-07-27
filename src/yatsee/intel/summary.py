"""
Records/Events summary orchestration for YATSEE intelligence jobs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List

from yatsee.core.errors import ConfigError, ValidationError
from yatsee.intel.llm import (
    GeneratedChunk,
    LLMRuntime,
    TokenUsage,
    generate_chunk_outputs,
    llm_generate_with_runtime,
)
from yatsee.intel.prompts import resolve_summary_prompt_ids
from yatsee.intel.speakers import build_name_permutations
from yatsee.intel.writers import write_chunk_file, write_summary_file
from yatsee.providers.pricing import build_pricing_summary
from yatsee.providers.tokenization import estimate_token_count

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SummaryRunConfig:
    """
    Hold summary-specific runtime settings for one or more meetings.

    Provider configuration, resolved paths, and prompt bundle loading remain
    outside this object. These fields define only the behavior owned by the
    summary workflow itself.

    :param chunk_style: Chunk boundary strategy
    :param max_tokens: Approximate structured-input token budget per chunk
    :param max_pass: Maximum number of summary reduction passes
    :param output_format: Final summary artifact format
    :param first_prompt: Optional first-pass prompt override
    :param second_prompt: Optional multi-pass prompt override
    :param final_prompt: Optional final synthesis prompt override
    :param enable_chunk_writer: Whether to persist intermediate summary chunks
    :param show_pricing: Whether to calculate reference pricing
    :param pricing_provider: Optional reference pricing provider
    :param pricing_model: Optional reference pricing model
    """

    chunk_style: str
    max_tokens: int
    max_pass: int
    output_format: str
    first_prompt: str | None
    second_prompt: str | None
    final_prompt: str | None
    enable_chunk_writer: bool
    show_pricing: bool
    pricing_provider: str | None
    pricing_model: str | None


def _resolve_density_threshold(pass_num: int) -> int:
    """
    Resolve the density threshold used for a summarization pass.

    Earlier passes work on larger structured-fact input, while later passes
    work on increasingly compressed material. The threshold is relaxed across passes
    so density-aware chunking does not become overly aggressive once the text
    has already been reduced.

    :param pass_num: One-based summarization pass number
    :return: Density threshold for chunking
    """
    return [25, 75, 500][min(pass_num - 1, 2)]


def _validate_resolved_prompts(
    prompts: Dict[str, str],
    prompt_ids: Dict[str, str],
) -> None:
    """
    Validate that resolved prompt identifiers exist in the loaded prompt set.

    Prompt routing should fail early and clearly if a route points at a missing
    prompt. This avoids vague downstream failures during chunk or final-pass
    generation.

    :param prompts: Loaded prompt text map
    :param prompt_ids: Resolved first/multi/final prompt IDs
    :raises ConfigError: If any resolved prompt is missing
    """
    for pass_label, prompt_id in prompt_ids.items():
        if prompt_id not in prompts:
            available = ", ".join(sorted(prompts.keys()))
            raise ConfigError(
                f"Resolved {pass_label} prompt '{prompt_id}' is not present "
                f"in the loaded prompt bundle. Available prompts: {available}"
            )


def _write_generated_summary_chunk(
    generated_chunk: GeneratedChunk,
    *,
    output_dir: str,
    meeting_type: str,
    base_name: str,
    pass_num: int,
) -> None:
    """
    Persist one generated summary chunk immediately after provider completion.

    Keeping the write callback in the summary workflow preserves existing debug
    artifact behavior while the shared generation layer remains output-agnostic.

    :param generated_chunk: Completed generated chunk
    :param output_dir: Root summary output directory
    :param meeting_type: Detected or assigned meeting type
    :param base_name: Meeting identifier used for chunk filenames
    :param pass_num: Current summary pass number
    :return: None
    """
    chunk_file = write_chunk_file(
        chunk_text=generated_chunk.output,
        output_dir=output_dir,
        meeting_type=meeting_type,
        base_name=base_name,
        pass_num=pass_num,
        chunk_id=generated_chunk.chunk_id,
    )
    logger.info(
        "Wrote %d chunk tokens to %s",
        generated_chunk.output_tokens,
        chunk_file,
    )


RECORDS_SUMMARY_CONSOLIDATE_PROMPT = "records_summary_consolidate"
RECORDS_SUMMARY_FINAL_PROMPT = "records_summary_final"


def _compact_value(value: Any) -> str:
    """
    Render one structured value without verbose JSON whitespace.

    Human-readable strings remain plain text. Lists and mappings retain their
    exact values in compact JSON so vote details, dates, amounts, identifiers,
    and profile-specific attributes survive without expanding the prompt with
    indentation and repeated field scaffolding.

    :param value: Arbitrary structured value
    :return: Compact single-line representation
    """
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _append_compact_field(lines: List[str], label: str, value: Any) -> None:
    """
    Append one non-empty compact field to a prompt block.

    :param lines: Mutable output lines
    :param label: Human-readable field label
    :param value: Candidate field value
    :return: None
    """
    if value is None:
        return
    if isinstance(value, str) and not value.strip():
        return
    if isinstance(value, (list, tuple, dict)) and not value:
        return
    lines.append(f"{label}: {_compact_value(value)}")


def _format_summary_record(record: Dict[str, Any]) -> str:
    """
    Format one canonical Record for reader-oriented summary consumption.

    Evidence, source chunks, timestamps, video identifiers, extraction
    metadata, and internal Record numbering are deliberately excluded. Records
    already provide normalized facts, while attribution and supported
    attributes retain the source-domain details needed by the summarizer.

    :param record: Canonical Record object
    :return: Compact one-line Record representation
    """
    parts = [
        _compact_value(record.get("record_type") or "other"),
        _compact_value(record.get("subject") or "Untitled"),
        _compact_value(record.get("summary") or ""),
    ]
    line = " | ".join(part for part in parts if part)

    details: List[str] = []
    attribution = record.get("attribution")
    if isinstance(attribution, str) and attribution.strip():
        details.append(f"attribution={_compact_value(attribution)}")
    attributes = record.get("attributes")
    if isinstance(attributes, dict) and attributes:
        details.append(f"attributes={_compact_value(attributes)}")
    if details:
        line += " | " + " | ".join(details)
    return f"- {line}"


def _format_summary_event(
    event: Dict[str, Any],
    records: List[Dict[str, Any]],
    represented_indices: set[int],
) -> str:
    """
    Format one Event plus its canonical source Record summaries.

    Events provide deterministic organization. Canonical Records add the
    normalized factual detail needed for summary generation without repeating
    raw evidence, provenance, grouping internals, or embedded source-record JSON.

    :param event: Event object
    :param records: Canonical Records list
    :param represented_indices: Mutable set of linked one-based Record indices
    :return: Compact multi-line Event block
    """
    subject = str(event.get("subject") or "Untitled event").strip()
    lines = [f"## {subject}"]
    _append_compact_field(lines, "Type", event.get("event_type"))
    _append_compact_field(lines, "Summary", event.get("summary"))
    _append_compact_field(lines, "Outcome", event.get("outcome"))
    _append_compact_field(lines, "Outcome values", event.get("outcome_values"))
    _append_compact_field(lines, "Attribution", event.get("attributions"))
    review_flags = event.get("review_flags")
    if isinstance(review_flags, list) and "conflicting_outcomes" in review_flags:
        lines.append("Source uncertainty: conflicting outcomes were preserved")

    linked_records: List[str] = []
    source_indices = event.get("source_record_indices")
    if isinstance(source_indices, list):
        for value in source_indices:
            if isinstance(value, bool):
                continue
            try:
                record_index = int(value)
            except (TypeError, ValueError):
                continue
            if record_index < 1 or record_index > len(records):
                continue
            represented_indices.add(record_index)
            record = records[record_index - 1]
            record_summary = _compact_value(record.get("summary") or "")
            event_summary = _compact_value(event.get("summary") or "")
            attributes = record.get("attributes")

            # Event summaries usually repeat the primary Record verbatim. Skip
            # only exact duplicates without additional structured attributes;
            # secondary Records with amounts, dates, votes, or other facts stay.
            if record_summary == event_summary and not attributes:
                continue
            linked_records.append(_format_summary_record(record))

    if linked_records:
        lines.append("Supporting facts:")
        lines.extend(linked_records)
    return "\n".join(lines)


def resolve_artifact_meeting_type(
    records_artifact: Dict[str, Any],
    events_artifact: Dict[str, Any],
) -> str:
    """
    Resolve the meaningful meeting type carried by paired artifacts.

    Events are authoritative when they contain a classified value. A missing or
    explicitly unknown Events value falls back to Records, then to ``general``
    so prompt routing remains deterministic.

    :param records_artifact: Complete Records artifact
    :param events_artifact: Complete Events artifact
    :return: Meeting type suitable for prompt routing
    """
    for artifact in (events_artifact, records_artifact):
        meeting_type = str(artifact.get("meeting_type") or "").strip()
        if meeting_type and meeting_type != "unknown":
            return meeting_type
    return "general"


def build_records_events_summary_input(
    records_artifact: Dict[str, Any],
    events_artifact: Dict[str, Any],
) -> tuple[str, List[str]]:
    """
    Build compact prompt blocks from complete Records and Events artifacts.

    Events remain the organizational layer, while linked canonical Record
    summaries preserve material details. Verbose JSON, raw evidence, timestamps,
    provenance, internal identifiers, and embedded duplicate source Records
    are omitted to keep the model input compact and reader-oriented. Any
    Record not linked by Events is appended separately as a safety fallback.

    :param records_artifact: Complete Records artifact
    :param events_artifact: Complete Events artifact derived from those Records
    :return: Meeting metadata header and ordered compact prompt blocks
    :raises ValidationError: If either artifact is incomplete or malformed
    """
    if records_artifact.get("status") != "complete":
        raise ValidationError("Records artifact must have status 'complete'")
    if events_artifact.get("status") != "complete":
        raise ValidationError("Events artifact must have status 'complete'")

    records = records_artifact.get("records")
    events = events_artifact.get("events")
    if not isinstance(records, list):
        raise ValidationError("Records artifact must contain a records list")
    if not isinstance(events, list):
        raise ValidationError("Events artifact must contain an events list")
    if any(not isinstance(record, dict) for record in records):
        raise ValidationError("Each Record must be a JSON object")
    if any(not isinstance(event, dict) for event in events):
        raise ValidationError("Each Event must be a JSON object")

    records_source_hash = records_artifact.get("source_sha256")
    events_source_hash = events_artifact.get("source_sha256")
    if (
        records_source_hash
        and events_source_hash
        and records_source_hash != events_source_hash
    ):
        raise ValidationError(
            "Records and Events artifacts reference different normalized sources"
        )

    records_meeting_type = str(
        records_artifact.get("meeting_type") or ""
    ).strip()
    events_meeting_type = str(
        events_artifact.get("meeting_type") or ""
    ).strip()
    meaningful_types = {"", "unknown"}
    if (
        records_meeting_type not in meaningful_types
        and events_meeting_type not in meaningful_types
        and records_meeting_type != events_meeting_type
    ):
        raise ValidationError(
            "Records and Events artifacts disagree on meeting_type"
        )

    source_record_count = events_artifact.get("source_record_count")
    if (
        isinstance(source_record_count, int)
        and not isinstance(source_record_count, bool)
        and source_record_count != len(records)
    ):
        raise ValidationError(
            "Events source_record_count does not match the Records artifact"
        )

    event_count = events_artifact.get("event_count")
    if (
        isinstance(event_count, int)
        and not isinstance(event_count, bool)
        and event_count != len(events)
    ):
        raise ValidationError(
            "Events event_count does not match the Events artifact payload"
        )

    meeting_type = resolve_artifact_meeting_type(
        records_artifact,
        events_artifact,
    )
    header_lines = [
        "# Structured Meeting Facts",
        f"Meeting: {events_artifact.get('meeting') or 'unknown'}",
        f"Meeting date: {events_artifact.get('meeting_date') or 'unknown'}",
        f"Meeting type: {meeting_type}",
    ]
    header = "\n".join(header_lines)

    represented_indices: set[int] = set()
    blocks = [
        _format_summary_event(event, records, represented_indices)
        for event in events
    ]

    unlinked_records = [
        _format_summary_record(record)
        for index, record in enumerate(records, start=1)
        if index not in represented_indices
    ]
    if unlinked_records:
        blocks.append(
            "## Additional extracted facts\n"
            "Preserve these facts even though they were not grouped above.\n"
            + "\n".join(unlinked_records)
        )

    return header, blocks


def generate_records_events_summary(
    *,
    session: Any,
    llm_runtime: LLMRuntime,
    records_artifact: Dict[str, Any],
    events_artifact: Dict[str, Any],
    context: str,
    prompts: Dict[str, str],
    chunk_style: str,
    max_tokens: int,
    max_pass: int,
    first_prompt_id: str = RECORDS_SUMMARY_CONSOLIDATE_PROMPT,
    multi_prompt_id: str = RECORDS_SUMMARY_CONSOLIDATE_PROMPT,
    final_prompt_id: str = RECORDS_SUMMARY_FINAL_PROMPT,
    density_keywords: set[str] | None = None,
    known_name_permutations: Dict[str, List[str]] | None = None,
    on_chunk: Callable[[GeneratedChunk, int], None] | None = None,
) -> Dict[str, Any]:
    """
    Generate one production summary from existing Records and Events.

    Structured facts use the shared chunk generation and bounded reduction
    rules from the established summary workflow. Raw transcript text is never
    supplied, and deterministic Records and Events remain immutable inputs.

    :param session: Shared provider session
    :param llm_runtime: Resolved provider runtime
    :param records_artifact: Complete Records artifact
    :param events_artifact: Complete Events artifact
    :param context: Human-readable meeting context
    :param prompts: Loaded summary prompt text mapping
    :param chunk_style: Existing YATSEE chunking strategy
    :param max_tokens: Configured approximate source tokens per chunk
    :param max_pass: Maximum number of bounded reduction passes
    :param first_prompt_id: First reduction-pass prompt identifier
    :param multi_prompt_id: Later reduction-pass prompt identifier
    :param final_prompt_id: Final synthesis prompt identifier
    :param density_keywords: Optional density-chunking keywords
    :param known_name_permutations: Optional configured-name variants
    :param on_chunk: Optional callback receiving a generated chunk and pass number
    :return: Generated summary and run metadata
    :raises ConfigError: If required prompts are missing
    :raises ValidationError: If artifacts or reduction settings are invalid
    """
    if max_tokens <= 0:
        raise ValidationError("Summary chunk size must be positive")
    if max_pass <= 0:
        raise ValidationError("Summary max_pass must be positive")

    prompt_ids = {
        "first": first_prompt_id,
        "multi": multi_prompt_id,
        "final": final_prompt_id,
    }
    _validate_resolved_prompts(prompts, prompt_ids)

    header, blocks = build_records_events_summary_input(
        records_artifact,
        events_artifact,
    )
    working_text = f"{header}\n\n" + "\n\n".join(blocks)
    structured_input_tokens = estimate_token_count(working_text)
    usage = TokenUsage()
    pass_num = 1
    chunk_counts: List[int] = []

    def finalize(reduced_text: str, completed_passes: int) -> Dict[str, Any]:
        """
        Run the selected final presentation prompt and return summary metadata.

        :param reduced_text: Structured facts ready for final synthesis
        :param completed_passes: Number of consolidation passes performed
        :return: Generated summary and token metadata
        """
        final_prompt = prompts[final_prompt_id].format(
            context=context or "No context provided.",
            text=reduced_text,
        )
        summary = llm_generate_with_runtime(
            session=session,
            runtime=llm_runtime,
            prompt=final_prompt,
        )
        usage.add(
            input_tokens=estimate_token_count(final_prompt),
            output_tokens=estimate_token_count(summary),
        )
        return {
            "summary": summary,
            "pass_count": completed_passes,
            "chunk_counts": chunk_counts,
            "structured_input_tokens": structured_input_tokens,
            "reduced_tokens": estimate_token_count(reduced_text),
            "event_count": len(events_artifact.get("events", [])),
            "record_count": len(records_artifact.get("records", [])),
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
        }

    # A source that already fits the configured summary payload does not need a
    # lossy consolidation call. Send the structured facts directly to the final
    # profile style and report zero reduction passes.
    if structured_input_tokens <= max_tokens:
        return finalize(working_text, 0)

    while pass_num <= max_pass:
        prompt_id = first_prompt_id if pass_num == 1 else multi_prompt_id
        chunk_callback = None
        if on_chunk is not None:
            chunk_callback = lambda chunk, current_pass=pass_num: on_chunk(
                chunk,
                current_pass,
            )

        generated_chunks, pass_usage = generate_chunk_outputs(
            session=session,
            runtime=llm_runtime,
            text=working_text,
            prompt_id=prompt_id,
            prompt_template=prompts[prompt_id],
            context=context,
            chunk_style=chunk_style,
            max_tokens=max_tokens,
            density_keywords=density_keywords,
            density_threshold=_resolve_density_threshold(pass_num),
            known_name_permutations=known_name_permutations,
            stage_label=f"Summary pass {pass_num}",
            on_chunk=chunk_callback,
        )
        usage.add(
            input_tokens=pass_usage.input_tokens,
            output_tokens=pass_usage.output_tokens,
        )

        chunk_summaries = [chunk.output for chunk in generated_chunks]
        chunk_counts.append(len(chunk_summaries))
        working_text = "\n\n".join(chunk_summaries)
        logger.debug(
            "Summary pass %d joined output tokens %d",
            pass_num,
            estimate_token_count(working_text),
        )

        # Consolidation stops as soon as the joined output fits the final
        # payload, collapses to one chunk, or reaches the explicit pass bound.
        if (
            estimate_token_count(working_text) <= max_tokens
            or len(chunk_summaries) == 1
            or pass_num == max_pass
        ):
            return finalize(working_text, pass_num)

        pass_num += 1

    raise ValidationError("Summary reduction completed without final synthesis")


def summarize_one_meeting(
    *,
    session: Any,
    llm_runtime: LLMRuntime,
    records_artifact: Dict[str, Any],
    events_artifact: Dict[str, Any],
    base_name: str,
    context: str,
    prompt_bundle: Dict[str, Any],
    entity_cfg: Dict[str, Any],
    run_cfg: SummaryRunConfig,
    output_dir: str,
) -> Dict[str, Any]:
    """
    Summarize one meeting from complete Records and Events artifacts.

    The Events artifact supplies the deterministic meeting type used for prompt
    routing. The function preserves the existing summary-stage behaviors for
    prompt overrides, chunk debugging, token accounting, pricing, and final
    artifact writing without reclassifying or rereading the raw transcript.

    :param session: Shared provider session
    :param llm_runtime: Resolved provider runtime
    :param records_artifact: Complete Records artifact
    :param events_artifact: Complete Events artifact
    :param base_name: Meeting identifier used for output naming
    :param context: Human-readable meeting context
    :param prompt_bundle: Loaded summary prompt bundle and routing metadata
    :param entity_cfg: Resolved entity configuration
    :param run_cfg: Summary-specific runtime configuration
    :param output_dir: Directory for final summaries
    :return: Summary result metadata and generated output
    :raises ConfigError: If routed prompts are incomplete
    :raises ValidationError: If source artifacts are incomplete or malformed
    """
    prompts = prompt_bundle["prompts"]
    prompt_lookup = prompt_bundle["prompt_router"]
    meeting_type = resolve_artifact_meeting_type(
        records_artifact,
        events_artifact,
    )

    prompt_ids = resolve_summary_prompt_ids(
        meeting_type=meeting_type,
        prompt_lookup=prompt_lookup,
        first_override=run_cfg.first_prompt,
        multi_override=run_cfg.second_prompt,
        final_override=run_cfg.final_prompt,
    )
    _validate_resolved_prompts(prompts, prompt_ids)

    known_name_permutations = build_name_permutations(entity_cfg)
    density_keywords = set(
        prompt_bundle.get("density_keywords", {}).get("keywords", [])
    )

    chunk_writer = None
    if run_cfg.enable_chunk_writer:
        def chunk_writer(generated_chunk: GeneratedChunk, pass_num: int) -> None:
            """
            Persist one generated summary chunk for inspection.

            :param generated_chunk: Completed generated chunk
            :param pass_num: Current reduction pass
            :return: None
            """
            _write_generated_summary_chunk(
                generated_chunk,
                output_dir=output_dir,
                meeting_type=meeting_type,
                base_name=base_name,
                pass_num=pass_num,
            )

    result = generate_records_events_summary(
        session=session,
        llm_runtime=llm_runtime,
        records_artifact=records_artifact,
        events_artifact=events_artifact,
        context=context,
        prompts=prompts,
        chunk_style=run_cfg.chunk_style,
        max_tokens=run_cfg.max_tokens,
        max_pass=run_cfg.max_pass,
        first_prompt_id=prompt_ids["first"],
        multi_prompt_id=prompt_ids["multi"],
        final_prompt_id=prompt_ids["final"],
        density_keywords=density_keywords,
        known_name_permutations=known_name_permutations,
        on_chunk=chunk_writer,
    )

    pricing = build_pricing_summary(
        show_pricing=run_cfg.show_pricing,
        actual_provider=llm_runtime.provider_name,
        actual_model=llm_runtime.model,
        pricing_provider=run_cfg.pricing_provider,
        pricing_model=run_cfg.pricing_model,
        input_tokens=result["input_tokens"],
        output_tokens=result["output_tokens"],
    )
    output_path = write_summary_file(
        result["summary"],
        base_name,
        output_dir,
        run_cfg.output_format,
    )

    logger.info(
        "Final summary: %d tokens written to: %s",
        estimate_token_count(result["summary"]),
        output_path,
    )

    return {
        "base_name": base_name,
        "meeting_type": meeting_type,
        "summary": result["summary"],
        "output_path": output_path,
        "structured_input_tokens": result["structured_input_tokens"],
        "pass_count": result["pass_count"],
        "chunk_counts": result["chunk_counts"],
        "reduced_tokens": result["reduced_tokens"],
        "record_count": result["record_count"],
        "event_count": result["event_count"],
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "total_tokens": result["total_tokens"],
        "pricing": pricing,
    }
