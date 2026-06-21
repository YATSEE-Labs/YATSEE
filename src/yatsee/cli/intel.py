"""
CLI command registration and handlers for YATSEE intelligence commands.

This module owns the `yatsee intel ...` command group, including multi-pass
summarization and deterministic signal extraction.
"""

from __future__ import annotations

import argparse

from yatsee.intel.runner import run_intelligence_stage


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

    intel_run_parser = intel_subparsers.add_parser("run", help="Run multi-pass transcript summarization")
    intel_run_parser.add_argument("-e", "--entity", help="Entity handle to process")
    intel_run_parser.add_argument("-i", "--txt-input", help="Path to a transcript file or directory (.txt)")
    intel_run_parser.add_argument("-o", "--output-dir", help="Directory to save final summaries")
    intel_run_parser.add_argument("-m", "--model", help="LLM model override")
    intel_run_parser.add_argument(
        "--llm-provider",
        help="LLM provider override (e.g. ollama, llamacpp, openai, anthropic, codex_cli)",
    )
    intel_run_parser.add_argument("--llm-provider-url", help="LLM provider URL or executable target override")
    intel_run_parser.add_argument("--llm-api-key", help="LLM API key override")
    intel_run_parser.add_argument("--show-pricing", action="store_true", help="Estimate reference pricing for the run")
    intel_run_parser.add_argument(
        "--no-show-pricing",
        action="store_true",
        help="Disable reference pricing even if enabled in config",
    )
    intel_run_parser.add_argument("--pricing-provider", help="Reference provider for pricing (e.g. openai, anthropic)")
    intel_run_parser.add_argument("--pricing-model", help="Reference model for pricing (e.g. gpt-5.4, claude-sonnet-4)")
    intel_run_parser.add_argument(
        "-f",
        "--output-format",
        choices=["markdown", "yaml"],
        default="markdown",
        help="Summary output format",
    )
    intel_run_parser.add_argument(
        "-j",
        "--job-profile",
        choices=["civic", "research"],
        default="civic",
        help="Prompt workflow family",
    )
    intel_run_parser.add_argument(
        "-s",
        "--chunk-style",
        choices=["word", "sentence", "density"],
        default="word",
        help="Chunk boundary method",
    )
    intel_run_parser.add_argument("-w", "--max-words", type=int, help="Approximate word count threshold for chunking")
    intel_run_parser.add_argument("-t", "--max-tokens", type=int, help="Approximate max tokens per chunk")
    intel_run_parser.add_argument("-p", "--max-pass", type=int, default=3, help="Maximum summarization passes")
    intel_run_parser.add_argument(
        "-d",
        "--disable-auto-classification",
        action="store_true",
        help="Disable automatic meeting classification",
    )
    intel_run_parser.add_argument("--first-prompt", help="Prompt ID for first pass")
    intel_run_parser.add_argument("--second-prompt", help="Prompt ID for multi-pass chunk summaries")
    intel_run_parser.add_argument("--final-prompt", help="Prompt ID for final summary pass")
    intel_run_parser.add_argument("--context", default="", help="Optional human-readable meeting context")
    intel_run_parser.add_argument("--print-prompts", action="store_true", help="Print prompt templates and exit")
    intel_run_parser.add_argument(
        "--enable-chunk-writer",
        action="store_true",
        help="Write intermediate chunk summaries for debugging",
    )
    intel_run_parser.set_defaults(handler=handle_intel_run)

    intel_signals_parser = intel_subparsers.add_parser(
        "signals",
        help="Generate deterministic meeting signal artifacts",
    )
    intel_signals_parser.add_argument("-e", "--entity", help="Entity handle to process")
    intel_signals_parser.add_argument("-i", "--input-path", help="Normalized TXT file or directory")
    intel_signals_parser.add_argument("-o", "--output-dir", help="Directory to save meeting signal artifacts")
    intel_signals_parser.add_argument("--force", action="store_true", help="Overwrite existing outputs")
    intel_signals_parser.set_defaults(handler=handle_intel_signals)


def handle_intel_run(args: argparse.Namespace) -> int:
    """
    Run the intelligence summarization stage.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
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