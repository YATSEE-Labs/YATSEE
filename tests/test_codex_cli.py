"""
Tests for the controlled Codex CLI provider boundary.
"""

from __future__ import annotations

import subprocess
from unittest.mock import Mock, patch

from yatsee.providers.codex_cli import generate_text


def test_codex_cli_sends_prompt_through_stdin() -> None:
    """
    Keep transcript/prompt text out of process arguments.
    """
    prompt = "Sensitive transcript text " * 50
    completed = subprocess.CompletedProcess(
        args=["codex"],
        returncode=0,
        stdout="Generated response",
        stderr="",
    )

    with patch("yatsee.providers.codex_cli.subprocess.run", return_value=completed) as run:
        result = generate_text(
            session=Mock(),
            base_url="codex",
            model="test-model",
            prompt=prompt,
        )

    command = run.call_args.args[0]
    assert command[-1] == "-"
    assert prompt not in command
    assert run.call_args.kwargs["input"] == prompt
    assert result == "Generated response"
