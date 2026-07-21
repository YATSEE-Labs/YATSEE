"""
Validate records parsing and partial-failure behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yatsee.core.errors import ValidationError
from yatsee.core.hashing import compute_text_sha256
from yatsee.intel.llm import GeneratedChunk
from yatsee.intel.record import (
    ARTIFACT_KIND,
    ARTIFACT_SCHEMA_VERSION,
    EXTRACTION_VERSION,
    RecordsRunConfig,
    _artifact_paths,
    _parse_records_response_with_issues,
    _prepare_artifact_directory,
    _write_generated_record_chunk,
    build_records_extraction_metadata,
    load_reusable_records_artifact,
    parse_records_response,
    render_records_markdown,
    run_records_stage,
)


def _record(record_type: str, subject: str) -> dict[str, str]:
    """
    Build one minimal records payload item.

    :param record_type: Record type value
    :param subject: Record subject value
    :return: Minimal valid record dictionary
    """
    return {
        "record_type": record_type,
        "subject": subject,
        "summary": f"Summary for {subject}",
        "evidence": f"Evidence for {subject}",
    }


def test_proclamation_is_an_allowed_record_type() -> None:
    """
    Confirm proclamation records survive strict parsing.

    :return: None
    """
    parsed = parse_records_response(
        json.dumps({"records": [_record("proclamation", "Centennial Day")]})
    )

    assert parsed[0]["record_type"] == "proclamation"


def test_tolerant_parser_preserves_valid_sibling_records() -> None:
    """
    Confirm one unsupported type does not discard valid sibling records.

    :return: None
    """
    raw_output = json.dumps(
        {
            "records": [
                _record("motion", "Approve agenda"),
                _record("unsupported_type", "Bad record"),
                _record("vote", "Agenda vote"),
            ]
        }
    )

    records, issues = _parse_records_response_with_issues(raw_output)

    assert [record["record_type"] for record in records] == ["motion", "vote"]
    assert issues[0]["scope"] == "record"
    assert issues[0]["record_index"] == 2

    with pytest.raises(ValidationError):
        parse_records_response(raw_output)


def test_chunk_writer_marks_partial_without_raising(tmp_path: Path) -> None:
    """
    Confirm partial chunk artifacts retain valid records and diagnostics.

    :param tmp_path: Pytest temporary directory
    :return: None
    """
    paths = _artifact_paths(str(tmp_path), "meeting")
    _prepare_artifact_directory(paths)
    parsed_chunks: dict[int, list[dict[str, object]]] = {}
    chunk_statuses: dict[int, str] = {}
    chunk_validation_issues: dict[int, list[dict[str, object]]] = {}
    generated_chunk = GeneratedChunk(
        chunk_id=1,
        chunk_count=1,
        source_text="Source transcript text.",
        prompt="Rendered prompt.",
        output=json.dumps(
            {
                "records": [
                    _record("motion", "Approve agenda"),
                    _record("unsupported_type", "Bad record"),
                ]
            }
        ),
        input_tokens=10,
        output_tokens=10,
    )

    _write_generated_record_chunk(
        generated_chunk,
        paths=paths,
        prompt_id="city_council_records",
        parsed_chunks=parsed_chunks,
        chunk_statuses=chunk_statuses,
        chunk_validation_issues=chunk_validation_issues,
    )

    assert chunk_statuses[1] == "partial"
    assert len(parsed_chunks[1]) == 1

    chunk_artifact = json.loads(
        Path(paths["chunks_dir"], "part_001.records.json").read_text()
    )
    error_artifact = json.loads(
        Path(paths["errors_dir"], "part_001.validation.json").read_text()
    )

    assert chunk_artifact["status"] == "partial"
    assert chunk_artifact["validation_issue_count"] == 1
    assert error_artifact["validation_issues"][0]["record_index"] == 2


def test_chunk_writer_marks_invalid_json_failed_without_raising(tmp_path: Path) -> None:
    """
    Confirm malformed JSON fails only the current chunk and writes diagnostics.

    :param tmp_path: Pytest temporary directory
    :return: None
    """
    paths = _artifact_paths(str(tmp_path), "meeting")
    _prepare_artifact_directory(paths)
    parsed_chunks: dict[int, list[dict[str, object]]] = {}
    chunk_statuses: dict[int, str] = {}
    chunk_validation_issues: dict[int, list[dict[str, object]]] = {}
    generated_chunk = GeneratedChunk(
        chunk_id=1,
        chunk_count=2,
        source_text="Source transcript text.",
        prompt="Rendered prompt.",
        output="not valid json",
        input_tokens=10,
        output_tokens=3,
    )

    _write_generated_record_chunk(
        generated_chunk,
        paths=paths,
        prompt_id="city_council_records",
        parsed_chunks=parsed_chunks,
        chunk_statuses=chunk_statuses,
        chunk_validation_issues=chunk_validation_issues,
    )

    assert chunk_statuses[1] == "failed"
    assert parsed_chunks[1] == []
    assert chunk_validation_issues[1][0]["scope"] == "line"

    chunk_artifact = json.loads(
        Path(paths["chunks_dir"], "part_001.records.json").read_text()
    )
    assert chunk_artifact["status"] == "failed"


def test_records_passes_profile_density_keywords_to_shared_chunker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Confirm records forwards profile density keywords to shared generation.

    :param tmp_path: Pytest temporary directory
    :param monkeypatch: Pytest monkeypatch fixture
    :return: None
    """
    from yatsee.intel.llm import LLMRuntime, TokenUsage
    from yatsee.intel.record import extract_records_from_transcript

    captured: dict[str, object] = {}

    def fake_generate_chunk_outputs(
        **kwargs: object,
    ) -> tuple[list[GeneratedChunk], TokenUsage]:
        captured.update(kwargs)
        generated = GeneratedChunk(
            chunk_id=1,
            chunk_count=1,
            source_text="A motion was made.",
            prompt="Rendered prompt.",
            output="[]",
            input_tokens=10,
            output_tokens=1,
        )
        on_chunk = kwargs.get("on_chunk")
        assert callable(on_chunk)
        on_chunk(generated)
        return [generated], TokenUsage(input_tokens=10, output_tokens=1)

    monkeypatch.setattr(
        "yatsee.intel.record.generate_chunk_outputs",
        fake_generate_chunk_outputs,
    )

    runtime = LLMRuntime(
        provider_name="ollama",
        provider_target="http://127.0.0.1:11434",
        api_key=None,
        model="test-model",
        num_ctx=8192,
    )
    run_cfg = RecordsRunConfig(
        chunk_style="density",
        max_tokens=2000,
        disable_auto_classification=True,
        meeting_type=None,
        show_pricing=False,
        pricing_provider=None,
        pricing_model=None,
    )

    extract_records_from_transcript(
        session=object(),
        llm_runtime=runtime,
        transcript_path="meeting.txt",
        transcript="A motion was made.",
        base_name="meeting",
        context="City Council",
        classifier_bundle={
            "classifier_prompt": "",
            "allowed_meeting_types": ["general"],
            "path": "classifier.toml",
        },
        records_bundle={
            "records_router": {"general": "general_records"},
            "prompts": {
                "general_records": "Context: {context}\nTranscript: {text}"
            },
            "path": "records_prompt.toml",
        },
        density_keywords={"motion", "vote", "ordinance"},
        entity_cfg={},
        run_cfg=run_cfg,
        output_dir=str(tmp_path),
        job_profile="civic",
        source_sha256=compute_text_sha256("A motion was made."),
        extraction_metadata={
            "extraction_version": EXTRACTION_VERSION,
            "extraction_fingerprint": "fingerprint",
            "classifier_sha256": "classifier",
            "records_prompt_sha256": "records-prompt",
            "density_keywords_sha256": "density",
            "entity_context_sha256": "entity-context",
            "context_sha256": "context",
        },
    )

    assert captured["chunk_style"] == "density"
    assert captured["density_keywords"] == {"motion", "vote", "ordinance"}


def test_complete_self_describing_artifact_is_reusable(tmp_path: Path) -> None:
    """
    Confirm final JSON acts as its own cache identity.

    :param tmp_path: Pytest temporary directory
    :return: None
    """
    source_sha256 = compute_text_sha256("Normalized transcript.")
    artifact = {
        "artifact_kind": ARTIFACT_KIND,
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "extraction_version": EXTRACTION_VERSION,
        "extraction_fingerprint": "fingerprint",
        "source_sha256": source_sha256,
        "status": "complete",
        "chunk_count": 1,
        "successful_chunk_count": 1,
        "partial_chunk_count": 0,
        "failed_chunk_count": 0,
        "validation_issue_count": 0,
        "record_count": 1,
        "records": [
            {
                **_record("motion", "Approve agenda"),
                "source_chunks": [1],
            }
        ],
    }
    path = tmp_path / "meeting.records.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")

    loaded = load_reusable_records_artifact(
        str(path),
        source_sha256=source_sha256,
        extraction_fingerprint="fingerprint",
    )

    assert loaded == artifact
    assert load_reusable_records_artifact(
        str(path),
        source_sha256=compute_text_sha256("Changed transcript."),
        extraction_fingerprint="fingerprint",
    ) is None
    assert load_reusable_records_artifact(
        str(path),
        source_sha256=source_sha256,
        extraction_fingerprint="changed-fingerprint",
    ) is None


def test_records_markdown_is_deterministic_view_of_json() -> None:
    """
    Confirm Markdown groups records without inventing additional content.

    :return: None
    """
    artifact = {
        "source_transcript": "meeting.txt",
        "meeting_type": "township_board",
        "status": "complete",
        "records": [
            {
                **_record("motion", "Approve agenda"),
                "attribution": "Unknown",
                "source_chunks": [1],
            },
            {
                **_record("public_comment", "Road condition"),
                "attribution": "Jane Citizen",
                "source_chunks": [2],
            },
        ],
    }

    markdown = render_records_markdown(artifact)

    assert "## Motions and Votes" in markdown
    assert "## Public Comment" in markdown
    assert "**Approve agenda**" in markdown
    assert "Attribution: Unknown" not in markdown
    assert "Attribution: Jane Citizen" in markdown
    assert "Source chunks: 1" in markdown


def test_extraction_fingerprint_changes_with_prompt_or_model() -> None:
    """
    Confirm extraction-relevant changes invalidate records reuse.

    :return: None
    """
    from yatsee.intel.llm import LLMRuntime

    classifier_bundle = {
        "classifier_prompt": "Classify {text}",
        "allowed_meeting_types": ["general"],
    }
    records_bundle = {
        "records_router": {"general": "general_records"},
        "prompts": {"general_records": "Extract {text}"},
    }
    run_cfg = RecordsRunConfig(
        chunk_style="density",
        max_tokens=1500,
        disable_auto_classification=True,
        meeting_type=None,
        show_pricing=False,
        pricing_provider=None,
        pricing_model=None,
    )
    runtime = LLMRuntime(
        provider_name="ollama",
        provider_target="http://127.0.0.1:11434",
        api_key=None,
        model="gemma4:e4b",
        num_ctx=16384,
    )

    baseline = build_records_extraction_metadata(
        llm_runtime=runtime,
        run_cfg=run_cfg,
        job_profile="civic",
        context="Meeting",
        classifier_bundle=classifier_bundle,
        records_bundle=records_bundle,
        density_keywords={"motion"},
        entity_cfg={},
    )
    changed_prompts = build_records_extraction_metadata(
        llm_runtime=runtime,
        run_cfg=run_cfg,
        job_profile="civic",
        context="Meeting",
        classifier_bundle=classifier_bundle,
        records_bundle={
            **records_bundle,
            "prompts": {"general_records": "Changed {text}"},
        },
        density_keywords={"motion"},
        entity_cfg={},
    )
    changed_model = build_records_extraction_metadata(
        llm_runtime=LLMRuntime(
            provider_name="ollama",
            provider_target="http://127.0.0.1:11434",
            api_key=None,
            model="other-model",
            num_ctx=16384,
        ),
        run_cfg=run_cfg,
        job_profile="civic",
        context="Meeting",
        classifier_bundle=classifier_bundle,
        records_bundle=records_bundle,
        density_keywords={"motion"},
        entity_cfg={},
    )

    assert baseline["extraction_fingerprint"] != changed_prompts[
        "extraction_fingerprint"
    ]
    assert baseline["extraction_fingerprint"] != changed_model[
        "extraction_fingerprint"
    ]


def test_records_stage_reuses_cache_without_opening_llm_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Confirm a compatible final artifact skips all provider session work.

    :param tmp_path: Pytest temporary directory
    :param monkeypatch: Pytest monkeypatch fixture
    :return: None
    """
    from yatsee.intel.llm import LLMRuntime
    from yatsee.intel.resolve import extract_context_from_filename

    transcript_path = tmp_path / "meeting.txt"
    transcript_text = "Normalized transcript."
    transcript_path.write_text(transcript_text, encoding="utf-8")
    output_dir = tmp_path / "records"
    runtime = LLMRuntime(
        provider_name="ollama",
        provider_target="http://127.0.0.1:11434",
        api_key=None,
        model="gemma4:e4b",
        num_ctx=16384,
    )
    classifier_bundle = {
        "classifier_prompt": "Classify {text}",
        "allowed_meeting_types": ["general"],
        "path": "classifier.toml",
        "fallback": False,
    }
    records_bundle = {
        "records_router": {"general": "general_records"},
        "prompts": {"general_records": "Extract {text}"},
        "path": "records_prompt.toml",
    }
    summary_bundle = {
        "density_keywords": {"keywords": ["motion"]},
        "path": "prompts.toml",
    }
    run_cfg = RecordsRunConfig(
        chunk_style="density",
        max_tokens=1500,
        disable_auto_classification=True,
        meeting_type=None,
        show_pricing=False,
        pricing_provider=None,
        pricing_model=None,
    )
    extraction_metadata = build_records_extraction_metadata(
        llm_runtime=runtime,
        run_cfg=run_cfg,
        job_profile="civic",
        context=extract_context_from_filename(str(transcript_path), ""),
        classifier_bundle=classifier_bundle,
        records_bundle=records_bundle,
        density_keywords={"motion"},
        entity_cfg={},
    )
    paths = _artifact_paths(str(output_dir), "meeting")
    Path(paths["transcript_dir"]).mkdir(parents=True)
    artifact = {
        "artifact_kind": ARTIFACT_KIND,
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        **extraction_metadata,
        "status": "complete",
        "source_transcript": str(transcript_path),
        "source_sha256": compute_text_sha256(transcript_text),
        "meeting_type": "general",
        "job_profile": "civic",
        "prompt_id": "general_records",
        "provider": "ollama",
        "model": "gemma4:e4b",
        "chunk_count": 1,
        "successful_chunk_count": 1,
        "partial_chunk_count": 0,
        "failed_chunk_count": 0,
        "validation_issue_count": 0,
        "record_count": 1,
        "records": [
            {
                **_record("motion", "Approve agenda"),
                "source_chunks": [1],
            }
        ],
    }
    Path(paths["final_path"]).write_text(json.dumps(artifact), encoding="utf-8")

    monkeypatch.setattr(
        "yatsee.intel.record.resolve_record_paths",
        lambda *args, **kwargs: {
            "global_cfg": {
                "system": {
                    "llm_provider": "ollama",
                    "llm_provider_url": "http://127.0.0.1:11434",
                }
            },
            "entity_cfg": {},
            "model": "gemma4:e4b",
            "input_dir": str(tmp_path),
            "file_list": [str(transcript_path)],
            "output_dir": str(output_dir),
            "max_tokens": 1500,
            "num_ctx": 16384,
        },
    )
    monkeypatch.setattr(
        "yatsee.intel.record.load_classifier_bundle",
        lambda *args, **kwargs: classifier_bundle,
    )
    monkeypatch.setattr(
        "yatsee.intel.record.load_records_prompt_bundle",
        lambda *args, **kwargs: records_bundle,
    )
    monkeypatch.setattr(
        "yatsee.intel.record.load_summary_prompt_bundle",
        lambda *args, **kwargs: summary_bundle,
    )
    monkeypatch.setattr(
        "yatsee.intel.record.resolve_llm_runtime",
        lambda **kwargs: runtime,
    )

    def fail_session_creation() -> None:
        raise AssertionError("LLM session should not be created for reusable records")

    monkeypatch.setattr(
        "yatsee.intel.record.create_llm_session",
        fail_session_creation,
    )

    result = run_records_stage(
        global_config_path="yatsee.toml",
        job_profile="civic",
        chunk_style="density",
        disable_auto_classification=True,
    )

    assert result["extracted"] == 0
    assert result["reused"] == 1
    assert result["results"][0]["reused"] is True
    assert Path(paths["markdown_path"]).is_file()


def test_partial_rebuild_preserves_previous_complete_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Confirm a partial extraction attempt cannot replace the previous final JSON.

    :param tmp_path: Pytest temporary directory
    :param monkeypatch: Pytest monkeypatch fixture
    :return: None
    """
    from yatsee.intel.llm import LLMRuntime, TokenUsage
    from yatsee.intel.record import extract_records_from_transcript

    paths = _artifact_paths(str(tmp_path), "meeting")
    Path(paths["transcript_dir"]).mkdir(parents=True)
    previous_content = '{"sentinel": "previous-complete"}\n'
    Path(paths["final_path"]).write_text(previous_content, encoding="utf-8")

    def fake_generate_chunk_outputs(
        **kwargs: object,
    ) -> tuple[list[GeneratedChunk], TokenUsage]:
        generated = GeneratedChunk(
            chunk_id=1,
            chunk_count=1,
            source_text="A motion was made.",
            prompt="Rendered prompt.",
            output="not valid json",
            input_tokens=10,
            output_tokens=3,
        )
        on_chunk = kwargs.get("on_chunk")
        assert callable(on_chunk)
        on_chunk(generated)
        return [generated], TokenUsage(input_tokens=10, output_tokens=3)

    monkeypatch.setattr(
        "yatsee.intel.record.generate_chunk_outputs",
        fake_generate_chunk_outputs,
    )

    result = extract_records_from_transcript(
        session=object(),
        llm_runtime=LLMRuntime(
            provider_name="ollama",
            provider_target="http://127.0.0.1:11434",
            api_key=None,
            model="test-model",
            num_ctx=8192,
        ),
        transcript_path="meeting.txt",
        transcript="A motion was made.",
        base_name="meeting",
        context="Meeting",
        classifier_bundle={
            "classifier_prompt": "",
            "allowed_meeting_types": ["general"],
            "path": "classifier.toml",
        },
        records_bundle={
            "records_router": {"general": "general_records"},
            "prompts": {"general_records": "Extract {text}"},
            "path": "records_prompt.toml",
        },
        density_keywords={"motion"},
        entity_cfg={},
        run_cfg=RecordsRunConfig(
            chunk_style="density",
            max_tokens=1500,
            disable_auto_classification=True,
            meeting_type=None,
            show_pricing=False,
            pricing_provider=None,
            pricing_model=None,
        ),
        output_dir=str(tmp_path),
        job_profile="civic",
        source_sha256=compute_text_sha256("A motion was made."),
        extraction_metadata={
            "extraction_version": EXTRACTION_VERSION,
            "extraction_fingerprint": "fingerprint",
            "classifier_sha256": "classifier",
            "records_prompt_sha256": "records-prompt",
            "density_keywords_sha256": "density",
            "entity_context_sha256": "entity-context",
            "context_sha256": "context",
        },
    )

    assert result["status"] == "partial"
    assert result["cache_updated"] is False
    assert Path(paths["final_path"]).read_text(encoding="utf-8") == previous_content
    assert Path(paths["partial_path"]).is_file()
    assert Path(paths["partial_markdown_path"]).is_file()
