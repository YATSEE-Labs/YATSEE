"""
Audio normalization stage for YATSEE.

This module wraps the current format-audio behavior behind reusable functions
so the new CLI can call it without embedding stage logic directly.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Set

from yatsee.audio.ffmpeg import chunk_audio_file, format_audio, get_audio_duration
from yatsee.core.config import load_global_config, load_entity_config
from yatsee.core.discovery import discover_files
from yatsee.core.errors import ValidationError
from yatsee.core.hashing import compute_sha256
from yatsee.core.paths import get_entity_dir
from yatsee.core.tracking import append_tracker_value, load_tracker_set

SUPPORTED_INPUT_EXTENSIONS = (".m4a", ".mp4", ".webm", ".mp3", ".wav", ".flac", ".mov", ".mkv")


def resolve_format_paths(
    global_config_path: str,
    entity: str | None,
    input_dir: str | None,
    output_dir: str | None,
) -> Dict[str, Any]:
    """
    Resolve config and filesystem paths for the audio format stage.

    :param global_config_path: Path to global yatsee.toml
    :param entity: Optional entity handle
    :param input_dir: Optional direct input override
    :param output_dir: Optional direct output override
    :return: Dictionary containing resolved config and paths
    :raises ValidationError: If required arguments are missing
    """
    entity_cfg: Dict[str, Any] = {}
    global_cfg = load_global_config(global_config_path)

    if entity:
        entity_cfg = load_entity_config(global_cfg, entity)
    else:
        if not input_dir or not output_dir:
            raise ValidationError(
                "Without --entity, both --input-dir and --output-dir must be defined"
            )

    if entity:
        data_path = entity_cfg.get("data_path") or get_entity_dir(global_cfg, entity)
        data_path = os.path.abspath(data_path)

        global_media_cfg = global_cfg.get("media", {})
        entity_media_cfg = entity_cfg.get("media", {})

        if not isinstance(global_media_cfg, dict):
            global_media_cfg = {}

        if not isinstance(entity_media_cfg, dict):
            entity_media_cfg = {}

        media_cfg = {
            **global_media_cfg,
            **entity_media_cfg,
        }

        resolved_paths = {}

        for name, override, configured in (
                ("downloads_path", input_dir, media_cfg.get("input_dir") or "downloads"),
                ("audio_out", output_dir, media_cfg.get("audio_dir") or "audio"),
        ):
            if override:
                resolved_paths[name] = override
            elif os.path.isabs(configured):
                resolved_paths[name] = configured
            else:
                resolved_paths[name] = os.path.abspath(os.path.join(data_path, configured))

        downloads_path = resolved_paths["downloads_path"]
        audio_out = resolved_paths["audio_out"]

    else:
        downloads_path = input_dir
        audio_out = output_dir

    return {
        "global_cfg": global_cfg,
        "entity_cfg": entity_cfg,
        "downloads_path": downloads_path,
        "audio_out": audio_out,
        "chunk_root_dir": os.path.join(audio_out, "chunks"),
    }


def run_format_stage(
    global_config_path: str,
    entity: str | None = None,
    input_dir: str | None = None,
    output_dir: str | None = None,
    file_format: str = "flac",
    create_chunks: bool = False,
    chunk_duration: int = 600,
    chunk_overlap: int = 2,
    dry_run: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Run the audio normalization stage.

    This keeps the stage logic deterministic and reusable for both the new CLI
    and future tests.

    :param global_config_path: Path to global yatsee.toml
    :param entity: Optional entity handle
    :param input_dir: Optional input override
    :param output_dir: Optional output override
    :param file_format: Output format, 'wav' or 'flac'
    :param create_chunks: Whether to split converted audio into chunks
    :param chunk_duration: Duration of each chunk in seconds
    :param chunk_overlap: Overlap in seconds between chunks
    :param dry_run: Preview actions without writing files
    :param force: Reprocess files even if already converted
    :return: Summary dictionary describing stage results
    """
    resolved = resolve_format_paths(global_config_path, entity, input_dir, output_dir)
    downloads_path = resolved["downloads_path"]
    audio_out = resolved["audio_out"]
    chunk_root_dir = resolved["chunk_root_dir"]

    try:
        input_files = discover_files(downloads_path, SUPPORTED_INPUT_EXTENSIONS)
    except FileNotFoundError as exc:
        if input_dir:
            raise ValidationError(
                "No input files found. The path provided with -i/--input-dir does not exist: "
                f"{downloads_path}"
            ) from exc

        raise ValidationError(
            "No input files found in the default media input directory: "
            f"{downloads_path}\n"
            "Place input files there, or use -i/--input-dir to point at another source."
        ) from exc

    if not input_files:
        if input_dir:
            raise ValidationError(
                "No supported input files found in the path provided with -i/--input-dir: "
                f"{downloads_path}"
            )

        raise ValidationError(
            "No supported input files found in the default media input directory: "
            f"{downloads_path}\n"
            "Place input files there, or use -i/--input-dir to point at another source."
        )

    messages: List[str] = []

    if not dry_run:
        os.makedirs(audio_out, exist_ok=True)
        if create_chunks:
            os.makedirs(chunk_root_dir, exist_ok=True)

    hash_tracker = os.path.join(audio_out, ".converted")
    converted_hashes: Set[str] = set()
    if not force:
        converted_hashes = load_tracker_set(hash_tracker)

    processed = 0
    skipped = 0
    chunked = 0

    for src_path in input_files:
        file_hash = compute_sha256(src_path)

        if file_hash in converted_hashes and not force:
            skipped += 1
            messages.append(f"Skipped already converted: {src_path}")
            continue

        base_name = os.path.splitext(os.path.basename(src_path))[0]
        out_path = os.path.join(audio_out, f"{base_name}.{file_format}")

        if dry_run:
            processed += 1
            messages.append(f"Dry run: would convert {src_path} -> {out_path}")
            continue

        success, msg = format_audio(input_src=src_path, output_path=out_path, file_format=file_format)
        messages.append(msg)

        if not success:
            continue

        processed += 1
        append_tracker_value(hash_tracker, file_hash)

        if create_chunks:
            chunk_out_path = os.path.join(chunk_root_dir, base_name)
            os.makedirs(chunk_out_path, exist_ok=True)

            dur_success, total_duration, dur_msg = get_audio_duration(out_path)
            if not dur_success or total_duration is None:
                messages.append(dur_msg)
                continue

            chunk_success, chunks, chunk_msg = chunk_audio_file(
                input_file=out_path,
                output_dir=chunk_out_path,
                total_duration=total_duration,
                chunk_duration=chunk_duration,
                overlap=chunk_overlap,
            )
            messages.append(chunk_msg)

            if chunk_success:
                chunked += len(chunks)

    return {
        "input_dir": downloads_path,
        "output_dir": audio_out,
        "discovered": len(input_files),
        "processed": processed,
        "skipped": skipped,
        "chunked": chunked,
        "messages": messages,
    }