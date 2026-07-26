"""
Validate records parsing and partial-failure behavior.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from yatsee.core.errors import ConfigError, ValidationError
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
    load_reusable_records_artifact,
    load_timestamp_reusable_records_artifact,
    parse_records_response,
    render_records_markdown,
    run_records_stage,
)
from yatsee.intel.timestamp import ALIGNMENT_VERSION


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


def test_builtin_records_prompts_define_strict_required_field_contract() -> None:
    """
    Confirm first-run prompt profiles require evidence and stable event types.

    :return: None
    """
    project_root = Path(__file__).resolve().parents[1]
    prompt_path = project_root / "prompts" / "civic" / "records_prompt.toml"
    prompt_config = tomllib.loads(prompt_path.read_text(encoding="utf-8"))

    for prompt in prompt_config["prompts"].values():
        text = prompt["text"]
        assert "record_type describes the form of the civic event" in text
        assert "Every emitted object must contain non-empty" in text
        assert "Never omit the evidence field" in text
        assert "Never invent a new record_type" in text


def test_tolerant_parser_preserves_valid_sibling_records() -> None:
    """
    Confirm one rejected record does not discard valid sibling records.

    :return: None
    """
    raw_output = json.dumps(
        {
            "records": [
                _record("motion", "Approve agenda"),
                {
                    "record_type": "discussion",
                    "subject": "Bad record",
                    "summary": "Missing evidence",
                },
                _record("vote", "Agenda vote"),
            ]
        }
    )

    records, issues = _parse_records_response_with_issues(raw_output)

    assert [record["record_type"] for record in records] == ["motion", "vote"]
    assert issues[0]["scope"] == "record"
    assert issues[0]["record_index"] == 2
    assert issues[0]["action"] == "rejected"

    with pytest.raises(ValidationError):
        parse_records_response(raw_output)


def test_unsupported_record_type_is_normalized_to_other() -> None:
    """
    Confirm useful records with invented types remain in the stable schema.

    :return: None
    """
    raw_output = json.dumps(
        {
            "records": [
                _record("committee_report", "Committee update"),
            ]
        }
    )

    records, issues = _parse_records_response_with_issues(raw_output)

    assert records == [
        {
            "record_type": "other",
            "subject": "Committee update",
            "summary": "Summary for Committee update",
            "evidence": "Evidence for Committee update",
            "attributes": {"model_record_type": "committee_report"},
        }
    ]
    assert issues[0]["action"] == "normalized"
    assert issues[0]["original_record_type"] == "committee_report"
    assert parse_records_response(raw_output) == records


def test_chunk_writer_keeps_isolated_rejection_complete(tmp_path: Path) -> None:
    """
    Confirm one rejected record becomes a warning when siblings remain usable.

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
                    {
                        "record_type": "discussion",
                        "subject": "Bad record",
                        "summary": "Missing evidence",
                    },
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

    assert chunk_statuses[1] == "complete"
    assert len(parsed_chunks[1]) == 1

    chunk_artifact = json.loads(
        Path(paths["chunks_dir"], "part_001.records.json").read_text()
    )
    error_artifact = json.loads(
        Path(paths["errors_dir"], "part_001.validation.json").read_text()
    )

    assert chunk_artifact["status"] == "complete"
    assert chunk_artifact["validation_issue_count"] == 1
    assert chunk_artifact["validation_warning_count"] == 1
    assert chunk_artifact["rejected_record_count"] == 1
    assert chunk_artifact["normalized_record_count"] == 0
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
    )

    assert captured["chunk_style"] == "density"
    assert captured["density_keywords"] == {"motion", "vote", "ordinance"}


def test_source_hash_is_the_only_automatic_reuse_invalidator(
    tmp_path: Path,
) -> None:
    """
    Confirm prompt and extraction metadata do not invalidate records reuse.

    :param tmp_path: Pytest temporary directory
    :return: None
    """
    source_sha256 = compute_text_sha256("Normalized transcript.")
    artifact = {
        "artifact_kind": "historical_records",
        "schema_version": "historical_schema",
        "extraction_version": "records_v1",
        "extraction_fingerprint": "stale-fingerprint",
        "classifier_sha256": "stale-classifier",
        "records_prompt_sha256": "stale-prompt",
        "density_keywords_sha256": "stale-density",
        "entity_context_sha256": "stale-entity-context",
        "context_sha256": "stale-context",
        "source_sha256": source_sha256,
        "records": [
            {
                **_record("motion", "Approve agenda"),
                "source_chunks": [1],
            }
        ],
    }
    path = tmp_path / "meeting.records.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")

    assert load_reusable_records_artifact(
        str(path),
        source_sha256=source_sha256,
    ) == artifact
    assert load_reusable_records_artifact(
        str(path),
        source_sha256=compute_text_sha256("Changed transcript."),
    ) is None
    assert load_timestamp_reusable_records_artifact(str(path)) == artifact


def test_unusable_existing_records_require_force(tmp_path: Path) -> None:
    """
    Confirm malformed cache files cannot silently trigger model extraction.

    :param tmp_path: Pytest temporary directory
    :return: None
    """
    path = tmp_path / "meeting.records.json"
    path.write_text('{"source_sha256": "abc"}', encoding="utf-8")

    with pytest.raises(ConfigError, match="rerun with --force"):
        load_reusable_records_artifact(
            str(path),
            source_sha256="abc",
        )


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
                "timestamp": 372.4,
                "video_id": "AbC_dEf-123",
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
    assert "<script>" not in render_records_markdown(
        {
            "records": [
                {
                    **_record(
                        "discussion",
                        "Budget [link](https://example.invalid)",
                    ),
                    "summary": "Use *literal* formatting.",
                    "evidence": "<script>alert(1)</script>\nNext line",
                    "source_chunks": [1],
                }
            ]
        }
    )
    assert (
        "Source timestamp: [06:12](https://www.youtube.com/"
        "watch?v=AbC_dEf-123&t=372s)"
    ) in markdown


def test_new_records_artifacts_omit_component_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Confirm new artifacts retain source identity without prompt/config hashes.

    :param tmp_path: Pytest temporary directory
    :param monkeypatch: Pytest monkeypatch fixture
    :return: None
    """
    from yatsee.intel.llm import LLMRuntime, TokenUsage
    from yatsee.intel.record import extract_records_from_transcript

    def fake_generate_chunk_outputs(
        **kwargs: object,
    ) -> tuple[list[GeneratedChunk], TokenUsage]:
        generated = GeneratedChunk(
            chunk_id=1,
            chunk_count=1,
            source_text="A motion was made.",
            prompt="Rendered prompt.",
            output=json.dumps(_record("motion", "Approve agenda")),
            input_tokens=10,
            output_tokens=10,
        )
        on_chunk = kwargs.get("on_chunk")
        assert callable(on_chunk)
        on_chunk(generated)
        return [generated], TokenUsage(input_tokens=10, output_tokens=10)

    monkeypatch.setattr(
        "yatsee.intel.record.generate_chunk_outputs",
        fake_generate_chunk_outputs,
    )

    extract_records_from_transcript(
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
    )

    artifact = json.loads(
        Path(_artifact_paths(str(tmp_path), "meeting")["final_path"]).read_text(
            encoding="utf-8"
        )
    )
    assert artifact["source_sha256"] == compute_text_sha256(
        "A motion was made."
    )
    for key in (
        "extraction_fingerprint",
        "classifier_sha256",
        "records_prompt_sha256",
        "density_keywords_sha256",
        "entity_context_sha256",
        "context_sha256",
    ):
        assert key not in artifact


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
    paths = _artifact_paths(str(output_dir), "meeting")
    Path(paths["transcript_dir"]).mkdir(parents=True)
    artifact = {
        "artifact_kind": ARTIFACT_KIND,
        "schema_version": "historical_schema",
        "extraction_version": "records_v1",
        "extraction_fingerprint": "stale-fingerprint",
        "source_transcript": str(transcript_path),
        "source_sha256": compute_text_sha256(transcript_text),
        "meeting_type": "general",
        "job_profile": "civic",
        "prompt_id": "general_records",
        "provider": "ollama",
        "model": "gemma4:e4b",
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


def test_warning_rebuild_writes_complete_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Confirm isolated record rejection does not force a partial meeting artifact.

    :param tmp_path: Pytest temporary directory
    :param monkeypatch: Pytest monkeypatch fixture
    :return: None
    """
    from yatsee.intel.llm import LLMRuntime, TokenUsage
    from yatsee.intel.record import extract_records_from_transcript

    def fake_generate_chunk_outputs(
        **kwargs: object,
    ) -> tuple[list[GeneratedChunk], TokenUsage]:
        generated = GeneratedChunk(
            chunk_id=1,
            chunk_count=1,
            source_text="A motion was made.",
            prompt="Rendered prompt.",
            output="\n".join(
                [
                    json.dumps(_record("motion", "Approve agenda")),
                    json.dumps(
                        {
                            "record_type": "procedural_action",
                            "subject": "Meeting start",
                            "summary": "The meeting started.",
                        }
                    ),
                ]
            ),
            input_tokens=10,
            output_tokens=20,
        )
        on_chunk = kwargs.get("on_chunk")
        assert callable(on_chunk)
        on_chunk(generated)
        return [generated], TokenUsage(input_tokens=10, output_tokens=20)

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
    )

    paths = _artifact_paths(str(tmp_path), "meeting")
    artifact = json.loads(Path(paths["final_path"]).read_text(encoding="utf-8"))

    assert result["status"] == "complete"
    assert result["cache_updated"] is True
    assert result["validation_warning_count"] == 1
    assert result["rejected_record_count"] == 1
    assert result["record_count"] == 1
    assert artifact["status"] == "complete"
    assert artifact["validation_warning_count"] == 1
    assert not Path(paths["partial_path"]).exists()


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
    )

    assert result["status"] == "partial"
    assert result["cache_updated"] is False
    assert Path(paths["final_path"]).read_text(encoding="utf-8") == previous_content
    assert Path(paths["partial_path"]).is_file()
    assert Path(paths["partial_markdown_path"]).is_file()


def test_timestamp_only_is_existing_artifact_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Confirm timestamp-only mode never enters extraction or clears artifacts.

    The existing records artifact intentionally omits current schema, status,
    extraction, and chunk metadata. A second transcript has no records artifact.
    Timestamp-only mode must align the historical artifact, skip the missing one,
    and leave mutable extraction directories untouched.

    :param tmp_path: Pytest temporary directory
    :param monkeypatch: Pytest monkeypatch fixture
    :return: None
    """
    transcript_path = tmp_path / "meeting.txt"
    missing_path = tmp_path / "missing.txt"
    transcript_path.write_text("The motion passed.", encoding="utf-8")
    missing_path.write_text("No records exist yet.", encoding="utf-8")
    output_dir = tmp_path / "records"
    paths = _artifact_paths(str(output_dir), "meeting")
    missing_paths = _artifact_paths(str(output_dir), "missing")
    Path(paths["transcript_dir"]).mkdir(parents=True)

    artifact = {
        "source_transcript": str(transcript_path),
        "source_sha256": compute_text_sha256("Historical transcript text."),
        "meeting_type": "historical_committee",
        "classifier_file": "historical_classifier.toml",
        "prompt_file": "historical_records_prompt.toml",
        "records": [
            {
                **_record("vote", "Motion outcome"),
                "source_chunks": [1],
            }
        ],
    }
    Path(paths["final_path"]).write_text(
        json.dumps(artifact),
        encoding="utf-8",
    )

    sentinels = []
    for directory_key in ("raw_dir", "chunks_dir", "errors_dir"):
        directory = Path(paths[directory_key])
        directory.mkdir()
        sentinel = directory / "sentinel.txt"
        sentinel.write_text(directory_key, encoding="utf-8")
        sentinels.append((sentinel, directory_key))

    monkeypatch.setattr(
        "yatsee.intel.record.resolve_record_paths",
        lambda *args, **kwargs: {
            "global_cfg": {
                "system": {
                    "default_embedding_model": "BAAI/bge-small-en-v1.5",
                }
            },
            "entity_cfg": {},
            "model": "gemma4:e4b",
            "input_dir": str(tmp_path),
            "file_list": [str(transcript_path), str(missing_path)],
            "output_dir": str(output_dir),
            "max_tokens": 1500,
            "num_ctx": 16384,
        },
    )

    def fail_extraction_boundary(*args: object, **kwargs: object) -> None:
        raise AssertionError("timestamp-only mode entered the records extraction path")

    for target in (
        "load_classifier_bundle",
        "load_records_prompt_bundle",
        "load_summary_prompt_bundle",
        "resolve_llm_runtime",
        "create_llm_session",
        "extract_records_from_transcript",
        "_prepare_artifact_directory",
    ):
        monkeypatch.setattr(
            f"yatsee.intel.record.{target}",
            fail_extraction_boundary,
        )

    def fake_enrich(**kwargs: object) -> tuple[dict[str, object], dict[str, object], bool]:
        assert kwargs["force_realign"] is True
        enriched = json.loads(json.dumps(kwargs["artifact"]))
        enriched["records"][0]["timestamp"] = 12.0
        enriched["records"][0]["video_id"] = "AbC_dEf-123"
        return (
            enriched,
            {
                "timestamp_alignment_status": "complete",
                "timestamp_alignment_message": "aligned",
                "timestamp_alignment_path": paths["alignment_path"],
                "timestamp_matched_record_count": 1,
                "timestamp_ambiguous_record_count": 0,
                "timestamp_unmatched_record_count": 0,
            },
            True,
        )

    monkeypatch.setattr(
        "yatsee.intel.record._enrich_records_with_timestamps",
        fake_enrich,
    )

    result = run_records_stage(
        global_config_path="yatsee.toml",
        job_profile="civic",
        chunk_style="density",
        disable_auto_classification=True,
        timestamp_only=True,
    )

    written = json.loads(Path(paths["final_path"]).read_text(encoding="utf-8"))
    assert result["mode"] == "timestamp_only"
    assert result["extracted"] == 0
    assert result["existing_records_artifacts"] == 1
    assert result["aligned"] == 1
    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert result["results"][0]["records_artifact_status"] == "existing"
    assert result["results"][0]["timestamp_alignment_status"] == "complete"
    assert result["results"][1]["records_artifact_status"] == "missing"
    assert result["results"][1]["timestamp_alignment_status"] == "skipped"
    assert written["records"][0]["timestamp"] == 12.0
    assert written["records"][0]["video_id"] == "AbC_dEf-123"
    assert written["source_sha256"] == compute_text_sha256(
        "Historical transcript text."
    )
    assert written["classifier_file"] == "./historical_classifier.toml"
    assert written["prompt_file"] == "./historical_records_prompt.toml"
    assert not Path(written["source_transcript"]).is_absolute()
    assert not Path(missing_paths["transcript_dir"]).exists()

    for sentinel, expected in sentinels:
        assert sentinel.read_text(encoding="utf-8") == expected


def test_align_timestamps_runs_normal_records_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Confirm alignment reuses current records and creates missing records.

    :param tmp_path: Pytest temporary directory
    :param monkeypatch: Pytest monkeypatch fixture
    :return: None
    """
    from contextlib import nullcontext
    from yatsee.intel.llm import LLMRuntime

    existing_path = tmp_path / "existing.txt"
    missing_path = tmp_path / "missing.txt"
    existing_text = "Existing normalized transcript."
    existing_path.write_text(existing_text, encoding="utf-8")
    missing_path.write_text("Missing records transcript.", encoding="utf-8")
    output_dir = tmp_path / "records"
    existing_paths = _artifact_paths(str(output_dir), "existing")
    Path(existing_paths["transcript_dir"]).mkdir(parents=True)
    Path(existing_paths["final_path"]).write_text(
        json.dumps(
            {
                "source_sha256": compute_text_sha256(existing_text),
                "records": [_record("discussion", "Existing record")],
            }
        ),
        encoding="utf-8",
    )

    runtime = LLMRuntime(
        provider_name="ollama",
        provider_target="http://127.0.0.1:11434",
        api_key=None,
        model="gemma4:e4b",
        num_ctx=16384,
    )
    monkeypatch.setattr(
        "yatsee.intel.record.resolve_record_paths",
        lambda *args, **kwargs: {
            "global_cfg": {"system": {}},
            "entity_cfg": {},
            "model": "gemma4:e4b",
            "input_dir": str(tmp_path),
            "file_list": [str(existing_path), str(missing_path)],
            "output_dir": str(output_dir),
            "max_tokens": 1500,
            "num_ctx": 16384,
        },
    )
    monkeypatch.setattr(
        "yatsee.intel.record.load_classifier_bundle",
        lambda *args, **kwargs: {
            "classifier_prompt": "",
            "allowed_meeting_types": ["general"],
            "path": "classifier.toml",
            "fallback": False,
        },
    )
    monkeypatch.setattr(
        "yatsee.intel.record.load_records_prompt_bundle",
        lambda *args, **kwargs: {
            "records_router": {"general": "general_records"},
            "prompts": {"general_records": "Extract {text}"},
            "path": "records_prompt.toml",
        },
    )
    monkeypatch.setattr(
        "yatsee.intel.record.load_summary_prompt_bundle",
        lambda *args, **kwargs: {
            "density_keywords": {"keywords": []},
            "path": "prompts.toml",
        },
    )
    monkeypatch.setattr(
        "yatsee.intel.record.resolve_llm_runtime",
        lambda **kwargs: runtime,
    )
    monkeypatch.setattr(
        "yatsee.intel.record.create_llm_session",
        lambda: nullcontext(object()),
    )

    enriched_bases: list[str] = []

    def fake_enrich(**kwargs: object) -> tuple[dict[str, object], dict[str, object], bool]:
        enriched_bases.append(str(kwargs["base_name"]))
        artifact = json.loads(json.dumps(kwargs["artifact"]))
        artifact["records"][0]["timestamp"] = 12.0
        return (
            artifact,
            {
                "timestamp_alignment_status": "complete",
                "timestamp_alignment_message": "aligned",
                "timestamp_alignment_path": existing_paths["alignment_path"],
                "timestamp_matched_record_count": 1,
                "timestamp_ambiguous_record_count": 0,
                "timestamp_unmatched_record_count": 0,
            },
            True,
        )

    monkeypatch.setattr(
        "yatsee.intel.record._enrich_records_with_timestamps",
        fake_enrich,
    )

    captured: dict[str, object] = {}

    def fake_extract(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "base_name": "missing",
            "meeting_type": "general",
            "prompt_id": "general_records",
            "output_path": str(output_dir / "missing.records.json"),
            "markdown_path": str(output_dir / "missing.records.md"),
            "status": "complete",
            "chunk_count": 1,
            "successful_chunk_count": 1,
            "partial_chunk_count": 0,
            "failed_chunk_count": 0,
            "validation_issue_count": 0,
            "validation_warning_count": 0,
            "normalized_record_count": 0,
            "rejected_record_count": 0,
            "rejected_line_count": 0,
            "record_count": 1,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "pricing": {"enabled": False},
            "cache_updated": True,
            "reused": False,
            "skipped": False,
            "timestamp_alignment_status": "complete",
            "timestamp_alignment_message": "aligned",
            "timestamp_alignment_path": "missing.records.alignment.json",
            "timestamp_matched_record_count": 1,
            "timestamp_ambiguous_record_count": 0,
            "timestamp_unmatched_record_count": 0,
        }

    monkeypatch.setattr(
        "yatsee.intel.record.extract_records_from_transcript",
        fake_extract,
    )

    result = run_records_stage(
        global_config_path="yatsee.toml",
        align_timestamps=True,
        disable_auto_classification=True,
    )

    assert result["mode"] == "run"
    assert result["extracted"] == 1
    assert result["reused"] == 1
    assert result["timestamp_alignment_requested"] is True
    assert enriched_bases == ["existing"]
    assert captured["base_name"] == "missing"
    assert captured["align_timestamps"] is True
    assert result["results"][0]["timestamp_alignment_status"] == "complete"
    assert result["results"][1]["timestamp_alignment_status"] == "complete"


def test_timestamp_only_rejects_extraction_options() -> None:
    """
    Confirm timestamp-only mode cannot be combined with extraction controls.

    :return: None
    """
    with pytest.raises(ConfigError, match="--force"):
        run_records_stage(
            global_config_path="yatsee.toml",
            force=True,
            timestamp_only=True,
        )

    with pytest.raises(ConfigError, match="--align-timestamps"):
        run_records_stage(
            global_config_path="yatsee.toml",
            align_timestamps=True,
            timestamp_only=True,
        )


def test_force_with_timestamp_alignment_rebuilds_then_aligns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Confirm explicit force enters extraction and requests timestamp enrichment.

    :param tmp_path: Pytest temporary directory
    :param monkeypatch: Pytest monkeypatch fixture
    :return: None
    """
    from contextlib import nullcontext
    from yatsee.intel.llm import LLMRuntime

    transcript_path = tmp_path / "meeting.txt"
    transcript_path.write_text("Normalized transcript.", encoding="utf-8")
    output_dir = tmp_path / "records"
    runtime = LLMRuntime(
        provider_name="ollama",
        provider_target="http://127.0.0.1:11434",
        api_key=None,
        model="gemma4:e4b",
        num_ctx=16384,
    )

    monkeypatch.setattr(
        "yatsee.intel.record.resolve_record_paths",
        lambda *args, **kwargs: {
            "global_cfg": {"system": {}},
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
        lambda *args, **kwargs: {
            "classifier_prompt": "",
            "allowed_meeting_types": ["general"],
            "path": "classifier.toml",
            "fallback": False,
        },
    )
    monkeypatch.setattr(
        "yatsee.intel.record.load_records_prompt_bundle",
        lambda *args, **kwargs: {
            "records_router": {"general": "general_records"},
            "prompts": {"general_records": "Extract {text}"},
            "path": "records_prompt.toml",
        },
    )
    monkeypatch.setattr(
        "yatsee.intel.record.load_summary_prompt_bundle",
        lambda *args, **kwargs: {
            "density_keywords": {"keywords": []},
            "path": "prompts.toml",
        },
    )
    monkeypatch.setattr(
        "yatsee.intel.record.resolve_llm_runtime",
        lambda **kwargs: runtime,
    )
    monkeypatch.setattr(
        "yatsee.intel.record.create_llm_session",
        lambda: nullcontext(object()),
    )

    captured: dict[str, object] = {}

    def fake_extract(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "base_name": "meeting",
            "meeting_type": "general",
            "prompt_id": "general_records",
            "output_path": str(output_dir / "meeting.records.json"),
            "markdown_path": str(output_dir / "meeting.records.md"),
            "status": "complete",
            "chunk_count": 1,
            "successful_chunk_count": 1,
            "partial_chunk_count": 0,
            "failed_chunk_count": 0,
            "validation_issue_count": 0,
            "validation_warning_count": 0,
            "normalized_record_count": 0,
            "rejected_record_count": 0,
            "rejected_line_count": 0,
            "record_count": 1,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "pricing": {"enabled": False},
            "cache_updated": True,
            "reused": False,
            "skipped": False,
            "timestamp_alignment_status": "complete",
            "timestamp_alignment_message": "aligned",
            "timestamp_alignment_path": "meeting.records.alignment.json",
            "timestamp_matched_record_count": 1,
            "timestamp_ambiguous_record_count": 0,
            "timestamp_unmatched_record_count": 0,
        }

    monkeypatch.setattr(
        "yatsee.intel.record.extract_records_from_transcript",
        fake_extract,
    )

    result = run_records_stage(
        global_config_path="yatsee.toml",
        force=True,
        align_timestamps=True,
        disable_auto_classification=True,
    )

    assert result["mode"] == "run"
    assert result["extracted"] == 1
    assert result["timestamp_alignment_requested"] is True
    assert captured["align_timestamps"] is True


def test_missing_segments_skips_timestamp_enrichment(tmp_path: Path) -> None:
    """
    Confirm missing segment artifacts leave valid records untouched.

    :param tmp_path: Pytest temporary directory
    :return: None
    """
    from yatsee.intel.record import _enrich_records_with_timestamps

    artifact = {
        "record_count": 1,
        "records": [
            {
                **_record("vote", "Motion outcome"),
                "source_chunks": [1],
            }
        ],
    }
    paths = _artifact_paths(str(tmp_path / "records"), "meeting")

    enriched, result, changed = _enrich_records_with_timestamps(
        artifact=artifact,
        paths=paths,
        transcript_path=str(tmp_path / "normalized" / "meeting.txt"),
        base_name="meeting",
        entity_cfg={},
        embedding_model="BAAI/bge-small-en-v1.5",
    )

    assert enriched == artifact
    assert changed is False
    assert result["timestamp_alignment_status"] == "skipped"
    assert "timestamp" not in enriched["records"][0]


def test_reused_alignment_sidecar_paths_are_migrated_without_realigning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Confirm current alignment reports replace absolute inputs without ML work.

    :param tmp_path: Pytest temporary directory
    :param monkeypatch: Pytest monkeypatch fixture
    :return: None
    """
    from yatsee.intel.record import _enrich_records_with_timestamps

    project_root = tmp_path / "yatsee"
    transcript_path = project_root / "data" / "entity" / "normalized" / "meeting.txt"
    segments_path = transcript_path.with_name("meeting.segments.jsonl")
    output_dir = project_root / "data" / "entity" / "records"
    paths = _artifact_paths(str(output_dir), "meeting")
    transcript_path.parent.mkdir(parents=True)
    Path(paths["transcript_dir"]).mkdir(parents=True)
    transcript_path.write_text("The motion passed.", encoding="utf-8")
    segments_path.write_text("{}\n", encoding="utf-8")
    Path(paths["alignment_path"]).write_text(
        json.dumps(
            {
                "alignment_version": ALIGNMENT_VERSION,
                "inputs": {
                    "segments": str(segments_path.resolve()),
                    "records": str(Path(paths["final_path"]).resolve()),
                },
            }
        ),
        encoding="utf-8",
    )
    artifact = {
        "timestamp_alignment": {
            "version": ALIGNMENT_VERSION,
            "matched_record_count": 1,
            "ambiguous_record_count": 0,
            "unmatched_record_count": 0,
        },
        "record_count": 1,
        "records": [
            {
                **_record("vote", "Motion outcome"),
                "source_chunks": [1],
                "timestamp": 12.0,
            }
        ],
    }

    monkeypatch.setattr(
        "yatsee.intel.timestamp.alignment_is_current",
        lambda **kwargs: True,
    )

    def fail_alignment(**kwargs: object) -> None:
        raise AssertionError("Current alignment should not be recomputed")

    monkeypatch.setattr(
        "yatsee.intel.timestamp.align_records_artifact",
        fail_alignment,
    )

    enriched, result, changed = _enrich_records_with_timestamps(
        artifact=artifact,
        paths=paths,
        transcript_path=str(transcript_path),
        base_name="meeting",
        entity_cfg={},
        embedding_model="BAAI/bge-small-en-v1.5",
        project_root=str(project_root),
    )

    report = json.loads(Path(paths["alignment_path"]).read_text(encoding="utf-8"))
    assert enriched == artifact
    assert changed is False
    assert result["timestamp_alignment_status"] == "reused"
    assert report["inputs"] == {
        "segments": "./data/entity/normalized/meeting.segments.jsonl",
        "records": "./data/entity/records/meeting/meeting.records.json",
    }


def test_force_realign_bypasses_compatible_alignment_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Confirm forced timestamp-only work recalculates a current alignment.

    :param tmp_path: Pytest temporary directory
    :param monkeypatch: Pytest monkeypatch fixture
    :return: None
    """
    from types import SimpleNamespace
    from yatsee.intel.record import _enrich_records_with_timestamps

    transcript_path = tmp_path / "normalized" / "meeting.txt"
    segments_path = transcript_path.with_name("meeting.segments.jsonl")
    paths = _artifact_paths(str(tmp_path / "records"), "meeting")
    transcript_path.parent.mkdir(parents=True)
    Path(paths["transcript_dir"]).mkdir(parents=True)
    transcript_path.write_text("The motion passed.", encoding="utf-8")
    segments_path.write_text("{}\n", encoding="utf-8")
    Path(paths["alignment_path"]).write_text("{}\n", encoding="utf-8")

    artifact = {
        "record_count": 1,
        "records": [
            {
                **_record("vote", "Motion outcome"),
                "source_chunks": [1],
                "timestamp": 10.0,
            }
        ],
    }
    recalculated = json.loads(json.dumps(artifact))
    recalculated["records"][0]["timestamp"] = 22.0

    def fail_cache_check(**kwargs: object) -> bool:
        raise AssertionError("forced realignment consulted the alignment cache")

    monkeypatch.setattr(
        "yatsee.intel.timestamp.alignment_is_current",
        fail_cache_check,
    )
    monkeypatch.setattr(
        "yatsee.intel.timestamp.align_records_artifact",
        lambda **kwargs: SimpleNamespace(
            artifact=recalculated,
            report={"forced": True},
            metadata={
                "matched_record_count": 1,
                "ambiguous_record_count": 0,
                "unmatched_record_count": 0,
            },
        ),
    )

    enriched, result, changed = _enrich_records_with_timestamps(
        artifact=artifact,
        paths=paths,
        transcript_path=str(transcript_path),
        base_name="meeting",
        entity_cfg={},
        embedding_model="BAAI/bge-small-en-v1.5",
        project_root=str(tmp_path),
        force_realign=True,
    )

    assert changed is True
    assert enriched["records"][0]["timestamp"] == 22.0
    assert result["timestamp_alignment_status"] == "complete"
    assert json.loads(
        Path(paths["alignment_path"]).read_text(encoding="utf-8")
    ) == {"forced": True}
