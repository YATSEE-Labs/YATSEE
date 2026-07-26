"""
Align structured record evidence to timestamped transcript segments.

This module productionizes the validated V4 evidence matcher as an optional
records enrichment step. Evidence text remains the primary authority. Stored
segment embeddings shortlist plausible source neighborhoods before lexical and
semantic window scoring, while structural context is limited to tie-breaking
among candidates that already contain meaningful evidence overlap. Ambiguous
or unsupported evidence remains unaligned rather than being forced onto a
nearby procedural phrase.

The matcher consumes final meeting-level records and sliced ``.segments.jsonl``
artifacts. It returns an enriched copy and a diagnostic report without modifying
the caller's source artifact.
"""

from __future__ import annotations

import copy
import difflib
import json
import math
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from yatsee.core.format import format_timestamp
from yatsee.core.hashing import compute_json_sha256, compute_sha256
from yatsee.core.runtime import resolve_torch_device

ALIGNMENT_VERSION = "record_timestamp_v2"
_MODEL_CACHE: dict[tuple[str, str], Any] = {}
OCCURRENCE_SEGMENT_DISTANCE = 1
SAME_SUBJECT_WINDOW_SECONDS = 180.0
SAME_SUBJECT_WEIGHT = 0.18
CONTEXT_BOOST_MIN_LEXICAL_COVERAGE = 0.20
LEXICAL_DOMINANCE_MIN_COVERAGE = 0.85
LEXICAL_DOMINANCE_MIN_SCORE = 0.65
LEXICAL_DOMINANCE_MIN_COVERAGE_GAP = 0.35
GENERIC_FUZZY_MIN_LEXICAL_COVERAGE = 0.85
CANDIDATE_SEGMENTS_PER_QUERY = 12
CANDIDATE_NEIGHBOR_RADIUS = 1

TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*|[A-Za-z]+(?:['’][A-Za-z]+)?")
ELLIPSIS_RE = re.compile(r"(?:\.{3,}|…+)")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by",
    "did", "do", "does", "for", "from", "had", "has", "have", "he",
    "her", "here", "him", "his", "i", "if", "in", "is", "it", "its",
    "me", "my", "no", "not", "of", "on", "or", "our", "she", "so",
    "that", "the", "their", "them", "there", "they", "this", "to",
    "was", "we", "were", "what", "when", "which", "who", "with",
    "you", "your",
}


@dataclass(frozen=True)
class Segment:
    """
    Store one timestamped raw transcript segment.

    :param position: Zero-based position in the loaded JSONL
    :param index: Segment index from the artifact
    :param start_time: Segment start time in seconds
    :param video_id: Case-preserved video/source identifier
    :param text_raw: Raw VTT-derived segment text
    :param tokens: Canonical lexical tokens
    :param embedding: Optional precomputed normalized segment embedding
    """

    position: int
    index: int
    start_time: float
    video_id: str | None
    text_raw: str
    tokens: tuple[str, ...]
    embedding: tuple[float, ...] | None


@dataclass(frozen=True)
class Window:
    """
    Store one adjacent-segment candidate window.

    :param index: Zero-based window index
    :param start_position: First source segment position
    :param end_position: Exclusive final source segment position
    :param text: Concatenated raw text
    :param tokens: Canonical lexical tokens
    :param token_segment_positions: Source segment position for every token
    """

    index: int
    start_position: int
    end_position: int
    text: str
    tokens: tuple[str, ...]
    token_segment_positions: tuple[int, ...]


@dataclass(frozen=True)
class LexicalMatch:
    """
    Store lexical alignment details for one query and candidate window.

    :param score: Combined lexical score from zero to one
    :param coverage: Fraction of evidence tokens represented in matching blocks
    :param longest_ratio: Longest contiguous block divided by evidence length
    :param ratio: SequenceMatcher ratio
    :param exact: Whether the full evidence token sequence matched contiguously
    :param local_start: Candidate start token inside the window
    """

    score: float
    coverage: float
    longest_ratio: float
    ratio: float
    exact: bool
    local_start: int | None


def canonical_token(value: str) -> str:
    """
    Canonicalize one token without recreating YATSEE normalization.

    Numeric comma formatting is removed so ``809,000`` and ``809000`` compare
    consistently. Words retain apostrophes because names and contractions may
    use them meaningfully.

    :param value: Token text
    :return: Canonical token
    """
    token = unicodedata.normalize("NFKC", value).casefold()
    token = token.replace("’", "'").replace("‘", "'")
    if re.fullmatch(r"\d+(?:[.,]\d+)*", token):
        token = token.replace(",", "")
    return token


def tokenize(text: str) -> list[str]:
    """
    Convert text into canonical alignment tokens.

    :param text: Input text
    :return: Canonical tokens
    """
    return [canonical_token(match.group(0)) for match in TOKEN_RE.finditer(text)]


def validate_video_id(value: Any, source: Any = None) -> str | None:
    """
    Validate and preserve a case-sensitive source video ID.

    Current sliced artifacts may contain deterministic placeholder IDs when no
    canonical source video exists. When the JSONL includes its source basename,
    only an ID matching that basename prefix is retained for link generation.
    Older artifacts without ``source`` keep the original 11-character behavior.

    :param value: Candidate JSON value
    :param source: Optional source artifact basename
    :return: Valid case-preserved video ID or ``None``
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not VIDEO_ID_RE.fullmatch(candidate):
        return None

    if isinstance(source, str) and source.strip():
        source_prefix = source.strip()[:11]
        if not VIDEO_ID_RE.fullmatch(source_prefix):
            return None
        if source_prefix.casefold() != candidate.casefold():
            return None
    return candidate


def parse_segment_embedding(value: Any, *, line_number: int) -> tuple[float, ...] | None:
    """
    Validate one optional precomputed segment embedding.

    Missing embeddings remain valid for lexical-only alignment. A present but
    malformed vector is rejected because silently mixing corrupt vectors with
    valid segment text would make semantic candidate selection unreliable.

    :param value: JSON value from the segment ``embedding`` field
    :param line_number: Source JSONL line number for diagnostics
    :return: Numeric embedding tuple or ``None`` when the field is absent
    :raises ValueError: If a present embedding is empty or non-numeric
    """
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise ValueError(
            f"Segment row {line_number} has an invalid embedding"
        )

    embedding: list[float] = []
    for component in value:
        if (
            isinstance(component, bool)
            or not isinstance(component, (int, float))
            or not math.isfinite(float(component))
        ):
            raise ValueError(
                f"Segment row {line_number} has a non-numeric embedding"
            )
        embedding.append(float(component))
    return tuple(embedding)


def load_segments(path: Path) -> list[Segment]:
    """
    Load timestamped sliced JSONL in chronological order.

    :param path: Sliced JSONL artifact
    :return: Validated segments
    :raises ValueError: If rows are malformed or timestamps move backward
    """
    segments: list[Segment] = []
    previous_start = -math.inf

    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSONL at {path}:{line_number}: {exc.msg}"
                ) from exc
            if not isinstance(row, dict):
                raise ValueError(f"Segment row {line_number} must be an object")

            text_raw = row.get("text_raw")
            start_time = row.get("start_time")
            if not isinstance(text_raw, str) or not text_raw.strip():
                raise ValueError(f"Segment row {line_number} lacks text_raw")
            if not isinstance(start_time, (int, float)):
                raise ValueError(f"Segment row {line_number} lacks start_time")
            start = float(start_time)
            if start < previous_start:
                raise ValueError(
                    f"Segment timestamps move backward at row {line_number}"
                )
            previous_start = start

            segment_index = row.get("segment_index", len(segments))
            if not isinstance(segment_index, int):
                raise ValueError(f"Segment row {line_number} has invalid segment_index")

            tokens = tuple(tokenize(text_raw))
            if not tokens:
                continue
            segments.append(
                Segment(
                    position=len(segments),
                    index=segment_index,
                    start_time=start,
                    video_id=validate_video_id(row.get("video_id"), row.get("source")),
                    text_raw=text_raw,
                    tokens=tokens,
                    embedding=parse_segment_embedding(
                        row.get("embedding"),
                        line_number=line_number,
                    ),
                )
            )

    if not segments:
        raise ValueError(f"No usable timestamped segments found in {path}")
    return segments


def load_records(path: Path) -> dict[str, Any]:
    """
    Load and minimally validate a final records artifact.

    :param path: Final records JSON artifact
    :return: Parsed artifact
    :raises ValueError: If the expected records shape is missing
    """
    with path.open("r", encoding="utf-8") as handle:
        artifact = json.load(handle)
    if not isinstance(artifact, dict) or not isinstance(artifact.get("records"), list):
        raise ValueError("Records artifact must contain a records list")

    for record_index, record in enumerate(artifact["records"], start=1):
        if not isinstance(record, dict):
            raise ValueError(f"Record {record_index} must be an object")
        evidence = record.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError(f"Record {record_index} lacks evidence")
    return artifact


def build_windows(segments: Sequence[Segment], max_window_segments: int) -> list[Window]:
    """
    Build adjacent raw-segment candidate windows.

    Evidence frequently crosses one sliced JSONL boundary. Evaluating windows
    of one through several segments allows the query to match while still
    retaining token-level segment provenance for the start timestamp.

    :param segments: Chronological source segments
    :param max_window_segments: Maximum adjacent segments per window
    :return: Candidate windows
    :raises ValueError: If the requested size is invalid
    """
    if max_window_segments < 1:
        raise ValueError("max_window_segments must be at least 1")

    windows: list[Window] = []
    for start in range(len(segments)):
        text_parts: list[str] = []
        tokens: list[str] = []
        token_segments: list[int] = []
        for end in range(start, min(len(segments), start + max_window_segments)):
            segment = segments[end]
            text_parts.append(segment.text_raw)
            tokens.extend(segment.tokens)
            token_segments.extend([segment.position] * len(segment.tokens))
            windows.append(
                Window(
                    index=len(windows),
                    start_position=start,
                    end_position=end + 1,
                    text=" ".join(text_parts),
                    tokens=tuple(tokens),
                    token_segment_positions=tuple(token_segments),
                )
            )
    return windows


def build_window_index(
    windows: Sequence[Window],
    segment_count: int,
) -> list[tuple[int, ...]]:
    """
    Index candidate windows by every source segment they contain.

    Semantic preselection identifies promising source segments. This reverse
    index expands those anchors into the same adjacent windows used by the V4
    lexical and ambiguity rules without rescanning every transcript window for
    every record.

    :param windows: All adjacent source windows
    :param segment_count: Number of source segments
    :return: Window indexes grouped by source segment position
    """
    grouped: list[list[int]] = [[] for _ in range(segment_count)]
    for window in windows:
        for position in range(window.start_position, window.end_position):
            grouped[position].append(window.index)
    return [tuple(indexes) for indexes in grouped]


def build_token_source(
    segments: Sequence[Segment],
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """
    Flatten source tokens while retaining segment provenance.

    Exact evidence occurrences are inexpensive to discover in this flattened
    stream. Adding every exact occurrence to the semantic shortlist preserves
    ambiguity checks for repeated procedural phrases instead of allowing a
    top-K vector cutoff to hide a competing exact match.

    :param segments: Chronological source segments
    :return: Flattened tokens and matching segment positions
    """
    tokens: list[str] = []
    positions: list[int] = []
    for segment in segments:
        tokens.extend(segment.tokens)
        positions.extend([segment.position] * len(segment.tokens))
    return tuple(tokens), tuple(positions)


def find_all(haystack: Sequence[str], needle: Sequence[str]) -> list[int]:
    """
    Find every exact token-subsequence occurrence.

    :param haystack: Candidate tokens
    :param needle: Query tokens
    :return: Ordered start positions
    """
    if not needle or len(needle) > len(haystack):
        return []
    first = needle[0]
    matches: list[int] = []
    for start in range(len(haystack) - len(needle) + 1):
        if haystack[start] != first:
            continue
        if list(haystack[start : start + len(needle)]) == list(needle):
            matches.append(start)
    return matches


def exact_evidence_segment_positions(
    *,
    evidence_tokens: Sequence[str],
    source_tokens: Sequence[str],
    token_segment_positions: Sequence[int],
) -> set[int]:
    """
    Locate source segments containing the start of exact evidence occurrences.

    :param evidence_tokens: Canonical record evidence tokens
    :param source_tokens: Flattened source transcript tokens
    :param token_segment_positions: Source segment position per flattened token
    :return: Segment positions where exact evidence begins
    """
    return {
        token_segment_positions[start]
        for start in find_all(source_tokens, evidence_tokens)
        if start < len(token_segment_positions)
    }


def build_segment_embedding_matrix(segments: Sequence[Segment]) -> Any:
    """
    Build a normalized NumPy matrix from stored sliced-segment embeddings.

    Semantic timestamp alignment intentionally reuses the vectors generated by
    the slice stage. Re-embedding the entire transcript and every adjacent
    window duplicates work and turns lexical validation into a corpus-scale
    Cartesian scan.

    :param segments: Chronological source segments
    :return: Row-normalized NumPy embedding matrix
    :raises ValueError: If embeddings are missing, inconsistent, or zero-length
    """
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "numpy is required for embedding-backed timestamp alignment"
        ) from exc

    missing = [segment.index for segment in segments if segment.embedding is None]
    if missing:
        preview = ", ".join(str(index) for index in missing[:5])
        suffix = "..." if len(missing) > 5 else ""
        raise ValueError(
            "Timestamp alignment requires stored embeddings in every segment "
            f"row; missing segment indexes: {preview}{suffix}. Regenerate the "
            "sliced JSONL with --gen-embed."
        )

    dimensions = {len(segment.embedding or ()) for segment in segments}
    if len(dimensions) != 1 or not dimensions or next(iter(dimensions)) < 1:
        raise ValueError("Segment embeddings have inconsistent dimensions")

    matrix = np.asarray(
        [segment.embedding for segment in segments],
        dtype=np.float32,
    )
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if bool(np.any(norms == 0.0)):
        raise ValueError("Segment embeddings contain a zero-length vector")
    return matrix / norms


def build_window_embedding_matrix(
    windows: Sequence[Window],
    segment_embeddings: Any,
) -> Any:
    """
    Derive normalized adjacent-window vectors from stored segment vectors.

    Averaging the normalized vectors for the one-to-three source segments in a
    window provides a lightweight contextual representation without sending
    transcript text back through the embedding model. Lexical evidence remains
    the acceptance authority after semantic candidate selection.

    :param windows: Adjacent source windows
    :param segment_embeddings: Normalized NumPy segment embedding matrix
    :return: Normalized NumPy window embedding matrix
    :raises ValueError: If a derived window vector has zero length
    """
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "numpy is required for embedding-backed timestamp alignment"
        ) from exc

    vectors = np.asarray(
        [
            segment_embeddings[
                window.start_position : window.end_position
            ].mean(axis=0)
            for window in windows
        ],
        dtype=np.float32,
    )
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if bool(np.any(norms == 0.0)):
        raise ValueError("Derived window embeddings contain a zero-length vector")
    return vectors / norms


def top_segment_positions(scores: Any, limit: int) -> list[int]:
    """
    Return deterministic top segment positions from one similarity vector.

    :param scores: One-dimensional NumPy similarity vector
    :param limit: Maximum positions to retain
    :return: Segment positions ordered from highest to lowest similarity
    :raises ValueError: If the score shape or limit is invalid
    """
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "numpy is required for embedding-backed timestamp alignment"
        ) from exc

    values = np.asarray(scores)
    if values.ndim != 1:
        raise ValueError("Segment similarity scores must be one-dimensional")
    if limit < 1:
        raise ValueError("candidate segment limit must be at least 1")
    order = np.argsort(-values, kind="stable")
    return [int(position) for position in order[: min(limit, len(order))]]


def candidate_window_indexes(
    *,
    evidence_scores: Any,
    context_scores: Any,
    exact_positions: set[int],
    windows_by_segment: Sequence[Sequence[int]],
    segment_limit: int,
    neighbor_radius: int,
) -> tuple[int, ...]:
    """
    Expand semantic and exact segment anchors into candidate window indexes.

    Evidence and context each contribute top segment anchors. Exact lexical
    occurrences are always included so repeated generic phrases still compete
    in the ambiguity margin. A small neighboring radius protects boundary
    matches when the strongest semantic segment is adjacent to the evidence
    start.

    :param evidence_scores: Evidence-to-segment similarity vector
    :param context_scores: Context-to-segment similarity vector
    :param exact_positions: Exact evidence start segment positions
    :param windows_by_segment: Reverse index of windows by segment position
    :param segment_limit: Top evidence and context segments retained separately
    :param neighbor_radius: Adjacent segment expansion around each anchor
    :return: Sorted candidate window indexes
    :raises ValueError: If neighbor radius is negative
    """
    if neighbor_radius < 0:
        raise ValueError("candidate neighbor radius cannot be negative")

    anchors = set(exact_positions)
    anchors.update(top_segment_positions(evidence_scores, segment_limit))
    anchors.update(top_segment_positions(context_scores, segment_limit))

    expanded: set[int] = set()
    segment_count = len(windows_by_segment)
    for anchor in anchors:
        start = max(0, anchor - neighbor_radius)
        end = min(segment_count, anchor + neighbor_radius + 1)
        expanded.update(range(start, end))

    indexes: set[int] = set()
    for position in expanded:
        indexes.update(windows_by_segment[position])
    return tuple(sorted(indexes))


def lexical_match(query_tokens: Sequence[str], candidate_tokens: Sequence[str]) -> LexicalMatch:
    """
    Score and locate normalized evidence against raw candidate tokens.

    :param query_tokens: Canonical evidence tokens
    :param candidate_tokens: Canonical raw window tokens
    :return: Lexical score and local alignment position
    """
    if not query_tokens or not candidate_tokens:
        return LexicalMatch(0.0, 0.0, 0.0, 0.0, False, None)

    exact_starts = find_all(candidate_tokens, query_tokens)
    if exact_starts:
        return LexicalMatch(1.0, 1.0, 1.0, 1.0, True, exact_starts[0])

    matcher = difflib.SequenceMatcher(
        None,
        list(query_tokens),
        list(candidate_tokens),
        autojunk=False,
    )
    blocks = [block for block in matcher.get_matching_blocks() if block.size]
    if not blocks:
        return LexicalMatch(0.0, 0.0, 0.0, matcher.ratio(), False, None)

    matched_tokens = sum(block.size for block in blocks)
    longest = max(block.size for block in blocks)
    coverage = min(1.0, matched_tokens / len(query_tokens))
    longest_ratio = min(1.0, longest / len(query_tokens))
    ratio = matcher.ratio()
    score = min(1.0, 0.55 * coverage + 0.25 * longest_ratio + 0.20 * ratio)

    # Prefer the first meaningful block in evidence order. This identifies the
    # start segment better than the earliest common token anywhere in a window.
    ordered = sorted(blocks, key=lambda block: (block.a, -block.size, block.b))
    meaningful = next(
        (block for block in ordered if block.size >= min(3, len(query_tokens))),
        max(blocks, key=lambda block: block.size),
    )
    return LexicalMatch(
        score=score,
        coverage=coverage,
        longest_ratio=longest_ratio,
        ratio=ratio,
        exact=False,
        local_start=meaningful.b,
    )


def content_overlap(query_tokens: Sequence[str], candidate_tokens: Sequence[str]) -> float:
    """
    Measure non-stopword query coverage in a candidate window.

    This is used as a lightweight contextual tie-breaker for generic evidence
    such as ``All in favor`` where the subject often identifies the correct
    agenda neighborhood.

    :param query_tokens: Context tokens from subject and summary
    :param candidate_tokens: Raw candidate tokens
    :return: Coverage score from zero to one
    """
    content = [token for token in query_tokens if token not in STOPWORDS]
    if not content:
        return 0.0
    candidate_set = set(candidate_tokens)
    return sum(1 for token in content if token in candidate_set) / len(content)


def is_generic_evidence(tokens: Sequence[str]) -> bool:
    """
    Identify short or low-information procedural evidence.

    :param tokens: Canonical evidence tokens
    :return: Whether contextual tie-breaking should receive additional weight
    """
    content = [token for token in tokens if token not in STOPWORDS]
    return len(tokens) <= 12 or len(content) <= 4


def position_prior(
    record: dict[str, Any],
    max_source_chunk: int,
    window: Window,
    segment_count: int,
) -> float:
    """
    Compute a weak source-chunk position prior.

    ``source_chunks`` are derived from chronological extraction chunks. Their
    position is not exact because chunks overlap, so this score is deliberately
    low weight and is never used as a hard filter.

    :param record: Final record object
    :param max_source_chunk: Largest source chunk ID in the artifact
    :param window: Candidate raw window
    :param segment_count: Total source segments
    :return: Prior score from zero to one
    """
    chunks = record.get("source_chunks")
    if (
        not isinstance(chunks, list)
        or not chunks
        or max_source_chunk < 1
        or segment_count < 1
    ):
        return 0.5
    valid = [value for value in chunks if isinstance(value, int) and value >= 1]
    if not valid:
        return 0.5
    expected = (sum(valid) / len(valid) - 0.5) / max_source_chunk
    midpoint = ((window.start_position + window.end_position) / 2) / segment_count
    return max(0.0, 1.0 - abs(expected - midpoint) * 2.0)


def normalize_similarity(value: float) -> float:
    """
    Clamp cosine or lexical similarity into a zero-to-one range.

    :param value: Similarity value
    :return: Clamped score
    """
    return max(0.0, min(1.0, float(value)))


def score_candidate(
    *,
    backend: str,
    generic: bool,
    lexical: LexicalMatch,
    evidence_semantic: float,
    context_semantic: float,
    context_lexical: float,
    position: float,
    window_size: int,
) -> float:
    """
    Combine evidence, context, and weak positional signals.

    Evidence remains the primary query. Subject and summary context receive
    additional weight only for generic evidence where direct text cannot
    distinguish repeated procedural phrases.

    :param backend: ``lexical``, ``embedding``, or ``hybrid``
    :param generic: Whether the evidence is low-information
    :param lexical: Lexical alignment details
    :param evidence_semantic: Evidence-to-window cosine similarity
    :param context_semantic: Subject/summary-to-window cosine similarity
    :param context_lexical: Subject/summary token coverage
    :param position: Weak chronological source-chunk prior
    :param window_size: Number of source segments in the candidate
    :return: Combined score from zero to one
    """
    if backend == "lexical":
        if generic:
            score = 0.62 * lexical.score + 0.28 * context_lexical + 0.10 * position
        else:
            score = 0.86 * lexical.score + 0.09 * context_lexical + 0.05 * position
    elif backend == "embedding":
        if generic:
            score = 0.60 * evidence_semantic + 0.30 * context_semantic + 0.10 * position
        else:
            score = 0.82 * evidence_semantic + 0.13 * context_semantic + 0.05 * position
    else:
        if generic:
            score = (
                0.30 * lexical.score
                + 0.35 * evidence_semantic
                + 0.25 * context_semantic
                + 0.10 * position
            )
        else:
            score = (
                0.40 * lexical.score
                + 0.45 * evidence_semantic
                + 0.10 * context_semantic
                + 0.05 * position
            )

    # Larger windows are useful for context but should not win merely because
    # they contain more unrelated material.
    size_penalty = max(0.90, 1.0 - 0.025 * max(0, window_size - 1))
    return normalize_similarity(score * size_penalty)


def resolve_device(requested: str) -> str:
    """
    Resolve a SentenceTransformer device through the shared runtime helper.

    :param requested: ``auto``, ``cpu``, ``cuda``, or ``mps``
    :return: Concrete device name
    :raises ValueError: If the requested value is unsupported
    """
    if requested not in {"auto", "cpu", "cuda", "mps"}:
        raise ValueError(f"Unsupported timestamp alignment device: {requested}")
    return resolve_torch_device(requested)


def encode_texts(model_name: str, device: str, texts: Sequence[str]) -> Any:
    """
    Encode text with a lazily loaded, process-reused SentenceTransformer.

    The cache keeps one model instance per model/device pair so an entity batch
    does not reload embedding weights for every meeting. Heavy optional
    dependencies remain absent from normal records runs unless timestamp
    alignment is explicitly enabled.

    :param model_name: SentenceTransformer model name or local path
    :param device: Concrete model device
    :param texts: Text values to encode
    :return: NumPy embedding matrix
    :raises RuntimeError: If embedding dependencies are unavailable
    """
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "sentence-transformers is required for timestamp alignment. "
            "Install YATSEE with the intelligence optional dependency set."
        ) from exc

    cache_key = (model_name, device)
    model = _MODEL_CACHE.get(cache_key)
    if model is None:
        model = SentenceTransformer(model_name, device=device)
        _MODEL_CACHE[cache_key] = model

    return model.encode(
        list(texts),
        normalize_embeddings=True,
        show_progress_bar=sys.stderr.isatty(),
        convert_to_numpy=True,
    )


def build_context_text(record: dict[str, Any]) -> str:
    """
    Build a secondary disambiguation query from record metadata.

    :param record: Final record object
    :return: Subject and summary context
    """
    values = [record.get("subject"), record.get("summary"), record.get("record_type")]
    return ". ".join(value.strip() for value in values if isinstance(value, str) and value.strip())


def choose_start_segment(
    window: Window,
    lexical: LexicalMatch,
    evidence_scores: Sequence[float] | None,
) -> int:
    """
    Resolve the source segment containing the beginning of evidence.

    Lexical token provenance is preferred. When normalization eliminated enough
    raw words to prevent a useful token start, the earliest near-best individual
    segment embedding inside the selected window is used.

    :param window: Selected candidate window
    :param lexical: Evidence lexical alignment
    :param evidence_scores: Per-segment evidence cosine scores or ``None``
    :return: Source segment position
    """
    if lexical.local_start is not None and window.token_segment_positions:
        local_start = min(lexical.local_start, len(window.token_segment_positions) - 1)
        return window.token_segment_positions[local_start]

    if evidence_scores is not None:
        positions = list(range(window.start_position, window.end_position))
        best = max(evidence_scores[position] for position in positions)
        for position in positions:
            if evidence_scores[position] >= best - 0.03:
                return position
    return window.start_position


def windows_overlap(left: Window, right: Window) -> bool:
    """
    Return whether two half-open segment windows overlap.

    :param left: First candidate window
    :param right: Second candidate window
    :return: Whether the windows share at least one source segment
    """
    return max(left.start_position, right.start_position) < min(
        left.end_position,
        right.end_position,
    )


def same_local_occurrence(
    candidate: dict[str, Any],
    representative: dict[str, Any],
) -> bool:
    """
    Identify duplicate candidate windows for one local evidence occurrence.

    Window sizes of one through three segments often produce multiple strong
    candidates for the same excerpt. They should not compete with each other in
    the ambiguity margin. The representative is intentionally used as a fixed
    anchor so overlapping windows cannot chain across the entire transcript.

    :param candidate: Candidate being classified
    :param representative: Current occurrence representative
    :return: Whether both candidates describe one local occurrence
    """
    if abs(
        candidate["segment_position"] - representative["segment_position"]
    ) > OCCURRENCE_SEGMENT_DISTANCE:
        return False
    return windows_overlap(candidate["window"], representative["window"])


def collapse_occurrence_candidates(
    candidates: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Collapse overlapping candidate windows into distinct local occurrences.

    The highest-scoring candidate becomes the occurrence representative. The
    additional members are retained as diagnostics on that representative.

    :param candidates: Candidate windows for one record
    :return: Distinct occurrence representatives ordered by score
    """
    clusters: list[dict[str, Any]] = []
    for candidate in sorted(
        candidates,
        key=lambda value: value["score"],
        reverse=True,
    ):
        target = next(
            (
                cluster
                for cluster in clusters
                if same_local_occurrence(candidate, cluster["representative"])
            ),
            None,
        )
        if target is None:
            clusters.append(
                {
                    "representative": candidate,
                    "members": [candidate],
                }
            )
        else:
            target["members"].append(candidate)

    representatives: list[dict[str, Any]] = []
    for cluster in clusters:
        representative = dict(cluster["representative"])
        members = cluster["members"]
        representative["occurrence_member_count"] = len(members)
        representative["occurrence_segment_positions"] = sorted(
            {member["segment_position"] for member in members}
        )
        representative["occurrence_window_start"] = min(
            member["window"].start_position for member in members
        )
        representative["occurrence_window_end"] = max(
            member["window"].end_position for member in members
        )
        representatives.append(representative)
    return sorted(
        representatives,
        key=lambda value: value["score"],
        reverse=True,
    )


def normalize_subject(record: dict[str, Any]) -> str:
    """
    Build a conservative same-subject grouping key.

    :param record: Final record object
    :return: Canonical subject key or an empty string
    """
    subject = record.get("subject")
    if not isinstance(subject, str):
        return ""
    return " ".join(tokenize(subject))


def has_context_boost_support(candidate: dict[str, Any]) -> bool:
    """
    Require meaningful evidence overlap before applying structural context.

    Semantic and chronological context may disambiguate plausible evidence
    candidates, but they must not rescue a source window that contains almost
    none of the record evidence.

    :param candidate: Internal candidate score dictionary
    :return: Whether same-subject adjacency may affect the candidate
    """
    lexical = candidate["lexical"]
    return bool(
        lexical.exact
        or lexical.coverage >= CONTEXT_BOOST_MIN_LEXICAL_COVERAGE
    )


def is_lexically_dominant(
    best: dict[str, Any] | None,
    second: dict[str, Any] | None,
) -> bool:
    """
    Identify a clearly superior evidence match hidden by blended-score ties.

    This rule is intentionally comparative. A candidate must cover nearly all
    evidence tokens, have a strong lexical score, and exceed the next distinct
    occurrence by a large coverage gap. Repeated generic phrases therefore do
    not qualify because competing occurrences also have full coverage.

    :param best: Highest-ranked distinct occurrence
    :param second: Second-highest distinct occurrence
    :return: Whether the best candidate is lexically decisive
    """
    if best is None or second is None:
        return False
    best_lexical = best["lexical"]
    second_lexical = second["lexical"]
    return bool(
        best_lexical.coverage >= LEXICAL_DOMINANCE_MIN_COVERAGE
        and best_lexical.score >= LEXICAL_DOMINANCE_MIN_SCORE
        and (
            best_lexical.coverage - second_lexical.coverage
            >= LEXICAL_DOMINANCE_MIN_COVERAGE_GAP
        )
    )



def has_generic_fuzzy_support(
    *,
    generic: bool,
    candidate: dict[str, Any] | None,
) -> bool:
    """
    Require strong evidence coverage for non-exact generic matches.

    Short procedural evidence is common across civic meetings. A fuzzy match
    for that evidence is accepted only when the candidate contains nearly all
    evidence tokens. Exact matches remain eligible for contextual
    disambiguation, while weak partial matches are left unresolved rather than
    linked to a semantically similar but unrelated occurrence.

    :param generic: Whether the record evidence is low-information
    :param candidate: Highest-ranked distinct occurrence
    :return: Whether the candidate has enough direct evidence support
    """
    if candidate is None or not generic:
        return candidate is not None
    lexical = candidate["lexical"]
    return bool(
        lexical.exact
        or lexical.coverage >= GENERIC_FUZZY_MIN_LEXICAL_COVERAGE
    )

def same_subject_adjacency_score(
    *,
    record: dict[str, Any],
    candidate_timestamp: float,
    anchors: Sequence[dict[str, Any]],
) -> float:
    """
    Score temporal proximity to confident records with the same subject.

    Votes are expected at or after related motions/procedural actions. Motions
    are expected at or before a related vote. Other record types use symmetric
    proximity. This is a soft score used only to resolve otherwise ambiguous,
    generic evidence.

    :param record: Ambiguous record being resolved
    :param candidate_timestamp: Candidate source timestamp
    :param anchors: Confident same-subject records
    :return: Adjacency score from zero to one
    """
    record_type = str(record.get("record_type") or "").casefold()
    best = 0.0
    for anchor in anchors:
        anchor_timestamp = float(anchor["timestamp"])
        anchor_type = str(anchor.get("record_type") or "").casefold()
        delta = candidate_timestamp - anchor_timestamp

        directional = False
        if record_type == "vote" and anchor_type in {
            "motion",
            "procedural_action",
            "discussion",
        }:
            directional = True
            if delta < 0.0 or delta > SAME_SUBJECT_WINDOW_SECONDS:
                continue
            score = 1.0 - delta / SAME_SUBJECT_WINDOW_SECONDS
        elif record_type == "motion" and anchor_type == "vote":
            directional = True
            if delta > 0.0 or -delta > SAME_SUBJECT_WINDOW_SECONDS:
                continue
            score = 1.0 - (-delta) / SAME_SUBJECT_WINDOW_SECONDS
        else:
            distance = abs(delta)
            if distance > SAME_SUBJECT_WINDOW_SECONDS:
                continue
            score = 1.0 - distance / SAME_SUBJECT_WINDOW_SECONDS

        # Directional relationships are more informative than symmetric
        # proximity, but both remain soft tie-breakers.
        if directional:
            score = min(1.0, score + 0.05)
        best = max(best, score)
    return best


def shorten(text: str, limit: int = 220) -> str:
    """
    Produce compact one-line review text.

    :param text: Source text
    :param limit: Maximum output length
    :return: Compacted text
    """
    value = " ".join(text.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"



def candidate_to_report(candidate: dict[str, Any], segments: Sequence[Segment]) -> dict[str, Any]:
    """
    Convert an internal candidate into JSON review output.

    :param candidate: Candidate score dictionary
    :param segments: Source segments
    :return: JSON-compatible candidate details
    """
    segment = segments[candidate["segment_position"]]
    window = candidate["window"]
    report = {
        "timestamp": segment.start_time,
        "timestamp_display": format_timestamp(segment.start_time),
        "video_id": segment.video_id,
        "segment_index": segment.index,
        "window_segment_indexes": [
            segments[position].index
            for position in range(window.start_position, window.end_position)
        ],
        "score": round(candidate["score"], 4),
        "lexical_score": round(candidate["lexical"].score, 4),
        "lexical_coverage": round(candidate["lexical"].coverage, 4),
        "evidence_semantic": round(candidate["evidence_semantic"], 4),
        "context_semantic": round(candidate["context_semantic"], 4),
        "context_lexical": round(candidate["context_lexical"], 4),
        "position_prior": round(candidate["position"], 4),
        "exact": candidate["lexical"].exact,
        "raw_excerpt": shorten(segment.text_raw),
        "window_excerpt": shorten(window.text),
    }
    if "base_score" in candidate:
        report["base_score"] = round(candidate["base_score"], 4)
    if "same_subject_adjacency" in candidate:
        report["same_subject_adjacency"] = round(
            candidate["same_subject_adjacency"],
            4,
        )
    if "occurrence_member_count" in candidate:
        report["occurrence_member_count"] = candidate[
            "occurrence_member_count"
        ]
        report["occurrence_segment_indexes"] = [
            segments[position].index
            for position in candidate.get("occurrence_segment_positions", [])
        ]
    return report


def align_records(
    *,
    artifact: dict[str, Any],
    segments: Sequence[Segment],
    windows: Sequence[Window],
    backend: str,
    model_name: str,
    device: str,
    min_score: float,
    min_margin: float,
    top_candidates: int,
    candidate_segment_limit: int,
    candidate_neighbor_radius: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Align all record evidence directly to timestamped source segments.

    Embedding-backed modes reuse stored sliced-segment vectors to shortlist
    source neighborhoods. The validated lexical, contextual, occurrence, and
    ambiguity rules then run only against those candidate windows. Lexical-only
    mode retains the exhaustive standard-library path for testing and explicit
    non-embedding use.

    :param artifact: Final records artifact
    :param segments: Timestamped source segments
    :param windows: Adjacent-segment candidate windows
    :param backend: Alignment backend
    :param model_name: SentenceTransformer model name
    :param device: Model device
    :param min_score: Minimum accepted candidate score
    :param min_margin: Minimum score gap from the next distinct segment
    :param top_candidates: Number of review candidates retained per record
    :param candidate_segment_limit: Top evidence/context segments per record
    :param candidate_neighbor_radius: Adjacent segment expansion per anchor
    :return: Per-record results and runtime metadata
    """
    records = artifact["records"]
    evidence_texts = [record["evidence"] for record in records]
    context_texts = [build_context_text(record) for record in records]
    evidence_tokens_by_record = [tokenize(text) for text in evidence_texts]

    evidence_window_scores = None
    context_window_scores = None
    evidence_segment_scores = None
    concrete_device = "none"
    candidate_indexes_by_record = [
        tuple(window.index for window in windows)
        for _record in records
    ]
    derived_window_embedding_count = 0

    if backend in {"embedding", "hybrid"} and records:
        segment_embeddings = build_segment_embedding_matrix(segments)
        concrete_device = resolve_device(device)
        query_embeddings = encode_texts(
            model_name,
            concrete_device,
            evidence_texts + context_texts,
        )
        record_count = len(records)
        evidence_embeddings = query_embeddings[:record_count]
        context_embeddings = query_embeddings[record_count:]
        if evidence_embeddings.shape[1] != segment_embeddings.shape[1]:
            raise ValueError(
                "Stored segment embedding dimensions do not match the configured "
                "timestamp embedding model. Regenerate the sliced JSONL with "
                "the current embedding model."
            )

        evidence_segment_scores = evidence_embeddings @ segment_embeddings.T
        context_segment_scores = context_embeddings @ segment_embeddings.T
        window_embeddings = build_window_embedding_matrix(
            windows,
            segment_embeddings,
        )
        evidence_window_scores = evidence_embeddings @ window_embeddings.T
        context_window_scores = context_embeddings @ window_embeddings.T
        derived_window_embedding_count = len(windows)
        windows_by_segment = build_window_index(windows, len(segments))
        source_tokens, token_segment_positions = build_token_source(segments)
        candidate_indexes_by_record = []

        for record_position, evidence_tokens in enumerate(
            evidence_tokens_by_record
        ):
            exact_positions = exact_evidence_segment_positions(
                evidence_tokens=evidence_tokens,
                source_tokens=source_tokens,
                token_segment_positions=token_segment_positions,
            )
            candidate_indexes_by_record.append(
                candidate_window_indexes(
                    evidence_scores=evidence_segment_scores[record_position],
                    context_scores=context_segment_scores[record_position],
                    exact_positions=exact_positions,
                    windows_by_segment=windows_by_segment,
                    segment_limit=candidate_segment_limit,
                    neighbor_radius=candidate_neighbor_radius,
                )
            )

    max_source_chunk = max(
        (
            chunk
            for record in records
            for chunk in record.get("source_chunks", [])
            if isinstance(chunk, int)
        ),
        default=0,
    )

    states: list[dict[str, Any]] = []
    for record_index, record in enumerate(records, start=1):
        record_position = record_index - 1
        evidence_tokens = evidence_tokens_by_record[record_position]
        context_tokens = tokenize(context_texts[record_position])
        generic = is_generic_evidence(evidence_tokens)
        candidates: list[dict[str, Any]] = []

        for window_index in candidate_indexes_by_record[record_position]:
            window = windows[window_index]
            lexical = lexical_match(evidence_tokens, window.tokens)
            context_lexical = content_overlap(context_tokens, window.tokens)
            position = position_prior(
                record,
                max_source_chunk,
                window,
                len(segments),
            )
            evidence_semantic = (
                normalize_similarity(
                    evidence_window_scores[record_position, window.index]
                )
                if evidence_window_scores is not None
                else 0.0
            )
            context_semantic = (
                normalize_similarity(
                    context_window_scores[record_position, window.index]
                )
                if context_window_scores is not None
                else 0.0
            )
            score = score_candidate(
                backend=backend,
                generic=generic,
                lexical=lexical,
                evidence_semantic=evidence_semantic,
                context_semantic=context_semantic,
                context_lexical=context_lexical,
                position=position,
                window_size=window.end_position - window.start_position,
            )
            segment_scores = (
                evidence_segment_scores[record_position]
                if evidence_segment_scores is not None
                else None
            )
            segment_position = choose_start_segment(
                window,
                lexical,
                segment_scores,
            )
            candidates.append(
                {
                    "window": window,
                    "segment_position": segment_position,
                    "score": score,
                    "lexical": lexical,
                    "evidence_semantic": evidence_semantic,
                    "context_semantic": context_semantic,
                    "context_lexical": context_lexical,
                    "position": position,
                }
            )

        ranked = collapse_occurrence_candidates(candidates)
        states.append(
            {
                "record_index": record_index,
                "record": record,
                "generic": generic,
                "ranked": ranked,
                "resolved_by_same_subject": False,
                "resolved_by_lexical_dominance": False,
            }
        )

    def classify(state: dict[str, Any]) -> None:
        """
        Classify one ranked alignment state as accepted or ambiguous.

        :param state: Mutable alignment candidate state
        :return: None
        """
        ranked = state["ranked"]
        best = ranked[0] if ranked else None
        second = ranked[1] if len(ranked) > 1 else None
        margin = best["score"] - second["score"] if best and second else 1.0
        lexical_dominance = bool(
            best
            and best["score"] >= min_score
            and margin < min_margin
            and is_lexically_dominant(best, second)
        )
        generic_fuzzy_supported = has_generic_fuzzy_support(
            generic=state["generic"],
            candidate=best,
        )
        accepted = bool(
            best
            and best["score"] >= min_score
            and generic_fuzzy_supported
            and (margin >= min_margin or lexical_dominance)
        )
        state["best"] = best
        state["margin"] = margin
        state["accepted"] = accepted
        state["generic_fuzzy_supported"] = generic_fuzzy_supported
        state["resolved_by_lexical_dominance"] = lexical_dominance
        state["ambiguous"] = bool(
            best
            and best["score"] >= min_score
            and generic_fuzzy_supported
            and not accepted
        )

    for state in states:
        classify(state)

    # Confident same-subject records provide local chronological anchors for a
    # second pass over generic ambiguous evidence. Avoid weak generic anchors so
    # one repeated phrase cannot reinforce another incorrect phrase.
    anchors_by_subject: dict[str, list[dict[str, Any]]] = {}
    for state in states:
        if not state["accepted"]:
            continue
        if state["generic"] and state["margin"] < 0.08:
            continue
        subject_key = normalize_subject(state["record"])
        best = state["best"]
        if not subject_key or best is None:
            continue
        source_segment = segments[best["segment_position"]]
        anchors_by_subject.setdefault(subject_key, []).append(
            {
                "record_index": state["record_index"],
                "record_type": state["record"].get("record_type"),
                "timestamp": source_segment.start_time,
            }
        )

    for state in states:
        if not state["ambiguous"] or not state["generic"]:
            continue
        subject_key = normalize_subject(state["record"])
        anchors = anchors_by_subject.get(subject_key, [])
        if not anchors:
            continue

        rescored: list[dict[str, Any]] = []
        used_adjacency = False
        for candidate in state["ranked"]:
            updated = dict(candidate)
            source_segment = segments[candidate["segment_position"]]

            # Structural context may break ties only among candidates that
            # already contain meaningful evidence. This prevents a nearby but
            # unrelated segment from being promoted by chronology alone.
            adjacency = 0.0
            if has_context_boost_support(candidate):
                adjacency = same_subject_adjacency_score(
                    record=state["record"],
                    candidate_timestamp=source_segment.start_time,
                    anchors=anchors,
                )
            used_adjacency = used_adjacency or adjacency > 0.0
            updated["base_score"] = candidate["score"]
            updated["same_subject_adjacency"] = adjacency
            updated["score"] = normalize_similarity(
                candidate["score"] + SAME_SUBJECT_WEIGHT * adjacency
            )
            rescored.append(updated)

        if not used_adjacency:
            continue
        state["ranked"] = sorted(
            rescored,
            key=lambda candidate: candidate["score"],
            reverse=True,
        )
        state["resolved_by_same_subject"] = True
        classify(state)

    results: list[dict[str, Any]] = []
    for state in states:
        record_index = state["record_index"]
        record = state["record"]
        ranked = state["ranked"]
        best = state["best"]
        margin = state["margin"]
        accepted = state["accepted"]
        ambiguous = state["ambiguous"]

        result: dict[str, Any] = {
            "record_index": record_index,
            "record_type": record.get("record_type"),
            "subject": record.get("subject"),
            "source_chunks": record.get("source_chunks"),
            "evidence": shorten(record["evidence"], 300),
            "generic_evidence": state["generic"],
            "matched": accepted,
            "ambiguous": ambiguous,
            "score_margin": round(margin, 4),
            "resolved_by_same_subject": state["resolved_by_same_subject"],
            "resolved_by_lexical_dominance": state[
                "resolved_by_lexical_dominance"
            ],
            "generic_fuzzy_supported": state["generic_fuzzy_supported"],
            "candidates": [
                candidate_to_report(candidate, segments)
                for candidate in ranked[:top_candidates]
            ],
        }

        if best:
            best_report = candidate_to_report(best, segments)
            if accepted:
                method = (
                    f"{backend}_exact"
                    if best["lexical"].exact
                    else f"{backend}_fuzzy"
                )
                if state["resolved_by_same_subject"]:
                    method += "+same_subject"
                if state["resolved_by_lexical_dominance"]:
                    method += "+lexical_dominance"
                result.update(
                    {
                        "timestamp": best_report["timestamp"],
                        "timestamp_display": best_report["timestamp_display"],
                        "video_id": best_report["video_id"],
                        "segment_index": best_report["segment_index"],
                        "method": method,
                        "score": best_report["score"],
                        "raw_excerpt": best_report["raw_excerpt"],
                    }
                )
            elif ambiguous:
                result["reason"] = "top distinct occurrences are too close"
            elif (
                best["score"] >= min_score
                and not state["generic_fuzzy_supported"]
            ):
                result["reason"] = (
                    "generic fuzzy evidence lacks sufficient lexical coverage"
                )
            else:
                result["reason"] = "top candidate is below minimum score"
        else:
            result["reason"] = "no candidates"
        results.append(result)

    metadata = {
        "backend": backend,
        "embedding_model": model_name if backend in {"embedding", "hybrid"} else None,
        "device": concrete_device,
        "window_count": len(windows),
        "evaluated_window_count": sum(
            len(indexes) for indexes in candidate_indexes_by_record
        ),
        "reused_segment_embedding_count": (
            len(segments) if backend in {"embedding", "hybrid"} else 0
        ),
        "derived_window_embedding_count": derived_window_embedding_count,
        "candidate_strategy": (
            "stored_segment_embedding_top_k"
            if backend in {"embedding", "hybrid"}
            else "exhaustive_lexical"
        ),
        "candidate_segments_per_query": candidate_segment_limit,
        "candidate_neighbor_radius": candidate_neighbor_radius,
        "max_window_segments": max(
            (window.end_position - window.start_position for window in windows),
            default=0,
        ),
        "occurrence_segment_distance": OCCURRENCE_SEGMENT_DISTANCE,
        "same_subject_window_seconds": SAME_SUBJECT_WINDOW_SECONDS,
        "same_subject_weight": SAME_SUBJECT_WEIGHT,
        "context_boost_min_lexical_coverage": (
            CONTEXT_BOOST_MIN_LEXICAL_COVERAGE
        ),
        "lexical_dominance_min_coverage": LEXICAL_DOMINANCE_MIN_COVERAGE,
        "lexical_dominance_min_score": LEXICAL_DOMINANCE_MIN_SCORE,
        "lexical_dominance_min_coverage_gap": (
            LEXICAL_DOMINANCE_MIN_COVERAGE_GAP
        ),
        "generic_fuzzy_min_lexical_coverage": (
            GENERIC_FUZZY_MIN_LEXICAL_COVERAGE
        ),
    }
    return results, metadata


def build_report(
    *,
    segments_label: str,
    records_label: str,
    segments: Sequence[Segment],
    results: Sequence[dict[str, Any]],
    metadata: dict[str, Any],
    min_score: float,
    min_margin: float,
) -> dict[str, Any]:
    """
    Build a review-oriented JSON report.

    :param segments_label: Portable source sliced JSONL label
    :param records_label: Portable source records artifact label
    :param segments: Loaded source segments
    :param results: Per-record alignment results
    :param metadata: Backend metadata
    :param min_score: Acceptance threshold
    :param min_margin: Ambiguity threshold
    :return: JSON-compatible report
    """
    matched = sum(1 for result in results if result["matched"])
    ambiguous = sum(1 for result in results if result["ambiguous"])
    unmatched = len(results) - matched - ambiguous
    return {
        "alignment_version": ALIGNMENT_VERSION,
        "inputs": {
            "segments": segments_label,
            "records": records_label,
        },
        "settings": {
            **metadata,
            "min_score": min_score,
            "min_margin": min_margin,
        },
        "source": {
            "segment_count": len(segments),
            "video_ids_case_preserved": sorted(
                {segment.video_id for segment in segments if segment.video_id}
            ),
        },
        "records": {
            "record_count": len(results),
            "matched_count": matched,
            "ambiguous_count": ambiguous,
            "unmatched_count": unmatched,
            "match_rate": round(matched / len(results), 4) if results else 0.0,
        },
        "record_matches": list(results),
    }


def build_enriched_artifact(
    artifact: dict[str, Any],
    results: Sequence[dict[str, Any]],
    alignment_metadata: dict[str, Any],
) -> dict[str, Any]:
    """
    Build a non-destructive enriched records artifact copy.

    Existing timestamp fields are removed before accepted results are applied so
    rerunning a newer alignment version cannot leave stale links on records that
    are now ambiguous or unsupported.

    :param artifact: Original records artifact
    :param results: Per-record alignment results
    :param alignment_metadata: Artifact-level alignment identity and counts
    :return: Enriched artifact copy
    :raises ValueError: If result and record counts differ
    """
    enriched = copy.deepcopy(artifact)
    records = enriched["records"]
    if len(records) != len(results):
        raise ValueError(
            "Timestamp alignment result count does not match record count"
        )

    for record in records:
        record.pop("timestamp", None)
        record.pop("video_id", None)

    for record, result in zip(records, results):
        if not result["matched"]:
            continue
        record["timestamp"] = result["timestamp"]
        if result.get("video_id"):
            record["video_id"] = result["video_id"]

    enriched["timestamp_alignment"] = alignment_metadata
    return enriched

@dataclass(frozen=True)
class RecordTimestampConfig:
    """
    Configure evidence-to-timestamp alignment.

    Thresholds are versioned implementation settings rather than normal CLI
    tuning knobs. Tests may select lexical mode to avoid optional ML
    dependencies, while production records enrichment uses hybrid mode.

    :param backend: ``lexical``, ``embedding``, or ``hybrid``
    :param model_name: SentenceTransformer model name or local path
    :param device: ``auto``, ``cpu``, ``cuda``, or ``mps``
    :param max_window_segments: Maximum adjacent segments per candidate
    :param min_score: Minimum accepted blended score
    :param min_margin: Minimum lead over the next distinct occurrence
    :param top_candidates: Diagnostic candidates retained per record
    :param candidate_segments_per_query: Top evidence/context segments retained
    :param candidate_neighbor_radius: Adjacent segments added around each anchor
    """

    backend: str = "hybrid"
    model_name: str = "BAAI/bge-small-en-v1.5"
    device: str = "auto"
    max_window_segments: int = 3
    min_score: float = 0.58
    min_margin: float = 0.02
    top_candidates: int = 3
    candidate_segments_per_query: int = CANDIDATE_SEGMENTS_PER_QUERY
    candidate_neighbor_radius: int = CANDIDATE_NEIGHBOR_RADIUS


@dataclass(frozen=True)
class RecordTimestampOutcome:
    """
    Store one completed timestamp enrichment result.

    :param artifact: Enriched records artifact copy
    :param report: Review-oriented alignment diagnostics
    :param metadata: Compact artifact-level alignment metadata
    """

    artifact: dict[str, Any]
    report: dict[str, Any]
    metadata: dict[str, Any]


def _records_alignment_input(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Build the stable record payload used for alignment cache identity.

    Derived timestamp fields are excluded so the fingerprint remains stable
    after enrichment. Other record fields remain included because changes to
    evidence, subject, summary, type, or source chunks may change alignment.

    :param records: Final meeting-level records
    :return: Timestamp-free record payload
    """
    payload: list[dict[str, Any]] = []
    for record in records:
        clean = {
            key: copy.deepcopy(value)
            for key, value in record.items()
            if key not in {"timestamp", "video_id"}
        }
        payload.append(clean)
    return payload


def build_alignment_identity(
    *,
    artifact: dict[str, Any],
    segments_path: Path,
    config: RecordTimestampConfig,
) -> dict[str, str]:
    """
    Build an independent cache identity for timestamp alignment.

    Timestamp tuning must not invalidate expensive LLM extraction. The identity
    therefore combines only records content, segment content, and matcher
    settings.

    :param artifact: Final records artifact
    :param segments_path: Matching sliced JSONL artifact
    :param config: Alignment configuration
    :return: Component hashes and combined fingerprint
    """
    records_sha256 = compute_json_sha256(
        _records_alignment_input(artifact["records"])
    )
    segments_sha256 = compute_sha256(str(segments_path))
    settings_payload = {
        "version": ALIGNMENT_VERSION,
        "backend": config.backend,
        "model_name": (
            config.model_name
            if config.backend in {"embedding", "hybrid"}
            else None
        ),
        "max_window_segments": config.max_window_segments,
        "min_score": config.min_score,
        "min_margin": config.min_margin,
        "candidate_segments_per_query": (
            config.candidate_segments_per_query
        ),
        "candidate_neighbor_radius": config.candidate_neighbor_radius,
        "occurrence_segment_distance": OCCURRENCE_SEGMENT_DISTANCE,
        "same_subject_window_seconds": SAME_SUBJECT_WINDOW_SECONDS,
        "same_subject_weight": SAME_SUBJECT_WEIGHT,
        "context_boost_min_lexical_coverage": (
            CONTEXT_BOOST_MIN_LEXICAL_COVERAGE
        ),
        "lexical_dominance_min_coverage": (
            LEXICAL_DOMINANCE_MIN_COVERAGE
        ),
        "lexical_dominance_min_score": LEXICAL_DOMINANCE_MIN_SCORE,
        "lexical_dominance_min_coverage_gap": (
            LEXICAL_DOMINANCE_MIN_COVERAGE_GAP
        ),
        "generic_fuzzy_min_lexical_coverage": (
            GENERIC_FUZZY_MIN_LEXICAL_COVERAGE
        ),
    }
    settings_sha256 = compute_json_sha256(settings_payload)
    fingerprint = compute_json_sha256(
        {
            "records_sha256": records_sha256,
            "segments_sha256": segments_sha256,
            "settings_sha256": settings_sha256,
        }
    )
    return {
        "alignment_fingerprint": fingerprint,
        "records_sha256": records_sha256,
        "segments_sha256": segments_sha256,
        "settings_sha256": settings_sha256,
    }


def alignment_is_current(
    *,
    artifact: dict[str, Any],
    segments_path: Path,
    config: RecordTimestampConfig,
) -> bool:
    """
    Return whether an artifact already contains current timestamp enrichment.

    :param artifact: Existing complete records artifact
    :param segments_path: Matching sliced JSONL artifact
    :param config: Requested alignment configuration
    :return: Whether the stored fingerprint matches current inputs
    """
    metadata = artifact.get("timestamp_alignment")
    if not isinstance(metadata, dict):
        return False
    if metadata.get("version") != ALIGNMENT_VERSION:
        return False
    identity = build_alignment_identity(
        artifact=artifact,
        segments_path=segments_path,
        config=config,
    )
    return metadata.get("alignment_fingerprint") == identity[
        "alignment_fingerprint"
    ]


def align_records_artifact(
    *,
    artifact: dict[str, Any],
    segments_path: Path,
    config: RecordTimestampConfig,
    segments_label: str | None = None,
    records_label: str = "in-memory records artifact",
) -> RecordTimestampOutcome:
    """
    Align a complete records artifact to one timestamped segment artifact.

    :param artifact: Complete meeting-level records artifact
    :param segments_path: Matching sliced JSONL artifact
    :param config: Alignment configuration
    :param segments_label: Portable segment source for diagnostics
    :param records_label: Portable records source for diagnostics
    :return: Enriched artifact, diagnostic report, and compact metadata
    :raises ValueError: If artifacts or configuration are invalid
    :raises RuntimeError: If requested embedding dependencies are unavailable
    """
    records = artifact.get("records")
    if not isinstance(records, list):
        raise ValueError("Records artifact must contain a records list")
    if config.backend not in {"lexical", "embedding", "hybrid"}:
        raise ValueError(f"Unsupported timestamp backend: {config.backend}")
    if config.max_window_segments < 1:
        raise ValueError("max_window_segments must be at least 1")
    if not 0.0 <= config.min_score <= 1.0:
        raise ValueError("min_score must be between 0 and 1")
    if not 0.0 <= config.min_margin <= 1.0:
        raise ValueError("min_margin must be between 0 and 1")
    if config.top_candidates < 1:
        raise ValueError("top_candidates must be at least 1")
    if config.candidate_segments_per_query < 1:
        raise ValueError("candidate_segments_per_query must be at least 1")
    if config.candidate_neighbor_radius < 0:
        raise ValueError("candidate_neighbor_radius cannot be negative")

    segments = load_segments(segments_path)
    windows = build_windows(segments, config.max_window_segments)
    results, runtime_metadata = align_records(
        artifact=artifact,
        segments=segments,
        windows=windows,
        backend=config.backend,
        model_name=config.model_name,
        device=config.device,
        min_score=config.min_score,
        min_margin=config.min_margin,
        top_candidates=config.top_candidates,
        candidate_segment_limit=config.candidate_segments_per_query,
        candidate_neighbor_radius=config.candidate_neighbor_radius,
    )
    report = build_report(
        segments_label=segments_label or segments_path.name,
        records_label=records_label,
        segments=segments,
        results=results,
        metadata=runtime_metadata,
        min_score=config.min_score,
        min_margin=config.min_margin,
    )
    identity = build_alignment_identity(
        artifact=artifact,
        segments_path=segments_path,
        config=config,
    )
    counts = report["records"]
    metadata = {
        "version": ALIGNMENT_VERSION,
        **identity,
        "backend": config.backend,
        "embedding_model": (
            config.model_name
            if config.backend in {"embedding", "hybrid"}
            else None
        ),
        "device": runtime_metadata["device"],
        "candidate_strategy": runtime_metadata["candidate_strategy"],
        "evaluated_window_count": runtime_metadata[
            "evaluated_window_count"
        ],
        "reused_segment_embedding_count": runtime_metadata[
            "reused_segment_embedding_count"
        ],
        "derived_window_embedding_count": runtime_metadata[
            "derived_window_embedding_count"
        ],
        "segment_count": len(segments),
        "record_count": counts["record_count"],
        "matched_record_count": counts["matched_count"],
        "ambiguous_record_count": counts["ambiguous_count"],
        "unmatched_record_count": counts["unmatched_count"],
        "match_rate": counts["match_rate"],
    }
    enriched = build_enriched_artifact(artifact, results, metadata)
    return RecordTimestampOutcome(
        artifact=enriched,
        report=report,
        metadata=metadata,
    )
