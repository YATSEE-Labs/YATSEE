"""
Validate deterministic evidence-to-timestamp alignment behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import yatsee.intel.timestamp as timestamp_module
from yatsee.intel.timestamp import (
    RecordTimestampConfig,
    align_records_artifact,
    alignment_is_current,
)


def _write_segments(path: Path, rows: list[tuple[int, float, str]]) -> None:
    """
    Write a minimal timestamped segment fixture.

    :param path: JSONL destination
    :param rows: Segment index, timestamp, and text tuples
    :return: None
    """
    with path.open("w", encoding="utf-8") as handle:
        for index, start_time, text in rows:
            handle.write(
                json.dumps(
                    {
                        "segment_index": index,
                        "start_time": start_time,
                        "end_time": start_time + 9.0,
                        "source": "AbC_dEf-123.example_meeting",
                        "video_id": "AbC_dEf-123",
                        "text_raw": text,
                    }
                )
                + "\n"
            )


def _write_embedded_segments(
    path: Path,
    rows: list[tuple[int, float, str, list[float]]],
) -> None:
    """
    Write timestamped segment fixtures with stored embeddings.

    :param path: JSONL destination
    :param rows: Segment index, timestamp, text, and embedding tuples
    :return: None
    """
    with path.open("w", encoding="utf-8") as handle:
        for index, start_time, text, embedding in rows:
            handle.write(
                json.dumps(
                    {
                        "segment_index": index,
                        "start_time": start_time,
                        "end_time": start_time + 9.0,
                        "source": "AbC_dEf-123.example_meeting",
                        "video_id": "AbC_dEf-123",
                        "text_raw": text,
                        "embedding": embedding,
                    }
                )
                + "\n"
            )


def _artifact(records: list[dict[str, object]]) -> dict[str, object]:
    """
    Build a minimal final records artifact.

    :param records: Record payloads
    :return: Records artifact
    """
    return {
        "artifact_kind": "records",
        "status": "complete",
        "record_count": len(records),
        "records": records,
    }


def test_same_subject_adjacency_resolves_repeated_generic_vote(
    tmp_path: Path,
) -> None:
    """
    Confirm a confident same-subject record disambiguates repeated vote text.

    :param tmp_path: Pytest temporary directory
    :return: None
    """
    segments_path = tmp_path / "meeting.segments.jsonl"
    _write_segments(
        segments_path,
        [
            (0, 0.0, "Opening remarks for the annual meeting."),
            (1, 10.0, "All in favor?"),
            (
                2,
                20.0,
                "Item nine is disposal of surplus and obsolete equipment. "
                "The list is provided.",
            ),
            (3, 30.0, "Thank you. All in favor?"),
        ],
    )
    artifact = _artifact(
        [
            {
                "record_type": "procedural_action",
                "subject": "Disposal of Equipment Vote",
                "summary": "The disposal item was introduced.",
                "evidence": (
                    "Item nine is disposal of surplus and obsolete equipment. "
                    "The list is provided."
                ),
                "source_chunks": [1],
            },
            {
                "record_type": "vote",
                "subject": "Disposal of Equipment Vote",
                "summary": "The electors approved the disposal item.",
                "evidence": "All in favor?",
                "source_chunks": [1],
            },
        ]
    )
    config = RecordTimestampConfig(
        backend="lexical",
        max_window_segments=1,
        min_score=0.40,
        min_margin=0.08,
    )

    outcome = align_records_artifact(
        artifact=artifact,
        segments_path=segments_path,
        config=config,
    )

    vote = outcome.artifact["records"][1]
    assert vote["timestamp"] == 30.0
    assert vote["video_id"] == "AbC_dEf-123"
    assert outcome.report["record_matches"][1]["resolved_by_same_subject"] is True


def test_cross_segment_evidence_uses_starting_segment_timestamp(
    tmp_path: Path,
) -> None:
    """
    Confirm evidence spanning adjacent segments receives the first timestamp.

    :param tmp_path: Pytest temporary directory
    :return: None
    """
    segments_path = tmp_path / "meeting.segments.jsonl"
    _write_segments(
        segments_path,
        [
            (
                0,
                40.0,
                "Presentation and review of annual Township Meeting Minutes "
                "for 2025. We need a motion.",
            ),
            (1, 50.0, "And a second."),
        ],
    )
    artifact = _artifact(
        [
            {
                "record_type": "procedural_action",
                "subject": "Minutes Review Motion",
                "summary": "The annual meeting minutes were presented.",
                "evidence": (
                    "Presentation and review of annual Township Meeting Minutes "
                    "for 2025. We need a motion. And a second."
                ),
                "source_chunks": [1],
            }
        ]
    )
    config = RecordTimestampConfig(
        backend="lexical",
        max_window_segments=2,
        min_score=0.40,
        min_margin=0.02,
    )

    outcome = align_records_artifact(
        artifact=artifact,
        segments_path=segments_path,
        config=config,
    )

    assert outcome.artifact["records"][0]["timestamp"] == 40.0
    assert outcome.report["records"]["matched_count"] == 1


def test_weak_generic_fuzzy_evidence_remains_unaligned(tmp_path: Path) -> None:
    """
    Confirm incomplete generic vote text is not forced onto a nearby vote.

    :param tmp_path: Pytest temporary directory
    :return: None
    """
    segments_path = tmp_path / "meeting.segments.jsonl"
    _write_segments(
        segments_path,
        [(0, 72.0, "All in favor? Aye. Any opposed?")],
    )
    artifact = _artifact(
        [
            {
                "record_type": "vote",
                "subject": "Adjournment Motion Outcome",
                "summary": "The meeting adjourned.",
                "evidence": "All in favor? Aye. Any opposed? Being adjourned.",
                "source_chunks": [1],
            }
        ]
    )
    config = RecordTimestampConfig(
        backend="lexical",
        max_window_segments=1,
        min_score=0.40,
        min_margin=0.02,
    )

    outcome = align_records_artifact(
        artifact=artifact,
        segments_path=segments_path,
        config=config,
    )

    record = outcome.artifact["records"][0]
    match = outcome.report["record_matches"][0]
    assert "timestamp" not in record
    assert match["matched"] is False
    assert match["reason"] == (
        "generic fuzzy evidence lacks sufficient lexical coverage"
    )


def test_alignment_fingerprint_reuses_current_enrichment(tmp_path: Path) -> None:
    """
    Confirm alignment identity ignores derived timestamp fields.

    :param tmp_path: Pytest temporary directory
    :return: None
    """
    segments_path = tmp_path / "meeting.segments.jsonl"
    _write_segments(segments_path, [(0, 12.0, "The motion passed.")])
    artifact = _artifact(
        [
            {
                "record_type": "vote",
                "subject": "Motion outcome",
                "summary": "The motion passed.",
                "evidence": "The motion passed.",
                "source_chunks": [1],
            }
        ]
    )
    config = RecordTimestampConfig(
        backend="lexical",
        max_window_segments=1,
        min_score=0.40,
        min_margin=0.02,
    )

    outcome = align_records_artifact(
        artifact=artifact,
        segments_path=segments_path,
        config=config,
    )

    assert alignment_is_current(
        artifact=outcome.artifact,
        segments_path=segments_path,
        config=config,
    )

def test_placeholder_video_id_is_not_retained_as_source_video(
    tmp_path: Path,
) -> None:
    """
    Confirm sliced placeholder IDs are omitted from enriched records.

    :param tmp_path: Pytest temporary directory
    :return: None
    """
    segments_path = tmp_path / "meeting.segments.jsonl"
    with segments_path.open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "source": "meeting_without_source_video",
                    "segment_index": 0,
                    "start_time": 12.0,
                    "end_time": 20.0,
                    "video_id": "123456789ab",
                    "text_raw": "The motion passed.",
                }
            )
            + "\n"
        )
    artifact = _artifact(
        [
            {
                "record_type": "vote",
                "subject": "Motion outcome",
                "summary": "The motion passed.",
                "evidence": "The motion passed.",
                "source_chunks": [1],
            }
        ]
    )
    config = RecordTimestampConfig(
        backend="lexical",
        max_window_segments=1,
        min_score=0.40,
        min_margin=0.02,
    )

    outcome = align_records_artifact(
        artifact=artifact,
        segments_path=segments_path,
        config=config,
    )

    record = outcome.artifact["records"][0]
    assert record["timestamp"] == 12.0
    assert "video_id" not in record


def test_alignment_report_uses_supplied_project_relative_labels(
    tmp_path: Path,
) -> None:
    """
    Confirm diagnostics serialize portable input labels instead of host paths.

    :param tmp_path: Pytest temporary directory
    :return: None
    """
    segments_path = tmp_path / "meeting.segments.jsonl"
    _write_segments(segments_path, [(0, 12.0, "The motion passed.")])
    artifact = _artifact(
        [
            {
                "record_type": "vote",
                "subject": "Motion outcome",
                "summary": "The motion passed.",
                "evidence": "The motion passed.",
                "source_chunks": [1],
            }
        ]
    )
    config = RecordTimestampConfig(
        backend="lexical",
        max_window_segments=1,
        min_score=0.40,
        min_margin=0.02,
    )

    outcome = align_records_artifact(
        artifact=artifact,
        segments_path=segments_path,
        config=config,
        segments_label="./data/entity/transcripts/meeting.segments.jsonl",
        records_label="./data/entity/records/meeting.records.json",
    )

    assert outcome.report["inputs"] == {
        "segments": "./data/entity/transcripts/meeting.segments.jsonl",
        "records": "./data/entity/records/meeting.records.json",
    }


def test_hybrid_shortlists_windows_from_stored_segment_embeddings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Confirm hybrid mode avoids exhaustive lexical window comparisons.

    :param tmp_path: Pytest temporary directory
    :param monkeypatch: Pytest monkeypatch fixture
    :return: None
    """
    import numpy as np

    segments_path = tmp_path / "meeting.segments.jsonl"
    rows: list[tuple[int, float, str, list[float]]] = []
    for index in range(30):
        if index == 20:
            rows.append(
                (
                    index,
                    float(index * 10),
                    "Target motion approved the equipment purchase.",
                    [1.0, 0.0, 0.0],
                )
            )
        else:
            rows.append(
                (
                    index,
                    float(index * 10),
                    f"Unrelated county discussion segment {index}.",
                    [0.0, 1.0, 0.0],
                )
            )
    _write_embedded_segments(segments_path, rows)
    artifact = _artifact(
        [
            {
                "record_type": "motion",
                "subject": "Equipment purchase",
                "summary": "The equipment purchase was approved.",
                "evidence": "Target motion approved the equipment purchase.",
                "source_chunks": [2],
            }
        ]
    )

    encoded_batches: list[list[str]] = []

    def fake_encode(
        model_name: str,
        device: str,
        texts: list[str],
    ) -> np.ndarray:
        """
        Return deterministic fixture embeddings and retain encoded text batches.

        :param model_name: Unused model name
        :param device: Unused runtime device
        :param texts: Text batch
        :return: Normalized fixture embeddings
        """
        del model_name, device
        encoded_batches.append(list(texts))
        values = [
            [1.0, 0.0, 0.0]
            if "equipment" in text.casefold()
            else [0.0, 1.0, 0.0]
            for text in texts
        ]
        return np.asarray(values, dtype=np.float32)

    lexical_calls = 0
    original_lexical_match = timestamp_module.lexical_match

    def count_lexical_calls(
        query_tokens: list[str] | tuple[str, ...],
        candidate_tokens: list[str] | tuple[str, ...],
    ) -> object:
        """
        Count candidate validation calls while preserving matcher behavior.

        :param query_tokens: Evidence tokens
        :param candidate_tokens: Candidate window tokens
        :return: Original lexical match result
        """
        nonlocal lexical_calls
        lexical_calls += 1
        return original_lexical_match(query_tokens, candidate_tokens)

    monkeypatch.setattr(timestamp_module, "resolve_device", lambda _value: "cpu")
    monkeypatch.setattr(timestamp_module, "encode_texts", fake_encode)
    monkeypatch.setattr(timestamp_module, "lexical_match", count_lexical_calls)

    outcome = align_records_artifact(
        artifact=artifact,
        segments_path=segments_path,
        config=RecordTimestampConfig(
            backend="hybrid",
            max_window_segments=3,
            candidate_segments_per_query=1,
            candidate_neighbor_radius=0,
        ),
    )

    record = outcome.artifact["records"][0]
    settings = outcome.report["settings"]
    assert record["timestamp"] == 200.0
    assert settings["candidate_strategy"] == "stored_segment_embedding_top_k"
    assert settings["evaluated_window_count"] < settings["window_count"]
    assert lexical_calls == settings["evaluated_window_count"]
    assert len(encoded_batches) == 1
    assert len(encoded_batches[0]) == 2
    assert settings["reused_segment_embedding_count"] == 30
    assert settings["derived_window_embedding_count"] == settings["window_count"]


def test_hybrid_shortlist_preserves_all_exact_generic_occurrences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Confirm top-K filtering cannot hide a competing exact generic phrase.

    :param tmp_path: Pytest temporary directory
    :param monkeypatch: Pytest monkeypatch fixture
    :return: None
    """
    import numpy as np

    segments_path = tmp_path / "meeting.segments.jsonl"
    _write_embedded_segments(
        segments_path,
        [
            (0, 10.0, "All in favor?", [1.0, 0.0]),
            (1, 90.0, "All in favor?", [1.0, 0.0]),
        ],
    )
    artifact = _artifact(
        [
            {
                "record_type": "vote",
                "subject": "Generic vote",
                "summary": "A vote occurred.",
                "evidence": "All in favor?",
                "source_chunks": [],
            }
        ]
    )

    def fake_encode(
        model_name: str,
        device: str,
        texts: list[str],
    ) -> np.ndarray:
        """
        Return equal query vectors while top-K retains one tied source segment.

        :param model_name: Unused model name
        :param device: Unused runtime device
        :param texts: Text batch
        :return: Fixture embeddings
        """
        del model_name, device
        return np.asarray([[1.0, 0.0] for _text in texts], dtype=np.float32)

    monkeypatch.setattr(timestamp_module, "resolve_device", lambda _value: "cpu")
    monkeypatch.setattr(timestamp_module, "encode_texts", fake_encode)

    outcome = align_records_artifact(
        artifact=artifact,
        segments_path=segments_path,
        config=RecordTimestampConfig(
            backend="hybrid",
            max_window_segments=1,
            min_score=0.40,
            min_margin=0.02,
            candidate_segments_per_query=1,
            candidate_neighbor_radius=0,
        ),
    )

    record = outcome.artifact["records"][0]
    match = outcome.report["record_matches"][0]
    assert "timestamp" not in record
    assert match["ambiguous"] is True
    assert len(match["candidates"]) == 2


def test_hybrid_requires_stored_segment_embeddings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Confirm semantic alignment fails clearly before loading the model.

    :param tmp_path: Pytest temporary directory
    :param monkeypatch: Pytest monkeypatch fixture
    :return: None
    """
    segments_path = tmp_path / "meeting.segments.jsonl"
    _write_segments(segments_path, [(0, 12.0, "The motion passed.")])
    artifact = _artifact(
        [
            {
                "record_type": "vote",
                "subject": "Motion outcome",
                "summary": "The motion passed.",
                "evidence": "The motion passed.",
                "source_chunks": [1],
            }
        ]
    )

    def fail_encode(*_args: object, **_kwargs: object) -> object:
        """
        Fail if model encoding begins before segment validation.

        :return: Never returns
        :raises AssertionError: Always
        """
        raise AssertionError("encode_texts should not be called")

    monkeypatch.setattr(timestamp_module, "resolve_device", lambda _value: "cpu")
    monkeypatch.setattr(timestamp_module, "encode_texts", fail_encode)

    with pytest.raises(ValueError, match="requires stored embeddings"):
        align_records_artifact(
            artifact=artifact,
            segments_path=segments_path,
            config=RecordTimestampConfig(backend="hybrid"),
        )
