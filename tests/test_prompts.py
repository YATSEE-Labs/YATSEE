"""
Tests for filesystem-backed prompt profile loading mechanics.

The fixtures intentionally create a temporary ``prompts/civic`` profile so the
loader contract is tested without coupling the suite to repository prompt text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yatsee.core.errors import ConfigError
from yatsee.intel.prompts import (
    discover_summary_prompt_profiles,
    load_classifier_bundle,
    load_records_prompt_bundle,
    load_summary_prompt_bundle,
)


def _write_civic_profile(
    root: Path,
    *,
    summary_text: str = "Summarize {text} for {context}.",
    records_text: str = "Extract records from {text} for {context}.",
) -> Path:
    """
    Write a minimal synthetic civic profile under one prompt root.

    :param root: Prompt root directory
    :param summary_text: Summary prompt text
    :param records_text: Records prompt text
    :return: Created civic profile directory
    """
    profile_dir = root / "civic"
    profile_dir.mkdir(parents=True, exist_ok=True)

    (profile_dir / "classifier.toml").write_text(
        '[classifier_types]\nallowed = ["general"]\n',
        encoding="utf-8",
    )
    (profile_dir / "prompts.toml").write_text(
        "\n".join(
            [
                "[prompt_router.general]",
                'first = "summary"',
                'multi = "summary"',
                'final = "summary"',
                "",
                "[prompts.summary]",
                f'text = "{summary_text}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (profile_dir / "records_prompt.toml").write_text(
        "\n".join(
            [
                "[records_router]",
                'general = "records"',
                "",
                "[prompts.records]",
                f'text = "{records_text}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return profile_dir


@pytest.fixture
def civic_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """
    Create an isolated project containing only a synthetic civic profile.

    :param tmp_path: Pytest temporary directory
    :param monkeypatch: Pytest monkeypatch fixture
    :return: Temporary project root
    """
    _write_civic_profile(tmp_path / "prompts")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_civic_profile_loaders_accept_synthetic_filesystem_contract(
    civic_project: Path,
) -> None:
    """
    Load classifier, summary, and Records contracts from temporary files.

    :param civic_project: Isolated project containing the civic fixture
    :return: None
    """
    classifier = load_classifier_bundle(
        {},
        "civic",
        require_prompt_file=True,
    )
    summary = load_summary_prompt_bundle(
        {},
        "civic",
        require_prompt_file=True,
    )
    records = load_records_prompt_bundle({}, "civic")

    assert classifier["allowed_meeting_types"] == ["general"]
    assert classifier["classifier_prompt"] is None
    assert classifier["fallback"] is False
    assert set(summary["prompt_router"]) == {"general"}
    assert set(summary["prompts"]) == {"summary"}
    assert set(records["records_router"]) == {"general"}
    assert set(records["prompts"]) == {"records"}


def test_entity_local_civic_summary_file_overrides_project_file(
    civic_project: Path,
) -> None:
    """
    Resolve an entity-local civic summary bundle before the project bundle.

    :param civic_project: Isolated project containing the project-level profile
    :return: None
    """
    _write_civic_profile(
        civic_project / "prompts",
        summary_text="Project summary for {text} and {context}.",
    )
    entity_dir = civic_project / "data" / "test_entity"
    _write_civic_profile(
        entity_dir / "prompts",
        summary_text="Entity summary for {text} and {context}.",
    )

    bundle = load_summary_prompt_bundle(
        {"data_path": "./data/test_entity"},
        "civic",
        require_prompt_file=True,
    )

    assert bundle["prompts"]["summary"].startswith("Entity summary")
    assert Path(bundle["path"]) == (
        Path("data") / "test_entity" / "prompts" / "civic" / "prompts.toml"
    )


def test_civic_profile_discovery_uses_summary_file_presence(
    civic_project: Path,
) -> None:
    """
    Discover the synthetic civic profile from its summary prompt file.

    :param civic_project: Isolated project containing the civic fixture
    :return: None
    """
    assert discover_summary_prompt_profiles({}) == ["civic"]


def test_profile_names_reject_path_traversal() -> None:
    """
    Reject profile names that could escape the prompt root.
    """
    with pytest.raises(ConfigError):
        load_summary_prompt_bundle({}, "../outside")
