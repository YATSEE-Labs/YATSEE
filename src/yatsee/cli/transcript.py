"""
CLI command registration and handlers for YATSEE transcript commands.

This module owns the `yatsee transcript ...` command group, including slicing
and normalization stage entrypoints.
"""

from __future__ import annotations

import argparse

from yatsee.transcript.normalize import run_normalize_stage
from yatsee.transcript.slice import run_slice_stage


def register_transcript_commands(subparsers: argparse._SubParsersAction) -> None:
    """
    Register transcript-stage CLI commands.

    :param subparsers: Root argparse subparser registry
    :return: None
    """
    # ----------------------------
    # transcript
    # ----------------------------
    transcript_parser = subparsers.add_parser("transcript", help="Transcript preparation commands")
    transcript_subparsers = transcript_parser.add_subparsers(dest="transcript_command")

    slice_parser = transcript_subparsers.add_parser("slice", help="Slice VTT transcripts into TXT and JSONL segments")
    slice_parser.add_argument("-e", "--entity", help="Entity handle to process")
    slice_parser.add_argument("-i", "--vtt-input", help="VTT file or directory")
    slice_parser.add_argument("-o", "--output-dir", help="Directory to save transcript outputs")
    slice_parser.add_argument("-m", "--model", help="SentenceTransformer model override")
    slice_parser.add_argument("-g", "--gen-embed", action="store_true", help="Generate JSONL with embeddings")
    slice_parser.add_argument("--max-window", type=float, default=90.0, help="Hard upper limit on segment length")
    slice_parser.add_argument("--force", action="store_true", help="Overwrite existing outputs")
    slice_parser.add_argument(
        "-d",
        "--device",
        choices=["auto", "cuda", "cpu", "mps"],
        default="auto",
        help="Device for embedding execution",
    )
    slice_parser.set_defaults(handler=handle_transcript_slice)

    normalize_parser = transcript_subparsers.add_parser("normalize", help="Normalize transcript text")
    normalize_parser.add_argument("-e", "--entity", help="Entity handle to process")
    normalize_parser.add_argument("-i", "--input-path", help="TXT file or directory")
    normalize_parser.add_argument("-o", "--output-dir", help="Directory to save normalized output")
    normalize_parser.add_argument("-m", "--model", help="Transcription model suffix for input path resolution")
    normalize_parser.add_argument("--no-spacy", action="store_true", help="Disable spaCy sentence splitting")
    normalize_parser.add_argument("--deep-clean", action="store_true", help="Enable slightly more aggressive cleanup")
    normalize_parser.add_argument("--preserve-paragraphs", action="store_true", help="Preserve paragraph spacing")
    normalize_parser.add_argument("--force", action="store_true", help="Overwrite existing outputs")
    normalize_parser.set_defaults(handler=handle_transcript_normalize)


def handle_transcript_slice(args: argparse.Namespace) -> int:
    """
    Run the transcript slicing stage.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    result = run_slice_stage(
        global_config_path=args.config,
        entity=args.entity,
        vtt_input=args.vtt_input,
        output_dir=args.output_dir,
        model_override=args.model,
        gen_embed=args.gen_embed,
        max_window=args.max_window,
        force=args.force,
        device_arg=args.device,
    )

    print(f"VTT input: {result['vtt_input']}")
    print(f"Output directory: {result['output_dir']}")
    print(f"Embedding model: {result['embedding_model']}")
    print(f"Device: {result['device']}")
    print(f"Discovered files: {result['discovered']}")
    print(f"TXT written: {result['txt_written']}")
    print(f"JSONL written: {result['jsonl_written']}")

    for message in result["messages"]:
        print(f"- {message}")

    return 0


def handle_transcript_normalize(args: argparse.Namespace) -> int:
    """
    Run the transcript normalization stage.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    result = run_normalize_stage(
        global_config_path=args.config,
        entity=args.entity,
        input_path=args.input_path,
        output_dir=args.output_dir,
        model_override=args.model,
        no_spacy=args.no_spacy,
        deep_clean=args.deep_clean,
        preserve_paragraphs=args.preserve_paragraphs,
        force=args.force,
    )

    print(f"Input path: {result['input_path']}")
    print(f"Output directory: {result['output_dir']}")
    print(f"Sentence model: {result['sentence_model']}")
    print(f"Discovered files: {result['discovered']}")
    print(f"Written files: {result['written']}")
    print(f"Skipped files: {result['skipped']}")

    for message in result["messages"]:
        print(f"- {message}")

    return 0