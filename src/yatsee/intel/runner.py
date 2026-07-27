"""
Top-level Records-to-Summary orchestration for YATSEE intelligence jobs.

The summary command is a pipeline entry point. It reuses the canonical Records
stage, refreshes deterministic Events from those Records, and then performs only
summary consolidation and presentation. The implementation does not duplicate
extraction logic or create a competing summary subsystem.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from yatsee.core.errors import ConfigError, ValidationError
from yatsee.core.io import read_text
from yatsee.intel.events import (
    generate_events_for_records_artifact,
    run_events_stage,
)
from yatsee.intel.llm import create_llm_session, resolve_llm_runtime
from yatsee.intel.prompts import load_summary_prompt_bundle, validate_prompt
from yatsee.intel.record import run_records_stage
from yatsee.intel.resolve import resolve_summary_paths
from yatsee.intel.summary import SummaryRunConfig, summarize_one_meeting

logger = logging.getLogger(__name__)


def _load_json_object(path: str, label: str) -> Dict[str, Any]:
    """
    Load one JSON artifact and require an object root.

    :param path: Artifact path
    :param label: Human-readable artifact label
    :return: Parsed JSON object
    :raises ValidationError: If the artifact is missing, invalid, or not an object
    """
    artifact_path = Path(path)
    if not artifact_path.is_file():
        raise ValidationError(f"Missing {label} artifact: {path}")

    try:
        parsed = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid {label} JSON '{path}': {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValidationError(f"{label} artifact '{path}' must contain an object")
    return parsed


def _build_source_context(
    base_name: str,
    records_artifact: Dict[str, Any],
    events_artifact: Dict[str, Any],
    extra_context: str,
) -> str:
    """
    Build compact summary context from deterministic artifact metadata.

    :param base_name: Source basename
    :param records_artifact: Complete Records artifact
    :param events_artifact: Complete Events artifact
    :param extra_context: Optional user-supplied context note
    :return: Human-readable prompt context
    """
    values = (
        str(events_artifact.get("meeting") or base_name).strip(),
        str(events_artifact.get("meeting_date") or "").strip(),
        str(
            events_artifact.get("meeting_type")
            or records_artifact.get("meeting_type")
            or ""
        ).strip(),
    )
    context = " - ".join(value for value in values if value and value != "unknown")
    if extra_context.strip():
        context = (
            f"{context} ({extra_context.strip()})"
            if context
            else extra_context.strip()
        )
    return context


def _resolve_summary_profile(entity: str | None, job_profile: str | None) -> str:
    """
    Resolve summary profile policy for entity and standalone workflows.

    Entity runs retain the normal civic default. Standalone source files require
    an explicit profile so arbitrary content is not silently processed with
    civic extraction assumptions.

    :param entity: Optional entity handle
    :param job_profile: Optional requested profile
    :return: Resolved profile name
    :raises ValidationError: If standalone input has no explicit profile
    """
    if job_profile:
        return job_profile
    if entity:
        return "civic"
    raise ValidationError(
        "Standalone summary input requires --job-profile/-j so the Records "
        "and summary contracts are explicit"
    )


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
    job_profile: str | None = None,
    chunk_style: str = "sentence",
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
    Ensure Records and Events exist, then generate final summaries.

    Records extraction is delegated to ``run_records_stage`` so cache behavior,
    prompt routing, validation, and artifact output remain identical to the
    standalone Records command. Events are deterministically refreshed from the
    exact Records artifacts returned by that stage. Only then are compact facts
    consolidated as needed and passed to the selected final summary prompt.

    :param global_config_path: Path to the global ``yatsee.toml`` file
    :param entity: Optional entity handle
    :param input_path: Optional source text file or directory
    :param output_dir: Optional final summary output directory
    :param model: Optional Records and summary model override
    :param llm_provider: Optional provider override
    :param llm_provider_url: Optional provider URL or executable target override
    :param llm_api_key: Optional provider API key override
    :param show_pricing: Whether to enable reference pricing
    :param no_show_pricing: Whether to disable configured reference pricing
    :param pricing_provider: Optional reference pricing provider
    :param pricing_model: Optional reference pricing model
    :param output_format: Final summary output format
    :param job_profile: Optional prompt/profile name
    :param chunk_style: Summary fact chunk boundary strategy
    :param max_words: Optional approximate word threshold per summary chunk
    :param max_tokens: Optional approximate token threshold per summary chunk
    :param max_pass: Maximum number of summary consolidation passes
    :param disable_auto_classification: Whether Records should use the general route
    :param first_prompt: Optional first consolidation prompt override
    :param second_prompt: Optional later consolidation prompt override
    :param final_prompt: Optional final synthesis prompt override
    :param context: Optional additional source context
    :param print_prompts: Whether to return summary prompts without execution
    :param enable_chunk_writer: Whether to persist intermediate summary chunks
    :return: End-to-end summary workflow metadata
    :raises ConfigError: If prompt or provider configuration is invalid
    :raises ValidationError: If inputs or generated artifacts are invalid
    """
    resolved_profile = _resolve_summary_profile(entity, job_profile)
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
    prompt_bundle = load_summary_prompt_bundle(
        entity_cfg,
        resolved_profile,
        require_prompt_file=True,
    )

    if print_prompts:
        return {
            "mode": "print_prompts",
            "job_profile": resolved_profile,
            "prompt_file": prompt_bundle["path"],
            "used_fallback_prompts": prompt_bundle["fallback"],
            "prompts": prompt_bundle["prompts"],
        }

    validate_prompt(first_prompt, "first-prompt", prompt_bundle["prompts"])
    validate_prompt(second_prompt, "second-prompt", prompt_bundle["prompts"])
    validate_prompt(final_prompt, "final-prompt", prompt_bundle["prompts"])

    # Records owns source discovery, extraction, classification, validation, and
    # cache reuse. Summary intentionally does not replicate any of that logic.
    records_result = run_records_stage(
        global_config_path=global_config_path,
        entity=entity,
        input_path=input_path,
        output_dir=resolved["records_dir"],
        model=resolved["model"],
        llm_provider=llm_provider,
        llm_provider_url=llm_provider_url,
        llm_api_key=llm_api_key,
        show_pricing=show_pricing,
        no_show_pricing=no_show_pricing,
        pricing_provider=pricing_provider,
        pricing_model=pricing_model,
        job_profile=resolved_profile,
        chunk_style="density",
        disable_auto_classification=disable_auto_classification,
        context=context,
    )

    # A full entity run delegates to the standalone Events stage as well, so
    # per-meeting artifacts and aggregate indexes stay identical. Explicit
    # source selections refresh only their selected per-source Events artifacts
    # and do not replace a corpus-wide index with a partial subset.
    event_results_by_meeting: Dict[str, Dict[str, Any]] = {}
    if entity and input_path is None:
        events_result = run_events_stage(
            global_config_path,
            entity=entity,
            model=resolved["model"],
            output_dir=resolved["events_dir"],
        )
        event_results_by_meeting = {
            item["meeting"]: item for item in events_result["results"]
        }
        events_processed = events_result["processed"]
    else:
        events_processed = 0

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
        first_prompt=first_prompt,
        second_prompt=second_prompt,
        final_prompt=final_prompt,
        enable_chunk_writer=enable_chunk_writer,
        show_pricing=resolved_show_pricing,
        pricing_provider=resolved_pricing_provider,
        pricing_model=resolved_pricing_model,
    )

    project_root = str(Path(global_config_path).expanduser().resolve().parent)
    results: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []

    with create_llm_session() as session:
        for records_item in records_result["results"]:
            base_name = str(records_item.get("base_name") or "").strip()
            records_path = str(records_item.get("output_path") or "").strip()
            if not base_name or not records_path:
                skipped.append(
                    {
                        "meeting": base_name or "unknown",
                        "reason": "Records stage did not return a complete artifact path",
                    }
                )
                continue

            try:
                event_result = event_results_by_meeting.get(base_name)
                if event_result is None:
                    event_result = generate_events_for_records_artifact(
                        records_path=records_path,
                        output_dir=resolved["events_dir"],
                        project_root=project_root,
                        meeting=base_name,
                    )
                    events_processed += 1
                records_artifact = _load_json_object(records_path, "Records")
                events_artifact = event_result["artifact"]
                source_context = _build_source_context(
                    base_name,
                    records_artifact,
                    events_artifact,
                    context,
                )
                result = summarize_one_meeting(
                    session=session,
                    llm_runtime=llm_runtime,
                    records_artifact=records_artifact,
                    events_artifact=events_artifact,
                    base_name=base_name,
                    context=source_context,
                    prompt_bundle=prompt_bundle,
                    entity_cfg=entity_cfg,
                    run_cfg=run_cfg,
                    output_dir=resolved["output_dir"],
                )
                result["records_path"] = records_path
                result["events_path"] = event_result["json_path"]
                result["records_reused"] = bool(records_item.get("reused"))
                results.append(result)
            except (ConfigError, ValidationError) as exc:
                logger.warning("Skipping summary for %s: %s", base_name, exc)
                skipped.append({"meeting": base_name, "reason": str(exc)})

    if not results:
        detail = "; ".join(
            f"{item['meeting']}: {item['reason']}" for item in skipped[:5]
        )
        raise ValidationError(f"No summaries were generated. {detail}")

    return {
        "mode": "run",
        "entity": entity,
        "job_profile": resolved_profile,
        "input_dir": records_result["input_dir"],
        "records_dir": resolved["records_dir"],
        "events_dir": resolved["events_dir"],
        "output_dir": resolved["output_dir"],
        "llm_provider": llm_runtime.provider_name,
        "model": llm_runtime.model,
        "show_pricing": resolved_show_pricing,
        "pricing_provider": resolved_pricing_provider,
        "pricing_model": resolved_pricing_model,
        "discovered": records_result["discovered"],
        "records_extracted": records_result["extracted"],
        "records_reused": records_result["reused"],
        "events_processed": events_processed,
        "processed": len(results),
        "skipped": len(skipped),
        "results": results,
        "skipped_results": skipped,
        "records_prompt_file": records_result["prompt_file"],
        "prompt_file": prompt_bundle["path"],
        "used_fallback_prompts": prompt_bundle["fallback"],
    }
