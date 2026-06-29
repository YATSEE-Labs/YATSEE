"""
CLI command registration and handlers for YATSEE intelligence commands.

This module owns the `yatsee intel ...` command group, including multi-pass
summarization and deterministic signal extraction.
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
        help="Generate multi-pass transcript summaries",
    )
    _add_summarize_arguments(summarize_parser)
    summarize_parser.set_defaults(handler=handle_intel_run)

    prompts_parser = intel_subparsers.add_parser("prompts", help="Prompt bundle utilities")
    prompts_subparsers = prompts_parser.add_subparsers(dest="prompt_command")

    prompt_validate_parser = prompts_subparsers.add_parser(
        "validate",
        help="Validate prompt/profile bundle wiring",
    )
    prompt_validate_parser.add_argument("-e", "--entity", help="Entity handle for entity-local prompt lookup")
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

    intel_signals_parser = intel_subparsers.add_parser(
        "signals",
        help="Generate deterministic meeting signal artifacts",
    )
    intel_signals_parser.add_argument("-e", "--entity", help="Entity handle to process")
    intel_signals_parser.add_argument("-i", "--input-path", help="Normalized TXT file or directory")
    intel_signals_parser.add_argument("-o", "--output-dir", help="Directory to save meeting signal artifacts")
    intel_signals_parser.add_argument("--force", action="store_true", help="Overwrite existing outputs")
    intel_signals_parser.set_defaults(handler=handle_intel_signals)


def _add_summarize_arguments(parser: argparse.ArgumentParser) -> None:
    """
    Register arguments shared by the summarize command and its run alias.

    :param parser: Subparser to configure
    :return: None
    """
    parser.add_argument("-e", "--entity", help="Entity handle to process")
    parser.add_argument("-i", "--txt-input", help="Path to a transcript file or directory (.txt)")
    parser.add_argument("-o", "--output-dir", help="Directory to save final summaries")
    parser.add_argument("-m", "--model", help="LLM model override")
    parser.add_argument(
        "--llm-provider",
        help="LLM provider override (e.g. ollama, llamacpp, openai, anthropic, codex_cli)",
    )
    parser.add_argument("--llm-provider-url", help="LLM provider URL or executable target override")
    parser.add_argument(
        "--llm-api-key",
        help="LLM API key override; prefer llm_api_key_env or YATSEE_LLM_API_KEY",
    )
    parser.add_argument("--show-pricing", action="store_true", help="Estimate reference pricing for the run")
    parser.add_argument(
        "--no-show-pricing",
        action="store_true",
        help="Disable reference pricing even if enabled in config",
    )
    parser.add_argument("--pricing-provider", help="Reference provider for pricing (e.g. openai, anthropic)")
    parser.add_argument("--pricing-model", help="Reference model for pricing (e.g. gpt-5.4, claude-sonnet-4)")
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
        default="civic",
        help="Prompt/profile name for intelligence routing",
    )
    parser.add_argument(
        "-s",
        "--chunk-style",
        choices=["word", "sentence", "density"],
        default="word",
        help="Chunk boundary method",
    )
    parser.add_argument("-w", "--max-words", type=int, help="Approximate word count threshold for chunking")
    parser.add_argument("-t", "--max-tokens", type=int, help="Approximate max tokens per chunk")
    parser.add_argument("-p", "--max-pass", type=int, default=3, help="Maximum summarization passes")
    parser.add_argument(
        "-d",
        "--disable-auto-classification",
        action="store_true",
        help="Disable automatic meeting classification",
    )
    parser.add_argument("--first-prompt", help="Prompt ID for first pass")
    parser.add_argument("--second-prompt", help="Prompt ID for multi-pass chunk summaries")
    parser.add_argument("--final-prompt", help="Prompt ID for final summary pass")
    parser.add_argument("--context", default="", help="Optional human-readable meeting context")
    parser.add_argument("--print-prompts", action="store_true", help="Print prompt templates and exit")
    parser.add_argument(
        "--enable-chunk-writer",
        action="store_true",
        help="Write intermediate chunk summaries for debugging",
    )


def handle_intel_run(args: argparse.Namespace) -> int:
    """
    Run the intelligence summarization stage.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    from yatsee.intel.runner import run_intelligence_stage

    result = run_intelligence_stage(args)

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

    print(f"Input directory: {result['input_dir']}")
    print(f"Output directory: {result['output_dir']}")
    print(f"Provider: {result.get('llm_provider', '(unknown)')}")
    print(f"Model: {result['model']}")
    print(f"Processed transcripts: {result['processed']}")
    print(f"Prompt file: {result['prompt_file'] or 'inline fallback prompts'}")
    print(f"Used fallback prompts: {result['used_fallback_prompts']}")
    print(f"Show pricing: {result.get('show_pricing', False)}")

    pricing_provider = result.get("pricing_provider")
    pricing_model = result.get("pricing_model")
    if pricing_provider or pricing_model:
        print(f"Pricing reference: {pricing_provider or '(default provider)'} / {pricing_model or '(default model)'}")

    for item in result["results"]:
        print(f"- {item['base_name']}")
        print(f"  Meeting type: {item['meeting_type']}")
        print(f"  Output: {item['output_path'] or '(no file written)'}")
        print(
            f"  Tokens: in={item['input_tokens']} "
            f"out={item['output_tokens']} total={item['total_tokens']}"
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
    from yatsee.intel.prompts import discover_prompt_profiles, load_prompt_bundle

    entity_cfg = {}
    if args.entity:
        global_cfg = load_global_config(args.config)
        entity_cfg = load_entity_config(global_cfg, args.entity)

    if args.all:
        profiles = discover_prompt_profiles(entity_cfg)
        if not profiles:
            print("No filesystem-backed prompt profiles discovered.")
            return 1

        for profile in profiles:
            bundle = load_prompt_bundle(entity_cfg, profile, require_prompt_file=True)
            print(f"Job profile: {profile}")
            print(f"Prompt file: {bundle['path']}")
            print(f"Prompts: {len(bundle['prompts'])}")
            print(f"Routes: {len(bundle['prompt_router'])}")
            print()

        print(f"Prompt bundle validation passed for {len(profiles)} profile(s).")
        return 0

    bundle = load_prompt_bundle(entity_cfg, args.job_profile, require_prompt_file=True)

    print(f"Job profile: {args.job_profile}")
    print(f"Prompt file: {bundle['path']}")
    print(f"Used fallback prompts: {bundle['fallback']}")
    print(f"Prompts: {len(bundle['prompts'])}")
    print(f"Routes: {len(bundle['prompt_router'])}")
    print("Prompt bundle validation passed.")
    return 0


def handle_intel_signals(args: argparse.Namespace) -> int:
    """
    Run deterministic meeting-signal extraction and print a compact stage report.

    The signal stage emits mechanical evidence candidates from normalized
    transcript text. It does not produce final meeting records or official
    minutes.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    from yatsee.intel.signals import run_signals_stage

    result = run_signals_stage(
        global_config_path=args.config,
        entity=args.entity,
        input_path=args.input_path,
        output_dir=args.output_dir,
        force=args.force,
    )

    print(f"Input path: {result['input_path']}")
    print(f"Output directory: {result['output_dir']}")
    print(f"Discovered files: {result['discovered']}")
    print(f"Written files: {result['written']}")
    print(f"Skipped files: {result['skipped']}")

    count_fields = (
        ("action_count", "Action signals"),
        ("roll_call_count", "Roll call signals"),
        ("money_count", "Money signals"),
        ("civic_object_count", "Civic object signals"),
        ("question_count", "Question signals"),
        ("people_count", "People signals"),
        ("low_confidence_count", "Low-confidence lines"),
    )

    for item in result["results"]:
        print(f"- {item['base_name']}")
        print(f"  Output: {item['output_path']}")
        print(f"  Written: {item['written']}")

        for key, label in count_fields:
            if key in item:
                print(f"  {label}: {item[key]}")

    return 0