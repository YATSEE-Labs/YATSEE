"""
Validate portable project path serialization.
"""

from __future__ import annotations

from pathlib import Path

from yatsee.core.paths import serialize_project_path


def test_serialize_project_path_for_project_file(tmp_path: Path) -> None:
    """
    Confirm a path inside the project is rendered from the project root.

    :param tmp_path: Pytest temporary directory
    :return: None
    """
    project_root = tmp_path / "yatsee"
    artifact = project_root / "data" / "entity" / "meeting.records.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")

    assert serialize_project_path(artifact, project_root) == (
        "./data/entity/meeting.records.json"
    )


def test_serialize_project_path_for_external_file(tmp_path: Path) -> None:
    """
    Confirm an external path remains relative without leaking an absolute path.

    :param tmp_path: Pytest temporary directory
    :return: None
    """
    project_root = tmp_path / "yatsee"
    external = tmp_path / "incoming" / "meeting.txt"
    project_root.mkdir()
    external.parent.mkdir()
    external.write_text("meeting", encoding="utf-8")

    assert serialize_project_path(external, project_root) == (
        "../incoming/meeting.txt"
    )
