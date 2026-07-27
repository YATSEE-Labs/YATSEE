"""
Tests for production Records/Events summary generation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from yatsee.core.errors import ValidationError
from yatsee.intel.llm import GeneratedChunk, LLMRuntime, TokenUsage
from yatsee.intel.summary import (
    build_records_events_summary_input,
    generate_records_events_summary,
    resolve_artifact_meeting_type,
)


def _artifacts() -> tuple[dict, dict]:
    """
    Build representative Records and Events artifacts.

    :return: Complete Records and Events artifacts
    """
    records = [
        {
            "record_type": "project",
            "subject": "New bike path",
            "summary": (
                "The new bike path will begin construction on September 23, 2026."
            ),
            "evidence": "Construction starts September 23, 2026.",
            "source_chunks": [2],
        },
        {
            "record_type": "financial",
            "subject": "New bike path funding",
            "summary": "The council approved $125,000 for the new bike path.",
            "evidence": "The allocation is $125,000.",
            "source_chunks": [2],
        },
        {
            "record_type": "vote",
            "subject": "New bike path funding",
            "summary": "The funding passed by a 6-1 vote.",
            "evidence": "That passes six to one.",
            "attributes": {
                "vote_outcome": "passed",
                "vote_tally": "6-1",
            },
            "source_chunks": [2],
        },
        {
            "record_type": "follow_up",
            "subject": "Trail signage",
            "summary": "Staff will return with trail signage options.",
            "evidence": "Staff will bring back signage options.",
            "source_chunks": [3],
        },
    ]
    event_records = [
        {**record, "record_index": index}
        for index, record in enumerate(records[:3], start=1)
    ]
    events = [
        {
            "event_id": "event_0001",
            "event_type": "decision",
            "subject": "New bike path funding",
            "summary": "The council approved funding for the new bike path.",
            "outcome": "passed",
            "source_record_indices": [1, 2, 3],
            "evidence": [
                {
                    "text": "The allocation is $125,000 and construction starts September 23, 2026.",
                    "source_record_indices": [1, 2],
                },
                {
                    "text": "That passes six to one.",
                    "source_record_indices": [3],
                },
            ],
            "source_records": event_records,
            "timestamp": 120.0,
        }
    ]
    return (
        {
            "artifact_kind": "records",
            "status": "complete",
            "meeting_type": "city_council",
            "records": records,
        },
        {
            "artifact_kind": "events",
            "status": "complete",
            "meeting": "bike_path_meeting",
            "meeting_date": "2026-09-01",
            "meeting_type": "city_council",
            "events": events,
        },
    )


def _runtime() -> LLMRuntime:
    """
    Build a local fake runtime for generation tests.

    :return: LLM runtime
    """
    return LLMRuntime(
        provider_name="ollama",
        provider_target="http://127.0.0.1:11434",
        api_key=None,
        model="gemma4:e4b",
        num_ctx=16384,
    )


def _generated(output: str, chunk_id: int, chunk_count: int) -> GeneratedChunk:
    """
    Build one generated chunk for reduction-loop tests.

    :param output: Model output text
    :param chunk_id: One-based chunk identifier
    :param chunk_count: Total chunks in the mocked pass
    :return: Generated chunk
    """
    return GeneratedChunk(
        chunk_id=chunk_id,
        chunk_count=chunk_count,
        source_text=f"source-{chunk_id}",
        prompt=f"prompt-{chunk_id}",
        output=output,
        input_tokens=10,
        output_tokens=5,
    )


def test_summary_input_preserves_material_facts_and_unlinked_records() -> None:
    """
    Preserve amounts, dates, vote tallies, and Records missing from Events.

    :return: None
    """
    records, events = _artifacts()

    header, blocks = build_records_events_summary_input(records, events)
    rendered = f"{header}\n\n" + "\n\n".join(blocks)

    assert "$125,000" in rendered
    assert "September 23, 2026" in rendered
    assert 'attributes={"vote_outcome":"passed","vote_tally":"6-1"}' in rendered
    assert "Additional extracted facts" in rendered
    assert "Staff will return with trail signage options." in rendered
    assert "source_chunks" not in rendered
    assert "Construction starts September 23, 2026." not in rendered
    assert '"source_records"' not in rendered
    assert "Record 1" not in rendered
    assert "event_0001" not in rendered


def test_summary_uses_shared_chunking_and_bounded_reduction() -> None:
    """
    Use shared chunk generation, routed prompts, and bounded reduction rules.

    :return: None
    """
    records, events = _artifacts()
    prompts = {
        "records_summary_first": "FIRST\n{context}\n{text}",
        "records_summary_multi": "MULTI\n{context}\n{text}",
        "records_summary_final": "FINAL\n{context}\n{text}",
    }
    pass_one = (
        [
            _generated(" ".join(["Part A"] * 20), 1, 2),
            _generated(" ".join(["Part B"] * 20), 2, 2),
        ],
        TokenUsage(input_tokens=100, output_tokens=20),
    )
    pass_two = (
        [_generated("Combined facts.", 1, 1)],
        TokenUsage(input_tokens=30, output_tokens=10),
    )

    with (
        patch(
            "yatsee.intel.summary.generate_chunk_outputs",
            side_effect=[pass_one, pass_two],
        ) as chunk_generation,
        patch(
            "yatsee.intel.summary.llm_generate_with_runtime",
            return_value="Final summary output.",
        ) as final_generation,
    ):
        result = generate_records_events_summary(
            session=object(),
            llm_runtime=_runtime(),
            records_artifact=records,
            events_artifact=events,
            context="Bike path meeting",
            prompts=prompts,
            chunk_style="sentence",
            max_tokens=20,
            max_pass=3,
            first_prompt_id="records_summary_first",
            multi_prompt_id="records_summary_multi",
        )

    assert result["summary"] == "Final summary output."
    assert result["pass_count"] == 2
    assert result["chunk_counts"] == [2, 1]
    assert result["structured_input_tokens"] > 0
    assert chunk_generation.call_count == 2
    assert chunk_generation.call_args_list[0].kwargs["chunk_style"] == "sentence"
    assert chunk_generation.call_args_list[0].kwargs["max_tokens"] == 20
    assert (
        chunk_generation.call_args_list[0].kwargs["prompt_id"]
        == "records_summary_first"
    )
    assert (
        chunk_generation.call_args_list[1].kwargs["prompt_id"]
        == "records_summary_multi"
    )
    assert "Part A" in chunk_generation.call_args_list[1].kwargs["text"]
    assert "Part B" in chunk_generation.call_args_list[1].kwargs["text"]
    assert final_generation.call_count == 1
    assert final_generation.call_args.kwargs["prompt"].startswith("FINAL")
    assert "Combined facts." in final_generation.call_args.kwargs["prompt"]


def test_summary_skips_consolidation_when_structured_facts_fit() -> None:
    """
    Send already-small structured facts directly to final presentation.

    :return: None
    """
    records, events = _artifacts()
    prompts = {
        "records_summary_first": "FIRST\n{context}\n{text}",
        "records_summary_multi": "MULTI\n{context}\n{text}",
        "records_summary_final": "FINAL\n{context}\n{text}",
    }

    with (
        patch("yatsee.intel.summary.generate_chunk_outputs") as chunk_generation,
        patch(
            "yatsee.intel.summary.llm_generate_with_runtime",
            return_value="Final summary output.",
        ) as final_generation,
    ):
        result = generate_records_events_summary(
            session=object(),
            llm_runtime=_runtime(),
            records_artifact=records,
            events_artifact=events,
            context="Bike path meeting",
            prompts=prompts,
            chunk_style="sentence",
            max_tokens=5000,
            max_pass=3,
            first_prompt_id="records_summary_first",
            multi_prompt_id="records_summary_multi",
        )

    assert result["pass_count"] == 0
    assert result["chunk_counts"] == []
    assert result["reduced_tokens"] == result["structured_input_tokens"]
    chunk_generation.assert_not_called()
    assert final_generation.call_count == 1
    assert final_generation.call_args.kwargs["prompt"].startswith("FINAL")


def test_single_pass_setting_still_runs_final_synthesis() -> None:
    """
    Preserve the normal max-pass-one behavior for bounded experiments.

    :return: None
    """
    records, events = _artifacts()
    prompts = {
        "records_summary_first": "FIRST\n{context}\n{text}",
        "records_summary_multi": "MULTI\n{context}\n{text}",
        "records_summary_final": "FINAL\n{context}\n{text}",
    }
    pass_one = (
        [_generated("Consolidated facts.", 1, 1)],
        TokenUsage(input_tokens=50, output_tokens=10),
    )

    with (
        patch(
            "yatsee.intel.summary.generate_chunk_outputs",
            return_value=pass_one,
        ) as chunk_generation,
        patch(
            "yatsee.intel.summary.llm_generate_with_runtime",
            return_value="Final summary output.",
        ) as final_generation,
    ):
        result = generate_records_events_summary(
            session=object(),
            llm_runtime=_runtime(),
            records_artifact=records,
            events_artifact=events,
            context="Bike path meeting",
            prompts=prompts,
            chunk_style="sentence",
            max_tokens=20,
            max_pass=1,
            first_prompt_id="records_summary_first",
            multi_prompt_id="records_summary_multi",
        )

    assert result["pass_count"] == 1
    assert result["chunk_counts"] == [1]
    assert chunk_generation.call_count == 1
    assert final_generation.call_count == 1


def test_summary_input_omits_exact_event_record_duplicates() -> None:
    """
    Avoid repeating a canonical Record already represented by its Event summary.

    :return: None
    """
    records = {
        "status": "complete",
        "meeting_type": "general",
        "records": [
            {
                "record_type": "discussion",
                "subject": "Bike path schedule",
                "summary": "Construction starts September 23, 2026.",
                "evidence": "Construction starts September 23, 2026.",
                "source_chunks": [1],
            }
        ],
    }
    events = {
        "status": "complete",
        "meeting": "meeting",
        "meeting_type": "general",
        "events": [
            {
                "event_id": "event_0001",
                "event_type": "discussion",
                "subject": "Bike path schedule",
                "summary": "Construction starts September 23, 2026.",
                "source_record_indices": [1],
                "source_records": records["records"],
            }
        ],
    }

    header, blocks = build_records_events_summary_input(records, events)
    rendered = f"{header}\n\n" + "\n\n".join(blocks)

    assert rendered.count("Construction starts September 23, 2026.") == 1
    assert "Source Records:" not in rendered


def test_meeting_summary_routes_from_events_artifact(tmp_path) -> None:
    """
    Route production prompts from the deterministic Events meeting type.

    :param tmp_path: Pytest temporary directory
    :return: None
    """
    from yatsee.intel.summary import SummaryRunConfig, summarize_one_meeting

    records, events = _artifacts()
    prompts = {
        "city_first": "CITY FIRST\n{context}\n{text}",
        "city_multi": "CITY MULTI\n{context}\n{text}",
        "city_final": "CITY FINAL\n{context}\n{text}",
        "general_first": "GENERAL FIRST\n{context}\n{text}",
        "general_multi": "GENERAL MULTI\n{context}\n{text}",
        "general_final": "GENERAL FINAL\n{context}\n{text}",
    }
    prompt_bundle = {
        "prompts": prompts,
        "prompt_router": {
            "city_council": {
                "first": "city_first",
                "multi": "city_multi",
                "final": "city_final",
            },
            "general": {
                "first": "general_first",
                "multi": "general_multi",
                "final": "general_final",
            },
        },
        "density_keywords": {"keywords": []},
    }
    run_cfg = SummaryRunConfig(
        chunk_style="sentence",
        max_tokens=1500,
        max_pass=3,
        output_format="markdown",
        first_prompt=None,
        second_prompt=None,
        final_prompt=None,
        enable_chunk_writer=False,
        show_pricing=False,
        pricing_provider=None,
        pricing_model=None,
    )
    generated_result = {
        "summary": "Final summary.",
        "pass_count": 2,
        "chunk_counts": [2, 1],
        "structured_input_tokens": 100,
        "reduced_tokens": 25,
        "event_count": 1,
        "record_count": 4,
        "input_tokens": 200,
        "output_tokens": 50,
        "total_tokens": 250,
    }

    with patch(
        "yatsee.intel.summary.generate_records_events_summary",
        return_value=generated_result,
    ) as generation:
        result = summarize_one_meeting(
            session=object(),
            llm_runtime=_runtime(),
            records_artifact=records,
            events_artifact=events,
            base_name="bike_path_meeting",
            context="Bike path meeting",
            prompt_bundle=prompt_bundle,
            entity_cfg={},
            run_cfg=run_cfg,
            output_dir=str(tmp_path),
        )

    assert result["meeting_type"] == "city_council"
    assert result["output_path"].endswith("bike_path_meeting.summary.md")

    summary_text = Path(result["output_path"]).read_text(encoding="utf-8")
    assert summary_text == "# Summary: bike_path_meeting\n\nFinal summary.\n"
    assert "verification_required:" not in summary_text
    assert not summary_text.startswith("---\n")

    assert generation.call_args.kwargs["first_prompt_id"] == "city_first"
    assert generation.call_args.kwargs["multi_prompt_id"] == "city_multi"
    assert generation.call_args.kwargs["final_prompt_id"] == "city_final"


def test_summary_input_rejects_stale_records_events_pair() -> None:
    """
    Reject artifacts that reference different normalized transcript versions.

    :return: None
    """
    records, events = _artifacts()
    records["source_sha256"] = "records-hash"
    events["source_sha256"] = "events-hash"

    with pytest.raises(
        ValidationError,
        match="different normalized sources",
    ):
        build_records_events_summary_input(records, events)


def test_summary_input_rejects_meeting_type_mismatch() -> None:
    """
    Reject stale Events artifacts routed for a different meeting type.

    :return: None
    """
    records, events = _artifacts()
    events["meeting_type"] = "finance_committee"

    with pytest.raises(ValidationError, match="disagree on meeting_type"):
        build_records_events_summary_input(records, events)


def test_meeting_type_falls_back_from_unknown_events_to_records() -> None:
    """
    Use the Records classification when Events carries no meaningful value.

    :return: None
    """
    records, events = _artifacts()
    events["meeting_type"] = "unknown"

    assert resolve_artifact_meeting_type(records, events) == "city_council"


def test_meeting_type_defaults_to_general_when_both_are_unknown() -> None:
    """
    Route unclassified paired artifacts through the required general fallback.

    :return: None
    """
    records, events = _artifacts()
    records["meeting_type"] = "unknown"
    events["meeting_type"] = ""

    assert resolve_artifact_meeting_type(records, events) == "general"
