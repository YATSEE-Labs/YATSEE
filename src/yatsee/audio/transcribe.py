"""
Audio transcription stage for YATSEE.

This module ports the existing transcription stage behind reusable functions so
the new CLI can invoke it without embedding stage logic directly.

Behavior intentionally mirrors the current standalone script:
- global + entity config resolution
- hotword flattening from people aliases and entity terms
- CPU/CUDA/MPS device selection
- optional faster-whisper backend
- safe default transcription behavior for normal runs and QA rebuilds
- chunk-directory support
- SHA-256 tracker for idempotent reruns
- VTT output in transcripts_<model>/
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import sys
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Set

from tqdm import tqdm

from yatsee.audio.ffmpeg import get_audio_duration as probe_audio_duration
from yatsee.core.config import load_entity_config, load_global_config
from yatsee.core.discovery import discover_files
from yatsee.core.errors import ValidationError
from yatsee.core.hashing import compute_sha256
from yatsee.core.runtime import clear_torch_cache, resolve_torch_device
from yatsee.core.tracking import append_tracker_value, load_tracker_set

HAS_FASTER_WHISPER = importlib.util.find_spec("faster_whisper") is not None
SUPPORTED_INPUT_EXTENSIONS = (".mp3", ".wav", ".flac", ".m4a")
MIN_TRANSCRIBABLE_CHUNK_SECONDS = 1.0
DEFAULT_TRANSCRIPTION_PROFILE = "default"
SUPPORTED_TRANSCRIPTION_PROFILES = ("default",)


def load_flat_hotwords(entity_cfg: Dict[str, Any]) -> Optional[str]:
    """
    Flatten people aliases and entity terms into a comma-separated hotwords string.

    This intentionally excludes normal title buckets such as mayor, clerk,
    alderperson, directors, and staff.

    :param entity_cfg: Merged configuration dictionary for the entity
    :return: Comma-separated string of hotwords, or None if no hotwords found
    """
    hotwords: Set[str] = set()

    # People aliases, including people.legacy.
    for role_dict in entity_cfg.get("people", {}).values():
        for aliases in role_dict.values():
            if isinstance(aliases, list):
                hotwords.update(alias.strip() for alias in aliases if alias.strip())

    # Entity terms, if present.
    for entity_list in entity_cfg.get("entities", {}).values():
        if isinstance(entity_list, list):
            hotwords.update(entity.strip() for entity in entity_list if entity.strip())

    return ", ".join(sorted(hotwords)) if hotwords else None


CIVIC_SUPPRESSION_CONTEXT_PATTERNS = (
    r"\btake the roll\b",
    r"\bcall the roll\b",
    r"\broll call\b",
    r"\bmadam clerk\b",
    r"\bclerk[, ]+(?:would|could|please)\b",
    r"\bmotion (?:passes|passed|fails|failed)\b",
    r"\bordinance (?:passes|passed|fails|failed)\b",
    r"\bresolution (?:is )?(?:adopted|approved|passed|passes)\b",
    r"\b(?:passes|passed|fails|failed|approved|adopted) \d+\s*(?:-|to)\s*\d+\b",
    (
        r"\b(?:passes|passed|fails|failed|approved|adopted) "
        r"(?:one|two|three|four|five|six|seven|eight|nine|zero) to "
        r"(?:one|two|three|four|five|six|seven|eight|nine|zero)\b"
    ),
    r"\btie(?:d)? at \d+\s*(?:-|to)\s*\d+\b",
    r"\bthe motion is (?:approved|adopted|defeated)\b",
)


def has_civic_suppression_context(previous_text: str, next_text: str) -> bool:
    """
    Return True when neighboring transcript text implies roll-call or vote context.

    Hotword hallucinations and legitimate roll-call/vote name lists can look
    similar when a segment is inspected by itself. Neighboring cues provide the
    safest low-cost signal: a preceding request to take the roll or a following
    vote outcome means the candidate segment is civic structure and should be
    preserved for downstream review.

    :param previous_text: Nearby text before the candidate segment
    :param next_text: Nearby text after the candidate segment
    :return: True when suppression should be bypassed
    """
    context = f"{previous_text} {next_text}".casefold()
    return any(re.search(pattern, context) for pattern in CIVIC_SUPPRESSION_CONTEXT_PATTERNS)


def collect_neighbor_text(segments: Sequence[Any], index: int, before: bool, max_segments: int = 2) -> str:
    """
    Collect a small amount of adjacent segment text for suppression decisions.

    The suppressor needs only immediate context. Keeping the window small avoids
    preserving true hotword garbage merely because the meeting contains a vote
    elsewhere nearby.

    :param segments: Ordered transcript segments
    :param index: Candidate segment index
    :param before: Whether to collect text before or after the candidate
    :param max_segments: Maximum neighboring segments to include
    :return: Joined neighboring text
    """
    if before:
        start = max(0, index - max_segments)
        neighbors = segments[start:index]
    else:
        end = min(len(segments), index + max_segments + 1)
        neighbors = segments[index + 1:end]

    return " ".join(
        str(getattr(seg, "text", "")).strip()
        for seg in neighbors
        if getattr(seg, "text", "")
    ).strip()


def suppress_hotword_write(
    text: str,
    start: float,
    end: float,
    hotwords: Optional[str],
    previous_text: str = "",
    next_text: str = "",
) -> bool:
    """
    Return True when a segment is obvious hotword/name-list garbage.

    This intentionally suppresses only comma-heavy hotword hallucinations before
    VTT write while leaving roll calls, votes, motions, and normal civic text intact.
    Neighboring roll-call or vote context protects name-list segments because
    individual vote details are more valuable than aggressively deleting every
    suspicious roster-like cue.

    :param text: Segment text
    :param start: Segment start timestamp in seconds
    :param end: Segment end timestamp in seconds
    :param hotwords: Runtime hotword string passed to ASR
    :param previous_text: Nearby text before this segment
    :param next_text: Nearby text after this segment
    :return: True when the segment should not be written to VTT
    """
    if not hotwords:
        return False

    if end - start < 5.0:
        return False

    words = re.findall(r"[a-z]+", text.casefold())
    if text.count(",") < 3 or len(words) < 4 or len(words) > 40:
        return False

    safe_terms = {
        "absent",
        "adopted",
        "alderman",
        "aldermen",
        "alderperson",
        "alderpersons",
        "amendment",
        "approve",
        "approved",
        "aye",
        "board",
        "call",
        "clerk",
        "comment",
        "failed",
        "fails",
        "favor",
        "here",
        "mayor",
        "meeting",
        "motion",
        "nay",
        "no",
        "opposed",
        "ordinance",
        "passed",
        "passes",
        "present",
        "public",
        "quorum",
        "resolution",
        "roll",
        "second",
        "township",
        "yes",
    }
    if any(word in safe_terms for word in words):
        return False

    if has_civic_suppression_context(previous_text, next_text):
        return False

    hotword_words = set(re.findall(r"[a-z]+", hotwords.casefold()))
    hits = [word for word in words if word in hotword_words]

    return len(set(hits)) >= 6 and len(hits) >= 7 and (len(hits) / len(words)) >= 0.70


def get_audio_duration(audio_path: str) -> float:
    """
    Return the duration of an audio file in seconds using ffprobe.

    The transcription stage validates media at the final trust boundary before
    ASR. This keeps empty or invalid chunks from being interpreted through
    hotwords or previous-context prompts.

    :param audio_path: Path to the audio file
    :return: Duration in seconds as a float
    :raises RuntimeError: If file cannot be read or has no positive duration
    """
    success, duration, message = probe_audio_duration(audio_path)
    if not success or duration is None:
        raise RuntimeError(message)

    return duration


def should_skip_audio_chunk(
    audio_path: str,
    min_duration: float = MIN_TRANSCRIBABLE_CHUNK_SECONDS,
) -> tuple[bool, str]:
    """
    Decide whether an audio chunk is safe to pass to ASR.

    Empty, unreadable, or tiny chunks are poor ASR inputs. With hotwords and
    previous-text conditioning, they can produce repeated filler text instead
    of clean silence. The check is intentionally duration-based only so quiet
    but real speech is not filtered out before transcription.

    :param audio_path: Path to the candidate audio chunk
    :param min_duration: Minimum valid duration in seconds
    :return: Tuple(skip, reason)
    """
    if not os.path.exists(audio_path):
        return True, "missing file"

    try:
        if os.path.getsize(audio_path) == 0:
            return True, "zero-byte file"
    except OSError as exc:
        return True, f"cannot stat file: {exc}"

    try:
        duration = get_audio_duration(audio_path)
    except RuntimeError as exc:
        return True, str(exc)

    if duration < min_duration:
        return True, f"duration {duration:.3f}s is shorter than minimum {min_duration:.3f}s"

    return False, ""


def format_vtt_timestamp(seconds: float) -> str:
    """
    Convert seconds to WebVTT timestamp format HH:MM:SS.mmm.

    :param seconds: Time in seconds
    :return: Formatted timestamp string
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}.{millis:03}"


def normalize_segments(segments: list[Any]) -> list[SimpleNamespace]:
    """
    Convert transcription segments to objects with start, end, and text attributes.

    :param segments: List of segment dicts or objects
    :return: List of normalized segment objects
    """
    normalized: list[SimpleNamespace] = []
    for seg in segments:
        if isinstance(seg, dict):
            normalized.append(
                SimpleNamespace(
                    start=seg["start"],
                    end=seg["end"],
                    text=seg["text"],
                )
            )
        else:
            normalized.append(seg)
    return normalized


def _coerce_positive_int(value: Any, default: int) -> int:
    """
    Convert a config or CLI value to a positive integer with a safe fallback.

    :param value: Value to parse
    :param default: Fallback value
    :return: Positive integer
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default

    return parsed if parsed > 0 else default


def _coerce_nonnegative_int(value: Any, default: int) -> int:
    """
    Convert a config or CLI value to a non-negative integer with a safe fallback.

    :param value: Value to parse
    :param default: Fallback value
    :return: Non-negative integer
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default

    return parsed if parsed >= 0 else default


def _first_config_value(config: Dict[str, Any], keys: Sequence[str]) -> Any:
    """
    Return the first configured value from a list of accepted key names.

    :param config: Configuration dictionary to inspect
    :param keys: Candidate key names in priority order
    :return: First configured value, or None
    """
    for key in keys:
        if key in config:
            return config[key]

    return None


def normalize_transcription_profile_name(profile_name: str | None) -> str:
    """
    Normalize a transcription profile name supplied by the CLI or caller.

    Profiles are intentionally preset-only for now. They are not loaded from
    entity TOML so operators can choose a supported behavior without tuning
    low-level Whisper parameters.

    :param profile_name: Requested profile name
    :return: Normalized profile name
    """
    if not profile_name:
        return DEFAULT_TRANSCRIPTION_PROFILE

    return profile_name.strip().casefold().replace("-", "_")


def resolve_transcription_profile(profile_name: str | None) -> Dict[str, Any]:
    """
    Resolve a named transcription behavior preset.

    The default profile is the supported baseline for normal transcription and
    QA rebuilds. Previous-text conditioning stays disabled so adjacent ASR loop
    artifacts are less likely to become durable transcript output.

    This is intentionally a small built-in preset resolver rather than a
    user-editable TOML profile surface. Arbitrary Whisper tuning is not exposed
    here because the supported operator decision is whether to run or force a
    rebuild with the current safe defaults.

    :param profile_name: Requested profile name
    :return: Dictionary of transcription settings
    :raises ValidationError: If the profile is unknown
    """
    normalized_name = normalize_transcription_profile_name(profile_name)

    base_settings: Dict[str, Any] = {
        "name": "default",
        "condition_on_previous_text": False,
        "beam_size": 5,
        "no_speech_threshold": 0.6,
        "log_prob_threshold": -1.0,
        "compression_ratio_threshold": 2.4,
        "no_repeat_ngram_size": 0,
        "vad_filter_enabled": True,
    }

    if normalized_name == "default":
        return dict(base_settings)

    supported = ", ".join(SUPPORTED_TRANSCRIPTION_PROFILES)
    raise ValidationError(
        f"Unsupported transcription profile '{profile_name}'. Supported profiles: {supported}"
    )


def resolve_chunk_settings(
    global_cfg: Dict[str, Any],
    entity_cfg: Dict[str, Any],
    chunk_duration: int | None,
    chunk_overlap: int | None,
) -> tuple[int, int]:
    """
    Resolve chunk timing settings for timestamp reconstruction.

    The formatter already creates chunks using these settings. The transcriber
    needs the same timing contract so it can map per-chunk segment timestamps
    back onto the original source timeline. Multiple key aliases are accepted
    to avoid coupling this stage to one historical config spelling.

    :param global_cfg: Global YATSEE configuration
    :param entity_cfg: Entity-specific configuration
    :param chunk_duration: Optional direct duration override
    :param chunk_overlap: Optional direct overlap override
    :return: Tuple of chunk duration and overlap in seconds
    """
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

    if chunk_duration is None:
        chunk_duration = _first_config_value(
            media_cfg,
            (
                "chunk_duration",
                "chunk_seconds",
                "chunk_duration_seconds",
                "audio_chunk_duration",
                "audio_chunk_seconds",
            ),
        )

    if chunk_overlap is None:
        chunk_overlap = _first_config_value(
            media_cfg,
            (
                "chunk_overlap",
                "chunk_overlap_seconds",
                "overlap_seconds",
                "audio_chunk_overlap",
                "audio_chunk_overlap_seconds",
            ),
        )

    resolved_duration = _coerce_positive_int(chunk_duration, 600)
    resolved_overlap = _coerce_nonnegative_int(chunk_overlap, 2)

    if resolved_overlap >= resolved_duration:
        resolved_overlap = 0

    return resolved_duration, resolved_overlap


def resolve_chunk_step(
    audio_chunks: Sequence[str],
    total_duration: float,
    chunk_duration: int,
    chunk_overlap: int,
) -> float:
    """
    Resolve the source-timeline distance between consecutive chunk starts.

    Prefer inference from the original audio duration and final chunk duration,
    because that remains correct even if transcription was not explicitly passed
    the formatter's chunk settings. Fall back to duration minus overlap when the
    final chunk duration cannot be read.

    :param audio_chunks: Ordered chunk file paths
    :param total_duration: Original source audio duration in seconds
    :param chunk_duration: Configured chunk duration
    :param chunk_overlap: Configured chunk overlap
    :return: Step between chunk starts in seconds
    """
    fallback_step = float(chunk_duration - chunk_overlap)

    if len(audio_chunks) <= 1:
        return fallback_step

    try:
        final_chunk_duration = get_audio_duration(audio_chunks[-1])
    except Exception:
        return fallback_step

    inferred_step = (total_duration - final_chunk_duration) / (len(audio_chunks) - 1)

    if inferred_step <= 0:
        return fallback_step

    return inferred_step


def resolve_device(device_arg: str, faster: bool) -> tuple[str, bool]:
    """
    Resolve runtime device and fp16 usage.

    :param device_arg: Requested device: auto, cuda, cpu, mps
    :param faster: Whether faster-whisper was requested
    :return: Tuple of (device, use_fp16)
    """
    device = resolve_torch_device(device_arg, allow_mps=not faster)
    return device, device == "cuda"


def load_transcription_model(model: str, device: str, use_fp16: bool, faster: bool) -> tuple[Any, bool]:
    """
    Load the requested transcription backend.

    :param model: Whisper model name
    :param device: Resolved runtime device
    :param use_fp16: Whether fp16 should be enabled
    :param faster: Whether faster-whisper was requested
    :return: Tuple of (model_instance, using_faster_whisper)
    """
    if faster and HAS_FASTER_WHISPER:
        from faster_whisper import WhisperModel

        compute_type = "float16" if use_fp16 else "int8"
        return WhisperModel(model, device=device, compute_type=compute_type), True

    import whisper

    whisper_model = whisper.load_model(model).to(device)
    return whisper_model, False


def resolve_transcribe_paths(
    global_config_path: str,
    entity: str | None,
    audio_input: str | None,
    output_dir: str | None,
    model_override: str | None,
) -> Dict[str, Any]:
    """
    Resolve config and filesystem paths for the transcription stage.

    :param global_config_path: Path to global yatsee.toml
    :param entity: Optional entity handle
    :param audio_input: Optional direct input override
    :param output_dir: Optional direct output override
    :param model_override: Optional model override
    :return: Dictionary of resolved config and paths
    :raises ValidationError: If required arguments are missing
    """
    entity_cfg: Dict[str, Any] = {}
    global_cfg = load_global_config(global_config_path)

    if entity:
        entity_cfg = load_entity_config(global_cfg, entity)
    else:
        if not audio_input or not output_dir:
            raise ValidationError(
                "Without --entity, both --audio-input and --output-dir must be defined"
            )

    model = model_override or entity_cfg.get("transcription_model", "small")
    resolved_input = audio_input or os.path.join(entity_cfg.get("data_path"), "audio")
    resolved_output = output_dir or os.path.join(entity_cfg.get("data_path"), f"transcripts_{model}")

    return {
        "global_cfg": global_cfg,
        "entity_cfg": entity_cfg,
        "audio_input": resolved_input,
        "output_dir": resolved_output,
        "model": model,
    }


def run_transcribe_stage(
    global_config_path: str,
    entity: str | None = None,
    audio_input: str | None = None,
    output_dir: str | None = None,
    get_chunks: bool = False,
    model_override: str | None = None,
    faster: bool = False,
    language: str = "en",
    device_arg: str = "auto",
    verbose: bool = False,
    quiet: bool = False,
    chunk_duration: int | None = None,
    chunk_overlap: int | None = None,
    transcription_profile: str | None = DEFAULT_TRANSCRIPTION_PROFILE,
) -> Dict[str, Any]:
    """
    Run the audio transcription stage.

    :param global_config_path: Path to global yatsee.toml
    :param entity: Optional entity handle
    :param audio_input: Optional input override
    :param output_dir: Optional output override
    :param get_chunks: Whether to use chunk directories when present
    :param model_override: Optional model override
    :param faster: Whether to use faster-whisper
    :param language: Language code or 'auto'
    :param device_arg: Requested runtime device
    :param verbose: Enable verbose transcription output
    :param quiet: Suppress progress output
    :param chunk_duration: Optional chunk duration override for timestamp reconstruction
    :param chunk_overlap: Optional chunk overlap override for timestamp reconstruction
    :param transcription_profile: Named transcription behavior preset; default is the supported baseline
    :return: Summary dictionary describing stage results
    """
    resolved = resolve_transcribe_paths(
        global_config_path=global_config_path,
        entity=entity,
        audio_input=audio_input,
        output_dir=output_dir,
        model_override=model_override,
    )

    global_cfg = resolved["global_cfg"]
    entity_cfg = resolved["entity_cfg"]
    model = resolved["model"]
    audio_input_path = resolved["audio_input"]
    output_directory = resolved["output_dir"]

    chunk_duration, chunk_overlap = resolve_chunk_settings(
        global_cfg=global_cfg,
        entity_cfg=entity_cfg,
        chunk_duration=chunk_duration,
        chunk_overlap=chunk_overlap,
    )
    profile_settings = resolve_transcription_profile(transcription_profile)

    audio_file_list = discover_files(audio_input_path, SUPPORTED_INPUT_EXTENSIONS)
    if not audio_file_list:
        return {
            "audio_input": audio_input_path,
            "output_dir": output_directory,
            "model": model,
            "discovered": 0,
            "processed": 0,
            "skipped": 0,
            "messages": [f"No audio input files found at {audio_input_path}"],
        }

    os.makedirs(output_directory, exist_ok=True)

    device, use_fp16 = resolve_device(device_arg, faster)
    whisper_model, use_faster_whisper = load_transcription_model(model, device, use_fp16, faster)

    hotwords = load_flat_hotwords(entity_cfg)
    lang = None if language.lower() == "auto" else language

    hash_tracker = os.path.join(output_directory, ".vtt_hash")
    existing_hashes = load_tracker_set(hash_tracker)

    processed = 0
    skipped = 0
    messages: list[str] = []

    for audio_path in audio_file_list:
        base_name = os.path.splitext(os.path.basename(audio_path))[0]
        vtt_filepath = os.path.join(output_directory, f"{base_name}.vtt")

        file_hash = compute_sha256(audio_path)
        video_id = base_name.split(".", 1)[0]
        hash_key = f"{video_id}:{file_hash}"

        if hash_key in existing_hashes:
            skipped += 1
            messages.append(f"Skipped already transcribed: {audio_path}")
            continue

        audio_directory = audio_input_path
        if os.path.isfile(audio_input_path):
            audio_directory = os.path.dirname(audio_input_path)

        chunk_dir = os.path.join(audio_directory, "chunks", base_name)
        if get_chunks and os.path.isdir(chunk_dir):
            audio_chunks = sorted(
                os.path.join(chunk_dir, entry)
                for entry in os.listdir(chunk_dir)
                if entry.lower().endswith(SUPPORTED_INPUT_EXTENSIONS)
            )
        else:
            audio_chunks = [audio_path]

        try:
            total_duration = get_audio_duration(audio_path)
        except RuntimeError as exc:
            skipped += 1
            messages.append(f"Skipped unreadable audio '{audio_path}': {exc}")
            continue

        if not audio_chunks:
            skipped += 1
            messages.append(f"Skipped audio with no transcribable chunks: {audio_path}")
            continue

        progress_bar = None
        if sys.stdout.isatty() and not verbose and not quiet:
            progress_bar = tqdm(
                total=total_duration,
                unit="sec",
                desc=f"Transcribing {base_name}",
                dynamic_ncols=True,
                bar_format="{l_bar}{bar}| {n:.1f}/{total:.1f} {unit} [{elapsed}<{remaining}, {rate_fmt}]",
            )

        all_segments: list[Any] = []
        last_emitted_end = 0.0
        last_progress_end = 0.0
        chunk_step = resolve_chunk_step(
            audio_chunks=audio_chunks,
            total_duration=total_duration,
            chunk_duration=chunk_duration,
            chunk_overlap=chunk_overlap,
        )
        start_time = time.time()

        for index, audio_chunk in enumerate(audio_chunks, start=1):
            if progress_bar:
                progress_bar.set_description(f"Transcribing {base_name} | Chunk {index}/{len(audio_chunks)}")
                progress_bar.refresh()

            skip_chunk, skip_reason = should_skip_audio_chunk(audio_chunk)
            if skip_chunk:
                messages.append(
                    f"Skipped invalid chunk {index}/{len(audio_chunks)} for '{audio_path}': {skip_reason}"
                )
                continue

            try:
                if use_faster_whisper:
                    segments, _info = whisper_model.transcribe(
                        audio_chunk,
                        hotwords=hotwords,
                        log_progress=verbose,
                        beam_size=profile_settings["beam_size"],
                        language=lang,
                        condition_on_previous_text=profile_settings["condition_on_previous_text"],
                        no_speech_threshold=profile_settings["no_speech_threshold"],
                        log_prob_threshold=profile_settings["log_prob_threshold"],
                        compression_ratio_threshold=profile_settings["compression_ratio_threshold"],
                        no_repeat_ngram_size=profile_settings["no_repeat_ngram_size"],
                        vad_filter=profile_settings["vad_filter_enabled"],
                        vad_parameters=None,
                    )
                else:
                    result = whisper_model.transcribe(
                        audio_chunk,
                        initial_prompt=hotwords,
                        verbose=verbose,
                        language=lang,
                        fp16=use_fp16,
                        condition_on_previous_text=profile_settings["condition_on_previous_text"],
                        no_speech_threshold=profile_settings["no_speech_threshold"],
                        log_prob_threshold=profile_settings["log_prob_threshold"],
                        compression_ratio_threshold=profile_settings["compression_ratio_threshold"],
                    )
                    segments = result.get("segments", []) if isinstance(result, dict) else []
            except Exception as exc:
                messages.append(f"Error transcribing '{audio_chunk}': {exc}")
                continue

            normalized = normalize_segments(list(segments))
            if not normalized:
                continue

            chunk_start = (index - 1) * chunk_step if get_chunks and len(audio_chunks) > 1 else 0.0

            for seg in normalized:
                chunk_local_start = float(seg.start)
                chunk_local_end = float(seg.end)

                seg.start = chunk_start + chunk_local_start
                seg.end = chunk_start + chunk_local_end

                # Overlapped chunks can produce duplicate segments for the same
                # source audio. Keep the earliest emitted text for any fully
                # duplicated time range.
                if seg.end <= last_emitted_end:
                    continue

                if seg.start < last_emitted_end:
                    seg.start = last_emitted_end

                if seg.end <= seg.start:
                    continue

                all_segments.append(seg)
                last_emitted_end = max(last_emitted_end, seg.end)

                if progress_bar:
                    capped_end = min(seg.end, total_duration)
                    progress_bar.update(max(0.0, capped_end - last_progress_end))
                    last_progress_end = max(last_progress_end, capped_end)

            clear_torch_cache()

        all_segments.sort(key=lambda seg: seg.start)

        with open(vtt_filepath, "w", encoding="utf-8") as vtt_file:
            vtt_file.write("WEBVTT\n\n")
            for segment_index, seg in enumerate(all_segments):
                text = seg.text.strip()
                previous_text = collect_neighbor_text(all_segments, segment_index, before=True)
                next_text = collect_neighbor_text(all_segments, segment_index, before=False)

                # Suppress only obvious hotword/name-list hallucinations before
                # they become durable transcript artifacts. Immediate transcript
                # context protects roll-call and vote name lists from destructive
                # suppression.
                if suppress_hotword_write(
                    text,
                    seg.start,
                    seg.end,
                    hotwords,
                    previous_text=previous_text,
                    next_text=next_text,
                ):
                    messages.append(
                        "Suppressed likely hotword hallucination from VTT output: "
                        f"{base_name} {format_vtt_timestamp(seg.start)} --> "
                        f"{format_vtt_timestamp(seg.end)} | {text}"
                    )
                    continue

                start_ts = format_vtt_timestamp(seg.start)
                end_ts = format_vtt_timestamp(seg.end)
                vtt_file.write(f"{start_ts} --> {end_ts}\n{text}\n\n")

        if progress_bar:
            progress_bar.n = progress_bar.total
            progress_bar.refresh()
            progress_bar.close()

        append_tracker_value(hash_tracker, hash_key)
        processed += 1
        elapsed = round(time.time() - start_time, 2)
        backend_name = "faster-whisper" if use_faster_whisper else "whisper"
        messages.append(
            f"Transcribed successfully: {vtt_filepath} "
            f"(chunks={len(audio_chunks)}, elapsed={elapsed}s, "
            f"backend={backend_name}, profile={profile_settings['name']})"
        )

    return {
        "audio_input": audio_input_path,
        "output_dir": output_directory,
        "model": model,
        "device": device,
        "backend": "faster-whisper" if use_faster_whisper else "whisper",
        "transcription_profile": profile_settings["name"],
        "discovered": len(audio_file_list),
        "processed": processed,
        "skipped": skipped,
        "messages": messages,
    }