"""
CLI command registration and handlers for YATSEE intelligence commands.

This module owns the `yatsee intel ...` command group, including multi-pass
summarization and structured records extraction.
"""

from __future__ import annotations

import argparse



def register_intel_commands(subparsers: argparse._SubParsersAction) -> None:
    """
    Register intelligence-stage CLI commands.

    :param subparsers: Root argparse subparser registry
    :return: None
    """
    # ----------------------------
    # intel
    # ----------------------------
    intel_parser = subparsers.add_parser("intel", help="Intelligence-stage commands")
    intel_subparsers = intel_parser.add_subparsers(dest="intel_command")

    summarize_parser = intel_subparsers.add_parser(
        "summarize",
        aliases=["run"],
        help="Generate summaries through Records and Events",
    )
    _add_summarize_arguments(summarize_parser)
    summarize_parser.set_defaults(handler=handle_intel_run)

    records_parser = intel_subparsers.add_parser(
        "records",
        help="Extract structured civic records from transcripts",
    )
    _add_records_arguments(records_parser)
    records_parser.set_defaults(handler=handle_intel_records)

    events_parser = intel_subparsers.add_parser(
        "events",
        help="Generate deterministic civic events from records artifacts",
    )
    _add_events_arguments(events_parser)
    events_parser.set_defaults(handler=handle_intel_events)

    prompts_parser = intel_subparsers.add_parser("prompts", help="Prompt bundle utilities")
    prompts_subparsers = prompts_parser.add_subparsers(dest="prompt_command")

    prompt_validate_parser = prompts_subparsers.add_parser(
        "validate",
        help="Validate prompt/profile bundle wiring",
    )
    prompt_validate_parser.add_argument(
        "-e",
        "--entity",
        help="Entity handle for entity-local prompt lookup",
    )
    prompt_validate_parser.add_argument(
        "-j",
        "--job-profile",
        "--profile",
        dest="job_profile",
        default="civic",
        help="Prompt/profile name to validate",
    )
    prompt_validate_parser.add_argument(
        "--all",
        action="store_true",
        help="Validate all discovered filesystem-backed prompt profiles",
    )
    prompt_validate_parser.set_defaults(handler=handle_intel_prompts_validate)

def _add_summarize_arguments(parser: argparse.ArgumentParser) -> None:
    """
    Register arguments for Records-first summary orchestration.

    :param parser: Subparser to configure
    :return: None
    """
    parser.add_argument("-e", "--entity", help="Entity handle to process")
    parser.add_argument(
        "-i",
        "--txt-input",
        help="Path to a source text file or directory (.txt)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        help="Directory to save final summaries",
    )
    parser.add_argument(
        "-m",
        "--model",
        help="Records and summary model override",
    )
    parser.add_argument(
        "--llm-provider",
        help="LLM provider override (e.g. ollama, llamacpp, openai, anthropic, codex_cli)",
    )
    parser.add_argument(
        "--llm-provider-url",
        help="LLM provider URL or executable target override",
    )
    parser.add_argument(
        "--llm-api-key",
        help="LLM API key override; prefer llm_api_key_env or YATSEE_LLM_API_KEY",
    )
    parser.add_argument(
        "--show-pricing",
        action="store_true",
        help="Estimate reference pricing for Records and summary generation",
    )
    parser.add_argument(
        "--no-show-pricing",
        action="store_true",
        help="Disable reference pricing even if enabled in config",
    )
    parser.add_argument(
        "--pricing-provider",
        help="Reference provider for pricing (e.g. openai, anthropic)",
    )
    parser.add_argument(
        "--pricing-model",
        help="Reference model for pricing (e.g. gpt-5.4, claude-sonnet-4)",
    )
    parser.add_argument(
        "-f",
        "--output-format",
        choices=["markdown", "yaml"],
        default="markdown",
        help="Summary output format",
    )
    parser.add_argument(
        "-j",
        "--job-profile",
        default=None,
        help=(
            "Prompt/profile name. Defaults to civic for entity runs and is "
            "required for standalone input."
        ),
    )
    parser.add_argument(
        "-s",
        "--chunk-style",
        choices=["word", "sentence", "density"],
        default="sentence",
        help="Chunk boundary method for structured summary facts",
    )
    parser.add_argument(
        "-w",
        "--max-words",
        type=int,
        help="Approximate word threshold for structured summary chunks",
    )
    parser.add_argument(
        "-t",
        "--max-tokens",
        type=int,
        help="Approximate token threshold for structured summary chunks",
    )
    parser.add_argument(
        "-p",
        "--max-pass",
        type=int,
        default=3,
        help="Maximum summary consolidation passes",
    )
    parser.add_argument(
        "-d",
        "--disable-auto-classification",
        action="store_true",
        help="Use the profile's general Records route without classification",
    )
    parser.add_argument("--first-prompt", help="Prompt ID for first consolidation pass")
    parser.add_argument("--second-prompt", help="Prompt ID for later consolidation passes")
    parser.add_argument("--final-prompt", help="Prompt ID for final summary synthesis")
    parser.add_argument(
        "--context",
        default="",
        help="Optional human-readable source context",
    )
    parser.add_argument(
        "--print-prompts",
        action="store_true",
        help="Print summary prompt templates and exit",
    )
    parser.add_argument(
        "--enable-chunk-writer",
        action="store_true",
        help="Write intermediate summary chunks for debugging",
    )

def _add_records_arguments(parser: argparse.ArgumentParser) -> None:
    """
    Register records extraction arguments.

    CLI argument grouping is intentionally deferred until summary and records
    have both stabilized enough to reveal their durable overlap.

    :param parser: Records subparser to configure
    :return: None
    """
    parser.add_argument("-e", "--entity", help="Entity handle to process")
    parser.add_argument("-i", "--txt-input", help="Path to a transcript file or directory (.txt)")
    parser.add_argument("-o", "--output-dir", help="Directory to save record artifacts")
    parser.add_argument("-m", "--model", help="LLM model override")
    parser.add_argument("--llm-provider", help="LLM provider override")
    parser.add_argument("--llm-provider-url", help="LLM provider URL or executable target override")
    parser.add_argument(
        "--llm-api-key",
        help="LLM API key override; prefer llm_api_key_env or YATSEE_LLM_API_KEY",
    )
    parser.add_argument(
        "--show-pricing",
        action="store_true",
        help="Estimate reference pricing for the run",
    )
    parser.add_argument(
        "--no-show-pricing",
        action="store_true",
        help="Disable configured reference pricing",
    )
    parser.add_argument("--pricing-provider", help="Reference provider for pricing")
    parser.add_argument("--pricing-model", help="Reference model for pricing")
    parser.add_argument("-j", "--job-profile", default="civic", help="Prompt/profile name")
    parser.add_argument(
        "-s",
        "--chunk-style",
        choices=["word", "sentence", "density"],
        default="density",
        help="Chunk boundary method",
    )
    parser.add_argument("-w", "--max-words", type=int, help="Approximate words per chunk")
    parser.add_argument("-t", "--max-tokens", type=int, help="Approximate max tokens per chunk")
    parser.add_argument(
        "-d",
        "--disable-auto-classification",
        action="store_true",
        help="Use the general records route without model classification",
    )
    parser.add_argument("--meeting-type", help="Explicit records meeting-type route")
    parser.add_argument("--context", default="", help="Optional human-readable meeting context")
    parser.add_argument(
        "--print-prompts",
        action="store_true",
        help="Print records prompts and exit",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild records regardless of normalized source hash",
    )
    parser.add_argument(
        "--align-timestamps",
        action="store_true",
        help=(
            "Run the normal records workflow, then align all available records "
            "to sliced segment timestamps"
        ),
    )
    parser.add_argument(
        "--timestamp-only",
        action="store_true",
        help=(
            "Force timestamp recalculation for existing records only; skip missing "
            "or unusable records and never invoke the LLM"
        ),
    )


def _add_events_arguments(parser: argparse.ArgumentParser) -> None:
    """
    Register deterministic records-to-events arguments.

    :param parser: Events subparser to configure
    :return: None
    """
    parser.add_argument(
        "-e",
        "--entity",
        required=True,
        help="Entity handle whose records corpus should become events",
    )
    parser.add_argument(
        "-m",
        "--model",
        help="Configured records model/output directory override",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        help="Events output directory override",
    )
    parser.add_argument(
        "-i",
        "--meeting",
        dest="meeting_filter",
        help="Generate events only for meeting directories containing this text",
    )




def handle_intel_events(args: argparse.Namespace) -> int:
    """
    Generate deterministic civic events and print a compact stage report.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    from yatsee.intel.events import run_events_stage

    result = run_events_stage(
        global_config_path=args.config,
        entity=args.entity,
        model=args.model,
        output_dir=args.output_dir,
        input_path=args.txt_input,
    )

    print(f"Input: {result['input_dir']}")
    if result.get("entity"):
        print(f"Entity: {result['entity']}")
    print(f"Model: {result['model']}")
    print(f"Records directory: {result['records_dir']}")
    print(f"Events output directory: {result['output_dir']}")
    print(f"Sources discovered: {result['discovered']}")
    print(f"Records extracted: {result['records_extracted']}")
    print(f"Records reused: {result['records_reused']}")
    print(f"Events refreshed: {result['events_processed']}")
    print(f"Summaries written: {result['processed']}")
    print(f"Sources skipped: {result['skipped']}")
    print(f"Source records: {result['source_record_count']}")
    print(f"Events generated: {result['event_count']}")
    print(f"Decision events: {result['decision_event_count']}")
    print(f"Multi-record events: {result['multi_record_event_count']}")
    print(f"Adjacent duplicates consolidated: {result['duplicate_record_count']}")
    print(f"Events index: {result['events_jsonl_path']}")
    print(f"Meetings index: {result['meetings_json_path']}")

    for item in result["results"]:
        artifact = item["artifact"]
        print(f"- {item['meeting']}")
        print(f"  JSON: {item['json_path']}")
        print(f"  Markdown: {item['markdown_path']}")
        print(
            "  Records/events: "
            f"{artifact['source_record_count']}/{artifact['event_count']}"
        )
        print(f"  Decision events: {artifact['decision_event_count']}")
        if artifact["duplicate_record_count"]:
            print(
                "  Adjacent duplicates consolidated: "
                f"{artifact['duplicate_record_count']}"
            )
        if artifact.get("review_flag_counts"):
            rendered = ", ".join(
                f"{key}={value}"
                for key, value in sorted(artifact["review_flag_counts"].items())
            )
            print(f"  Review flags: {rendered}")

    for item in result["skipped_results"]:
        print(f"- Skipped {item['meeting']}: {item['reason']}")
    return 0



def handle_intel_run(args: argparse.Namespace) -> int:
    """
    Run the intelligence summarization stage.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    from yatsee.intel.runner import run_summary_stage

    result = run_summary_stage(
        global_config_path=args.config,
        entity=args.entity,
        input_path=args.txt_input,
        output_dir=args.output_dir,
        model=args.model,
        llm_provider=args.llm_provider,
        llm_provider_url=args.llm_provider_url,
        llm_api_key=args.llm_api_key,
        show_pricing=args.show_pricing,
        no_show_pricing=args.no_show_pricing,
        pricing_provider=args.pricing_provider,
        pricing_model=args.pricing_model,
        output_format=args.output_format,
        job_profile=args.job_profile,
        chunk_style=args.chunk_style,
        max_words=args.max_words,
        max_tokens=args.max_tokens,
        max_pass=args.max_pass,
        disable_auto_classification=args.disable_auto_classification,
        first_prompt=args.first_prompt,
        second_prompt=args.second_prompt,
        final_prompt=args.final_prompt,
        context=args.context,
        print_prompts=args.print_prompts,
        enable_chunk_writer=args.enable_chunk_writer,
    )

    if result.get("mode") == "print_prompts":
        print(f"Job profile: {result['job_profile']}")
        print(f"Prompt file: {result['prompt_file'] or 'inline fallback prompts'}")
        print(f"Used fallback prompts: {result['used_fallback_prompts']}")
        print()

        for key, prompt_text in result["prompts"].items():
            print(f"=== {key} ===")
            print(prompt_text)
            print()
        return 0

    print(f"Input: {result['input_dir']}")
    if result.get("entity"):
        print(f"Entity: {result['entity']}")
    print(f"Records directory: {result['records_dir']}")
    print(f"Events directory: {result['events_dir']}")
    print(f"Output directory: {result['output_dir']}")
    print(f"Provider: {result.get('llm_provider', '(unknown)')}")
    print(f"Model: {result['model']}")
    print(f"Sources discovered: {result['discovered']}")
    print(f"Records extracted: {result['records_extracted']}")
    print(f"Records reused: {result['records_reused']}")
    print(f"Events refreshed: {result['events_processed']}")
    print(f"Summaries written: {result['processed']}")
    print(f"Sources skipped: {result['skipped']}")
    print(f"Prompt file: {result['prompt_file'] or 'inline fallback prompts'}")
    print(f"Used fallback prompts: {result['used_fallback_prompts']}")
    print(f"Show pricing: {result.get('show_pricing', False)}")

    pricing_provider = result.get("pricing_provider")
    pricing_model = result.get("pricing_model")
    if pricing_provider or pricing_model:
        print(
            "Pricing reference: "
            f"{pricing_provider or '(default provider)'} / "
            f"{pricing_model or '(default model)'}"
        )

    for item in result["results"]:
        print(f"- {item['base_name']}")
        print(f"  Meeting type: {item['meeting_type']}")
        print(f"  Output: {item['output_path'] or '(no file written)'}")
        print(
            f"  Tokens: in={item['input_tokens']} "
            f"out={item['output_tokens']} total={item['total_tokens']}"
        )
        print(
            "  Structured input/reduced: "
            f"{item['structured_input_tokens']}/{item['reduced_tokens']}"
        )
        print(
            "  Reduction passes/chunks: "
            f"{item['pass_count']} / "
            + ", ".join(str(count) for count in item["chunk_counts"])
        )
        print(
            f"  Records/events: {item['record_count']}/{item['event_count']}"
        )

        pricing = item.get("pricing", {})
        if pricing.get("enabled"):
            ref_provider = pricing.get("reference_provider") or "(unknown)"
            ref_model = pricing.get("reference_model") or "(unknown)"
            estimated_cost = pricing.get("estimated_cost")

            if estimated_cost is None:
                print(f"  Reference pricing: unavailable for {ref_provider}/{ref_model}")
            else:
                print(
                    f"  Reference pricing ({ref_provider}/{ref_model}): "
                    f"${estimated_cost:.4f}"
                )

    for item in result["skipped_results"]:
        print(f"- Skipped {item['meeting']}: {item['reason']}")

    return 0


def handle_intel_prompts_validate(args: argparse.Namespace) -> int:
    """
    Validate prompt/profile bundle wiring.

    This validates loading and route references only. It does not evaluate prompt
    wording, output quality, or whether a profile is mature enough for production use.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    from yatsee.core.config import load_entity_config, load_global_config
    from yatsee.intel.prompts import (
        discover_summary_prompt_profiles,
        load_classifier_bundle,
        load_summary_prompt_bundle,
    )

    entity_cfg = {}
    if args.entity:
        global_cfg = load_global_config(args.config)
        entity_cfg = load_entity_config(global_cfg, args.entity)

    if args.all:
        profiles = discover_summary_prompt_profiles(entity_cfg)
        if not profiles:
            print("No filesystem-backed prompt profiles discovered.")
            return 1

        for profile in profiles:
            classifier_bundle = load_classifier_bundle(
                entity_cfg,
                profile,
                require_prompt_file=True,
            )
            bundle = load_summary_prompt_bundle(entity_cfg, profile, require_prompt_file=True)
            print(f"Job profile: {profile}")
            print(f"Classifier file: {classifier_bundle['path']}")
            print(f"Classifier types: {len(classifier_bundle['allowed_meeting_types'])}")
            print(f"Prompt file: {bundle['path']}")
            print(f"Prompts: {len(bundle['prompts'])}")
            print(f"Routes: {len(bundle['prompt_router'])}")
            print()

        print(f"Prompt bundle validation passed for {len(profiles)} profile(s).")
        return 0

    classifier_bundle = load_classifier_bundle(
        entity_cfg,
        args.job_profile,
        require_prompt_file=True,
    )
    bundle = load_summary_prompt_bundle(entity_cfg, args.job_profile, require_prompt_file=True)

    print(f"Job profile: {args.job_profile}")
    print(f"Classifier file: {classifier_bundle['path']}")
    print(f"Classifier types: {len(classifier_bundle['allowed_meeting_types'])}")
    print(f"Prompt file: {bundle['path']}")
    print(f"Used fallback prompts: {bundle['fallback']}")
    print(f"Prompts: {len(bundle['prompts'])}")
    print(f"Routes: {len(bundle['prompt_router'])}")
    print("Prompt bundle validation passed.")
    return 0


def handle_intel_records(args: argparse.Namespace) -> int:
    """
    Run structured records extraction and print a compact stage report.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    from yatsee.intel.record import run_records_stage

    result = run_records_stage(
        global_config_path=args.config,
        entity=args.entity,
        input_path=args.txt_input,
        output_dir=args.output_dir,
        model=args.model,
        llm_provider=args.llm_provider,
        llm_provider_url=args.llm_provider_url,
        llm_api_key=args.llm_api_key,
        show_pricing=args.show_pricing,
        no_show_pricing=args.no_show_pricing,
        pricing_provider=args.pricing_provider,
        pricing_model=args.pricing_model,
        job_profile=args.job_profile,
        chunk_style=args.chunk_style,
        max_words=args.max_words,
        max_tokens=args.max_tokens,
        disable_auto_classification=args.disable_auto_classification,
        meeting_type=args.meeting_type,
        context=args.context,
        print_prompts=args.print_prompts,
        force=args.force,
        align_timestamps=args.align_timestamps,
        timestamp_only=args.timestamp_only,
    )

    if result.get("mode") == "print_prompts":
        print(f"Job profile: {result['job_profile']}")
        print(f"Prompt file: {result['prompt_file']}")
        print()
        for meeting_type, prompt_id in sorted(result["records_router"].items()):
            print(f"{meeting_type}: {prompt_id}")
        print()
        for prompt_id, prompt_text in result["prompts"].items():
            print(f"=== {prompt_id} ===")
            print(prompt_text)
            print()
        return 0

    if result.get("mode") == "timestamp_only":
        print("Mode: forced timestamp realignment only")
        print(f"Input directory: {result['input_dir']}")
        print(f"Output directory: {result['output_dir']}")
        print(f"Timestamp embedding model: {result['embedding_model']}")
        print(f"Discovered transcripts: {result['discovered']}")
        print(
            "Existing records artifacts: "
            f"{result['existing_records_artifacts']}"
        )
        print(f"Aligned records artifacts: {result['aligned']}")
        print(f"Reused timestamp alignments: {result['alignment_reused']}")
        print(f"Skipped records artifacts: {result['skipped']}")
        print(f"Failed timestamp alignments: {result['failed']}")

        for item in result["results"]:
            print(f"- {item['base_name']}")
            print(f"  JSON: {item['output_path']}")
            if item["records_artifact_status"] == "existing":
                print(f"  Markdown: {item['markdown_path']}")
                print(f"  Records: {item['record_count']}")
            else:
                print(
                    "  Records artifact: "
                    f"{item['records_artifact_status']}"
                )
            print(
                "  Timestamp alignment: "
                f"{item['timestamp_alignment_status']}"
            )
            print(
                "  Timestamp detail: "
                f"{item['timestamp_alignment_message']}"
            )
            if item["timestamp_alignment_status"] in {"complete", "reused"}:
                print(
                    "  Timestamp matches: "
                    f"{item['timestamp_matched_record_count']} matched, "
                    f"{item['timestamp_ambiguous_record_count']} ambiguous, "
                    f"{item['timestamp_unmatched_record_count']} unmatched"
                )
                if item.get("timestamp_alignment_path"):
                    print(
                        "  Timestamp report: "
                        f"{item['timestamp_alignment_path']}"
                    )
        return 0

    print(f"Input directory: {result['input_dir']}")
    print(f"Output directory: {result['output_dir']}")
    print(f"Provider: {result['llm_provider']}")
    print(f"Model: {result['model']}")
    print(f"Prompt file: {result['prompt_file']}")
    print(f"Discovered transcripts: {result['discovered']}")
    print(f"Extracted transcripts: {result['extracted']}")
    print(f"Reused transcripts: {result['reused']}")
    if result.get("timestamp_alignment_requested"):
        print(f"Timestamp embedding model: {result['embedding_model']}")

    for item in result["results"]:
        print(f"- {item['base_name']}")
        print(f"  JSON: {item['output_path']}")
        print(f"  Markdown: {item['markdown_path']}")
        if item.get("reused"):
            if item.get("reuse_mode") == "timestamp_only":
                print(
                    "  Reused: existing complete records artifact "
                    "for timestamp enrichment"
                )
            else:
                print("  Reused: normalized source hash matched")

        print(f"  Status: {item['status']}")
        print(f"  Meeting type: {item['meeting_type']}")
        print(f"  Prompt: {item['prompt_id']}")
        print(
            f"  Chunks: {item['successful_chunk_count']}/{item['chunk_count']} "
            f"successful ({item['partial_chunk_count']} partial, "
            f"{item['failed_chunk_count']} failed)"
        )
        if item["status"] == "complete":
            print(f"  Validation warnings: {item['validation_warning_count']}")
        else:
            print(f"  Validation issues: {item['validation_issue_count']}")
        if item["normalized_record_count"]:
            print(f"  Normalized record types: {item['normalized_record_count']}")
        if item["rejected_record_count"]:
            print(f"  Rejected records: {item['rejected_record_count']}")
        if item["rejected_line_count"]:
            print(f"  Rejected JSONL lines: {item['rejected_line_count']}")
        print(f"  Records: {item['record_count']}")
        alignment_status = item.get("timestamp_alignment_status")
        if alignment_status and alignment_status != "not_requested":
            print(f"  Timestamp alignment: {alignment_status}")
            print(f"  Timestamp detail: {item['timestamp_alignment_message']}")
            if alignment_status in {"complete", "reused"}:
                print(
                    "  Timestamp matches: "
                    f"{item['timestamp_matched_record_count']} matched, "
                    f"{item['timestamp_ambiguous_record_count']} ambiguous, "
                    f"{item['timestamp_unmatched_record_count']} unmatched"
                )
                if item.get("timestamp_alignment_path"):
                    print(
                        "  Timestamp report: "
                        f"{item['timestamp_alignment_path']}"
                    )
        print(
            f"  Tokens: in={item['input_tokens']} "
            f"out={item['output_tokens']} total={item['total_tokens']}"
        )

        pricing = item.get("pricing", {})
        if pricing.get("enabled"):
            estimated_cost = pricing.get("estimated_cost")
            reference = (
                f"{pricing.get('reference_provider')}/{pricing.get('reference_model')}"
            )
            if estimated_cost is None:
                print(f"  Reference pricing: unavailable for {reference}")
            else:
                print(f"  Reference pricing ({reference}): ${estimated_cost:.4f}")

    return 0
