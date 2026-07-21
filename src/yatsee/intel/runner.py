"""
Top-level summary stage runner for YATSEE intelligence jobs.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from yatsee.core.io import read_text
from yatsee.intel.llm import create_llm_session, resolve_llm_runtime
from yatsee.intel.prompts import (
    load_classifier_bundle,
    load_summary_prompt_bundle,
    validate_prompt,
)
from yatsee.intel.resolve import extract_context_from_filename, resolve_summary_paths
from yatsee.intel.summary import SummaryRunConfig, summarize_one_transcript

logger = logging.getLogger(__name__)


def run_summary_stage(
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
    output_format: str = "markdown",
    job_profile: str = "civic",
    chunk_style: str = "word",
    max_words: int | None = None,
    max_tokens: int | None = None,
    max_pass: int = 3,
    disable_auto_classification: bool = False,
    first_prompt: str | None = None,
    second_prompt: str | None = None,
    final_prompt: str | None = None,
    context: str = "",
    print_prompts: bool = False,
    enable_chunk_writer: bool = False,
) -> Dict[str, Any]:
    """
    Run summary generation across one or more normalized transcript files.

    This function is the non-CLI entry point for summary execution. It resolves
    summary-specific paths and model settings, loads the selected prompt bundle,
    resolves provider configuration, and dispatches each transcript to the
    per-file summary workflow.

    :param global_config_path: Path to the global ``yatsee.toml`` file
    :param entity: Optional entity handle
    :param input_path: Optional normalized transcript file or directory
    :param output_dir: Optional final summary output directory
    :param model: Optional LLM model override
    :param llm_provider: Optional provider override
    :param llm_provider_url: Optional provider URL or executable target override
    :param llm_api_key: Optional provider API key override
    :param show_pricing: Whether to enable reference pricing
    :param no_show_pricing: Whether to disable configured reference pricing
    :param pricing_provider: Optional reference pricing provider
    :param pricing_model: Optional reference pricing model
    :param output_format: Final summary output format
    :param job_profile: Prompt/profile name
    :param chunk_style: Chunk boundary strategy
    :param max_words: Optional approximate word threshold per source chunk
    :param max_tokens: Optional approximate token threshold per source chunk
    :param max_pass: Maximum number of summary reduction passes
    :param disable_auto_classification: Whether to skip meeting classification
    :param first_prompt: Optional first-pass prompt override
    :param second_prompt: Optional multi-pass prompt override
    :param final_prompt: Optional final synthesis prompt override
    :param context: Optional additional meeting context
    :param print_prompts: Whether to return prompt text without model execution
    :param enable_chunk_writer: Whether to persist intermediate summary chunks
    :return: Summary-stage result metadata
    :raises ConfigError: If required prompt or provider configuration is missing
    :raises ValidationError: If summary inputs or model settings are invalid
    """
    resolved = resolve_summary_paths(
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
    prompt_bundle = load_summary_prompt_bundle(entity_cfg, job_profile)

    if print_prompts:
        return {
            "mode": "print_prompts",
            "job_profile": job_profile,
            "classifier_file": classifier_bundle["path"],
            "used_fallback_classifier": classifier_bundle["fallback"],
            "classifier_prompt": classifier_bundle["classifier_prompt"],
            "prompt_file": prompt_bundle["path"],
            "used_fallback_prompts": prompt_bundle["fallback"],
            "prompts": prompt_bundle["prompts"],
        }

    if disable_auto_classification:
        validate_prompt(first_prompt, "first-prompt", prompt_bundle["prompts"])
        validate_prompt(second_prompt, "second-prompt", prompt_bundle["prompts"])
        validate_prompt(final_prompt, "final-prompt", prompt_bundle["prompts"])

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

    run_cfg = SummaryRunConfig(
        chunk_style=chunk_style,
        max_tokens=resolved["max_tokens"],
        max_pass=max_pass,
        output_format=output_format,
        disable_auto_classification=disable_auto_classification,
        first_prompt=first_prompt,
        second_prompt=second_prompt,
        final_prompt=final_prompt,
        enable_chunk_writer=enable_chunk_writer,
        show_pricing=resolved_show_pricing,
        pricing_provider=resolved_pricing_provider,
        pricing_model=resolved_pricing_model,
    )

    results: List[Dict[str, Any]] = []

    with create_llm_session() as session:
        for file_path in resolved["file_list"]:
            base_name = os.path.splitext(os.path.basename(file_path))[0]
            file_context = extract_context_from_filename(file_path, context)
            transcript = read_text(file_path)

            result = summarize_one_transcript(
                session=session,
                llm_runtime=llm_runtime,
                transcript=transcript,
                base_name=base_name,
                context=file_context,
                classifier_bundle=classifier_bundle,
                prompt_bundle=prompt_bundle,
                entity_cfg=entity_cfg,
                run_cfg=run_cfg,
                output_dir=resolved["output_dir"],
            )
            results.append(result)

    return {
        "mode": "run",
        "input_dir": resolved["input_dir"],
        "output_dir": resolved["output_dir"],
        "llm_provider": llm_runtime.provider_name,
        "model": llm_runtime.model,
        "show_pricing": resolved_show_pricing,
        "pricing_provider": resolved_pricing_provider,
        "pricing_model": resolved_pricing_model,
        "processed": len(results),
        "results": results,
        "classifier_file": classifier_bundle["path"],
        "used_fallback_classifier": classifier_bundle["fallback"],
        "prompt_file": prompt_bundle["path"],
        "used_fallback_prompts": prompt_bundle["fallback"],
    }
