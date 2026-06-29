"""
CLI command registration and handlers for YATSEE audio commands.

This module owns the `yatsee audio ...` command group, including media
formatting and transcription stage entrypoints.
"""

from __future__ import annotations

import argparse



def register_audio_commands(subparsers: argparse._SubParsersAction) -> None:
    """
    Register audio-stage CLI commands.

    :param subparsers: Root argparse subparser registry
    :return: None
    """
    # ----------------------------
    # audio
    # ----------------------------
    audio_parser = subparsers.add_parser("audio", help="Audio processing commands")
    audio_subparsers = audio_parser.add_subparsers(dest="audio_command")

    format_parser = audio_subparsers.add_parser("format", help="Normalize media into mono 16kHz audio")
    format_parser.add_argument("-e", "--entity", help="Entity handle to process")
    format_parser.add_argument("-i", "--input-dir", help="Direct override path to media input")
    format_parser.add_argument("-o", "--output-dir", help="Directory to save normalized audio")
    format_parser.add_argument("--format", default="flac", choices=["wav", "flac"], help="Output audio format")
    format_parser.add_argument("--create-chunks", action="store_true", help="Split output audio into chunks")
    format_parser.add_argument("--chunk-duration", type=int, default=600, help="Chunk duration in seconds")
    format_parser.add_argument("--chunk-overlap", type=int, default=2, help="Chunk overlap in seconds")
    format_parser.add_argument("--dry-run", action="store_true", help="Preview actions without changing files")
    format_parser.add_argument("--force", action="store_true", help="Reprocess files even if already converted")
    format_parser.set_defaults(handler=handle_audio_format)

    transcribe_parser = audio_subparsers.add_parser("transcribe", help="Transcribe normalized audio to VTT")
    transcribe_parser.add_argument("-e", "--entity", help="Entity handle to process")
    transcribe_parser.add_argument("-i", "--audio-input", help="Audio file or directory")
    transcribe_parser.add_argument("-o", "--output-dir", help="Directory to save transcripts")
    transcribe_parser.add_argument("-g", "--get-chunks", action="store_true", help="Transcribe using audio chunk files")
    transcribe_parser.add_argument("-m", "--model", help="Whisper model size override")
    transcribe_parser.add_argument("--faster", action="store_true", help="Use faster-whisper if installed")
    transcribe_parser.add_argument(
        "--transcription-profile",
        default="default",
        choices=["default", "qa_cleanup"],
        help=(
            "Transcription behavior preset. Use qa_cleanup when rebuilding "
            "transcripts flagged by QA for ASR loop artifacts."
        ),
    )
    transcribe_parser.add_argument("-l", "--lang", default="en", help="Language code or 'auto'")
    transcribe_parser.add_argument(
        "-d",
        "--device",
        choices=["auto", "cuda", "cpu", "mps"],
        default="auto",
        help="Device for model execution",
    )
    transcribe_parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    transcribe_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress verbose output")
    transcribe_parser.set_defaults(handler=handle_audio_transcribe)


def handle_audio_format(args: argparse.Namespace) -> int:
    """
    Run the audio formatting stage.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    from yatsee.audio.format import run_format_stage

    result = run_format_stage(
        global_config_path=args.config,
        entity=args.entity,
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        file_format=args.format,
        create_chunks=args.create_chunks,
        chunk_duration=args.chunk_duration,
        chunk_overlap=args.chunk_overlap,
        dry_run=args.dry_run,
        force=args.force,
    )

    print(f"Input directory: {result['input_dir']}")
    print(f"Output directory: {result['output_dir']}")
    print(f"Discovered files: {result['discovered']}")
    print(f"Processed files: {result['processed']}")
    print(f"Skipped files: {result['skipped']}")
    print(f"Chunks created: {result['chunked']}")

    for message in result["messages"]:
        print(f"- {message}")

    return 0


def handle_audio_transcribe(args: argparse.Namespace) -> int:
    """
    Run the audio transcription stage.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    from yatsee.audio.transcribe import run_transcribe_stage

    result = run_transcribe_stage(
        global_config_path=args.config,
        entity=args.entity,
        audio_input=args.audio_input,
        output_dir=args.output_dir,
        get_chunks=args.get_chunks,
        model_override=args.model,
        faster=args.faster,
        language=args.lang,
        device_arg=args.device,
        verbose=args.verbose,
        quiet=args.quiet,
        transcription_profile=args.transcription_profile,
    )

    print(f"Audio input: {result['audio_input']}")
    print(f"Output directory: {result['output_dir']}")
    print(f"Model: {result['model']}")
    print(f"Backend: {result['backend']}")
    print(f"Device: {result['device']}")
    print(f"Transcription profile: {result.get('transcription_profile', args.transcription_profile)}")
    print(f"Discovered files: {result['discovered']}")
    print(f"Processed files: {result['processed']}")
    print(f"Skipped files: {result['skipped']}")

    for message in result["messages"]:
        print(f"- {message}")

    return 0