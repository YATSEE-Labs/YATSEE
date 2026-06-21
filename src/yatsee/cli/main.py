"""
Root CLI entrypoint for YATSEE.

This module owns the global parser, top-level command registration, and
process-level error handling. Command-group parser setup and handlers live in
the sibling CLI modules.
"""

from __future__ import annotations

import argparse
import sys

from yatsee.cli.audio import register_audio_commands
from yatsee.cli.config import register_config_commands
from yatsee.cli.intel import register_intel_commands
from yatsee.cli.transcript import register_transcript_commands
from yatsee.core.config import GLOBAL_CONFIG_PATH
from yatsee.core.errors import YatseeError


def build_parser() -> argparse.ArgumentParser:
    """
    Build the root CLI parser.

    :return: Fully configured argparse parser
    """
    parser = argparse.ArgumentParser(
        prog="yatsee",
        description="YATSEE command-line interface",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=GLOBAL_CONFIG_PATH,
        help="Path to global yatsee.toml",
    )

    subparsers = parser.add_subparsers(dest="command")

    register_config_commands(subparsers)
    register_audio_commands(subparsers)
    register_transcript_commands(subparsers)
    register_intel_commands(subparsers)

    return parser


def main() -> int:
    """
    Parse CLI arguments and dispatch to the selected command handler.

    :return: Process exit code
    """
    parser = build_parser()
    args = parser.parse_args()

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0

    try:
        return handler(args)
    except YatseeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())