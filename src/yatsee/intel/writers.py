"""
Output writing helpers for YATSEE intelligence jobs.
"""

from __future__ import annotations

import mdformat
import yaml

from yatsee.core.io import ensure_directory, write_text
from yatsee.core.paths import resolve_contained_path, sanitize_path_component


def write_summary_file(
    summary: str,
    basename: str,
    output_dir: str,
    fmt: str = "yaml",
) -> str:
    """
    Write a summary artifact to disk in Markdown or YAML format.

    Markdown output includes a generated title header based on the source
    basename. YAML output stores the generated text under a single ``summary``
    key so downstream readers can consume a stable structure.

    :param summary: Generated summary content
    :param basename: Base filename without extension
    :param output_dir: Directory where the summary file should be written
    :param fmt: Output format, either ``markdown`` or ``yaml``
    :return: Full path to the written summary file
    :raises ConfigError: If the directory or file cannot be written
    :raises ValueError: If an unsupported output format is requested
    :raises ValidationError: If a path component is invalid
    """
    if fmt not in {"markdown", "yaml"}:
        raise ValueError(f"Unsupported summary output format: {fmt}")

    extension = "md" if fmt == "markdown" else "yaml"
    safe_basename = sanitize_path_component(basename)
    output_path = resolve_contained_path(
        output_dir,
        f"{safe_basename}.summary.{extension}",
    )

    ensure_directory(output_dir)

    if fmt == "markdown":
        markdown_content = f"# Summary: {basename}\n\n{summary.strip()}\n"
        content = mdformat.text(markdown_content, options={"number": True})
    else:
        content = yaml.safe_dump({"summary": summary}, sort_keys=False)

    write_text(output_path, content)
    return output_path


def write_chunk_file(
    chunk_text: str,
    output_dir: str,
    meeting_type: str,
    base_name: str,
    pass_num: int,
    chunk_id: int,
) -> str:
    """
    Write one summarized chunk to a structured debug output path.

    Chunk files are grouped by transcript basename, detected meeting type, and
    summarization pass so intermediate model output can be inspected without
    changing final summary behavior.

    :param chunk_text: Summarized chunk text to persist
    :param output_dir: Root output directory for intelligence artifacts
    :param meeting_type: Detected or assigned meeting type label
    :param base_name: Source transcript basename
    :param pass_num: Summarization pass number
    :param chunk_id: One-based chunk number within the pass
    :return: Full path to the written chunk file
    :raises ConfigError: If the directory or file cannot be written
    :raises ValidationError: If a path component escapes the output root
    """
    safe_base_name = sanitize_path_component(base_name)
    safe_meeting_type = sanitize_path_component(meeting_type)

    chunk_dir = resolve_contained_path(
        output_dir,
        "chunks",
        safe_base_name,
        safe_meeting_type,
        f"pass_{pass_num}",
    )
    chunk_filename = f"{safe_base_name}_part{chunk_id:02d}.txt"
    chunk_path = resolve_contained_path(chunk_dir, chunk_filename)

    ensure_directory(chunk_dir)
    write_text(chunk_path, chunk_text)
    return chunk_path
