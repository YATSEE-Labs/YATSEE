"""
Per-transcript summary orchestration for YATSEE intelligence jobs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import Any, Dict

from yatsee.core.errors import ConfigError
from yatsee.intel.classifier import classify_transcript
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
    Hold summary-specific runtime settings for one or more transcripts.

    Provider configuration, resolved paths, and prompt bundle loading remain
    outside this object. These fields define only the behavior owned by the
    summary workflow itself.

    :param chunk_style: Chunk boundary strategy
    :param max_tokens: Approximate source token budget per chunk
    :param max_pass: Maximum number of summary reduction passes
    :param output_format: Final summary artifact format
    :param disable_auto_classification: Whether to skip meeting classification
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
    disable_auto_classification: bool
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

    Earlier passes work on larger raw transcript text, while later passes work
    on increasingly compressed material. The threshold is relaxed across passes
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
                f"Resolved {pass_label} prompt '{prompt_id}' is not present in the loaded prompt bundle. "
                f"Available prompts: {available}"
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
    :param base_name: Source transcript basename
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


def summarize_one_transcript(
    *,
    session: Any,
    llm_runtime: LLMRuntime,
    transcript: str,
    base_name: str,
    context: str,
    classifier_bundle: Dict[str, Any],
    prompt_bundle: Dict[str, Any],
    entity_cfg: Dict[str, Any],
    run_cfg: SummaryRunConfig,
    output_dir: str,
) -> Dict[str, Any]:
    """
    Summarize one transcript through the configured multi-pass intelligence flow.

    The transcript may be classified first to select a prompt route. The working
    text is then chunked and summarized across one or more passes until it is
    small enough for final synthesis or the maximum pass count is reached.
    Configured-name context is derived from entity configuration and injected into
    chunk prompts to preserve the existing summary behavior.

    Token counts are tracked for visibility and optional reference pricing.
    Final output may be written to disk, and chunk-level debug output can also
    be emitted when enabled.

    :param session: Shared requests session
    :param llm_runtime: Resolved provider runtime
    :param transcript: Transcript text
    :param base_name: Transcript basename used for output naming
    :param context: Human-readable transcript context
    :param classifier_bundle: Shared classifier prompt and allowed labels
    :param prompt_bundle: Loaded summary prompt bundle and routing metadata
    :param entity_cfg: Resolved entity configuration
    :param run_cfg: Summary-specific runtime configuration
    :param output_dir: Directory for final outputs
    :return: Summary result metadata and generated output
    :raises ProviderError: If provider execution fails
    :raises ConfigError: If resolved prompt configuration is invalid
    """
    prompts = prompt_bundle["prompts"]
    prompt_lookup = prompt_bundle["prompt_router"]
    classifier_prompt_template = classifier_bundle["classifier_prompt"]
    allowed_meeting_types = classifier_bundle["allowed_meeting_types"]
    density_keywords_cfg = prompt_bundle["density_keywords"]

    usage = TokenUsage()

    meeting_type = "general"
    if not run_cfg.disable_auto_classification:
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
        logger.info("Auto-detected meeting type: %s", meeting_type)

    prompt_ids = resolve_summary_prompt_ids(
        meeting_type=meeting_type,
        prompt_lookup=prompt_lookup,
        first_override=run_cfg.first_prompt,
        multi_override=run_cfg.second_prompt,
        final_override=run_cfg.final_prompt,
    )
    _validate_resolved_prompts(prompts, prompt_ids)

    known_name_permutations = build_name_permutations(entity_cfg)

    pass_num = 1
    max_pass = run_cfg.max_pass
    summary = ""
    working_text = transcript

    while pass_num <= max_pass:
        prompt_type = prompt_ids["first"] if pass_num == 1 else prompt_ids["multi"]
        density_threshold = _resolve_density_threshold(pass_num)
        density_keywords = set(density_keywords_cfg.get("keywords", []))

        chunk_writer = None
        if run_cfg.enable_chunk_writer:
            chunk_writer = partial(
                _write_generated_summary_chunk,
                output_dir=output_dir,
                meeting_type=meeting_type,
                base_name=base_name,
                pass_num=pass_num,
            )

        generated_chunks, pass_usage = generate_chunk_outputs(
            session=session,
            runtime=llm_runtime,
            text=working_text,
            prompt_id=prompt_type,
            prompt_template=prompts[prompt_type],
            context=context,
            chunk_style=run_cfg.chunk_style,
            max_tokens=run_cfg.max_tokens,
            density_keywords=density_keywords,
            density_threshold=density_threshold,
            known_name_permutations=known_name_permutations,
            stage_label=f"Pass {pass_num}",
            on_chunk=chunk_writer,
        )
        usage.add(
            input_tokens=pass_usage.input_tokens,
            output_tokens=pass_usage.output_tokens,
        )

        chunk_summaries = [chunk.output for chunk in generated_chunks]
        working_text = "\n\n".join(chunk_summaries)
        logger.debug("Joined transcript tokens %d", estimate_token_count(working_text))

        # Final synthesis starts once the intermediate text has been reduced to
        # a manageable size, collapsed to a single chunk, or the run has reached
        # its configured pass limit. A single-pass run always performs final
        # synthesis so the function returns a complete summary.
        should_finalize = False

        if pass_num >= 2:
            should_finalize = (
                estimate_token_count(working_text) <= run_cfg.max_tokens
                or len(chunk_summaries) == 1
                or pass_num == max_pass
            )
        elif max_pass == 1:
            should_finalize = True

        if should_finalize:
            final_prompt_id = prompt_ids["final"]
            logger.info("Processing final summary with prompt [%s]", final_prompt_id)

            final_prompt_template = prompts[final_prompt_id]
            final_prompt = final_prompt_template.format(
                context=context or "No context provided.",
                text=working_text,
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
            break

        pass_num += 1

    pricing = build_pricing_summary(
        show_pricing=run_cfg.show_pricing,
        actual_provider=llm_runtime.provider_name,
        actual_model=llm_runtime.model,
        pricing_provider=run_cfg.pricing_provider,
        pricing_model=run_cfg.pricing_model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )

    logger.info(
        "Token usage: tokIn=%d tokOut=%d total=%d",
        usage.input_tokens,
        usage.output_tokens,
        usage.total_tokens,
    )

    if pricing["enabled"]:
        if pricing["estimated_cost"] is None:
            logger.info(
                "Estimated reference cost unavailable for provider=%s model=%s",
                pricing["reference_provider"],
                pricing["reference_model"],
            )
        else:
            logger.info(
                "Estimated reference cost using %s/%s: $%.4f",
                pricing["reference_provider"],
                pricing["reference_model"],
                pricing["estimated_cost"],
            )

    output_path = write_summary_file(
        summary,
        base_name,
        output_dir,
        run_cfg.output_format,
    )
    logger.info(
        "Final summary: %d tokens written to: %s",
        estimate_token_count(summary),
        output_path,
    )

    return {
        "base_name": base_name,
        "meeting_type": meeting_type,
        "summary": summary,
        "output_path": output_path,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "pricing": pricing,
    }
