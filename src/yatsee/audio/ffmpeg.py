"""
Low-level ffmpeg and ffprobe helpers for YATSEE audio processing.

These helpers isolate external process execution so the higher-level audio
format stage remains focused on pipeline behavior instead of shell details.
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional, Tuple


MIN_VALID_CHUNK_DURATION_SECONDS = 1.0


def get_audio_duration(input_file: str) -> Tuple[bool, Optional[float], str]:
    """
    Determine the duration of an audio file in seconds using ffprobe.

    :param input_file: Path to the FLAC or WAV audio file
    :return: Tuple(success, duration, message)
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        input_file,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        duration_text = result.stdout.strip()
        if not duration_text:
            return False, None, f"ffprobe returned no duration for {input_file}"

        duration = float(duration_text)
        if duration <= 0:
            return False, None, f"ffprobe returned non-positive duration for {input_file}: {duration}"

        return True, duration, f"Duration: {duration}s"
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else str(exc)
        return False, None, f"ffprobe failed for {input_file}: {stderr}"
    except Exception as exc:
        return False, None, f"Unexpected error for {input_file}: {exc}"


def format_audio(input_src: str, output_path: str, file_format: str = "flac") -> Tuple[bool, str]:
    """
    Convert media files to mono 16kHz audio for transcription.

    :param input_src: Path to the input media file
    :param output_path: Full path for the normalized output file
    :param file_format: Desired audio format: 'wav' or 'flac'
    :return: Tuple(success, message)
    """
    if file_format not in {"wav", "flac"}:
        return False, f"Unsupported format: {file_format}"

    codec = "pcm_s16le" if file_format == "wav" else "flac"
    ASR_AUDIO_FILTER = "highpass=f=80,lowpass=f=7600,dynaudnorm=f=250:g=15:p=0.85:m=8"

    cmd = [
        "ffmpeg",
        "-y",
        "-vn",
        "-i",
        input_src,
        "-af",
        ASR_AUDIO_FILTER,
        "-ar",
        "16000",
        "-ac",
        "1",
        "-sample_fmt",
        "s16",
        "-c:a",
        codec,
        output_path,
    ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return True, f"Converted successfully: {output_path}"
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="ignore") if exc.stderr else str(exc)
        return False, f"ffmpeg failed for {input_src}: {stderr}"


def chunk_audio_file(
    input_file: str,
    output_dir: str,
    total_duration: float,
    chunk_duration: int = 600,
    overlap: int = 2,
) -> Tuple[bool, list[str], str]:
    """
    Split a long audio file into sequential smaller FLAC chunks.

    Each chunk is decoded and re-encoded instead of stream-copied so that
    duration, sample count, and container metadata remain accurate for strict
    downstream ASR loaders such as NeMo/Lhotse.

    :param input_file: Path to the source audio file
    :param output_dir: Directory where chunk files will be written
    :param total_duration: Total length of the audio in seconds
    :param chunk_duration: Duration of each chunk in seconds
    :param overlap: Overlap in seconds between consecutive chunks
    :return: Tuple(success, chunks, message)
    """
    if chunk_duration <= 0:
        return False, [], "Chunk duration must be greater than zero"

    if overlap < 0:
        return False, [], "Chunk overlap cannot be negative"

    if overlap >= chunk_duration:
        return False, [], "Chunk overlap must be smaller than chunk duration"

    if total_duration <= 0:
        return False, [], "Total duration must be greater than zero"

    chunks: list[str] = []
    skipped_tiny_chunks = 0
    start = 0.0
    idx = 0
    step = chunk_duration - overlap

    try:
        while start < total_duration:
            actual_duration = min(chunk_duration, total_duration - start)

            # Avoid creating tiny trailing chunks caused by float precision or
            # overlap math near the end of the source file. These chunks are
            # poor ASR inputs and can trigger prompt/hotword-driven hallucinations.
            if actual_duration < MIN_VALID_CHUNK_DURATION_SECONDS:
                skipped_tiny_chunks += 1
                break

            out_file = os.path.join(output_dir, f"{idx:03d}.flac")

            cmd = [
                "ffmpeg",
                "-y",
                "-vn",
                "-i",
                input_file,
                "-ss",
                str(max(0.0, start)),
                "-t",
                str(actual_duration),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-sample_fmt",
                "s16",
                "-c:a",
                "flac",
                out_file,
            ]

            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

            dur_success, chunk_actual_duration, dur_msg = get_audio_duration(out_file)
            if not dur_success or chunk_actual_duration is None:
                try:
                    os.remove(out_file)
                except OSError:
                    pass
                return False, chunks, f"Failed to validate chunk {idx}: {dur_msg}"

            if chunk_actual_duration < MIN_VALID_CHUNK_DURATION_SECONDS:
                try:
                    os.remove(out_file)
                except OSError:
                    pass

                # A tiny final chunk is not useful to ASR. A tiny non-final chunk
                # means the chunking command produced an unexpected artifact.
                if start + actual_duration >= total_duration:
                    skipped_tiny_chunks += 1
                    break

                return (
                    False,
                    chunks,
                    f"Generated unexpectedly short chunk {idx}: {chunk_actual_duration:.3f}s",
                )

            chunks.append(out_file)
            idx += 1
            start += step

        message = f"Created {len(chunks)} chunks in {output_dir}"
        if skipped_tiny_chunks:
            message = f"{message}; skipped {skipped_tiny_chunks} tiny trailing chunk(s)"

        return True, chunks, message
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="ignore") if exc.stderr else str(exc)
        return False, chunks, f"ffmpeg failed for chunk {idx}: {stderr}"
    except Exception as exc:
        return False, chunks, f"Unexpected error during chunking: {exc}"