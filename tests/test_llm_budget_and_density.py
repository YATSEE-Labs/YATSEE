"""
Validate density chunk boundaries and Ollama output budgeting.

These tests protect the original local summarizer behavior while keeping the
shared chunker sentence aligned.
"""

from __future__ import annotations

import json

from yatsee.intel.chunking import prepare_text_chunk_density
from yatsee.intel.classifier import classify_transcript
from yatsee.providers.ollama import generate_text
from yatsee.providers.tokenization import estimate_provider_tokens


class _FakeResponse:
    """
    Provide the minimal streaming response surface used by the Ollama provider.
    """

    def raise_for_status(self) -> None:
        """
        Simulate a successful HTTP response.

        :return: None
        """

    def iter_lines(self):
        """
        Yield one completed Ollama response frame.

        :return: Iterable response lines
        """
        yield json.dumps({"response": "ok", "done": True}).encode("utf-8")


class _FakeSession:
    """
    Capture the provider payload without making a network request.
    """

    def __init__(self) -> None:
        """
        Initialize captured request state.
        """
        self.payload = None

    def post(self, url, *, json, **kwargs):
        """
        Capture one Ollama request and return a successful response.

        :param url: Requested endpoint URL
        :param json: Request payload
        :param kwargs: Remaining request options
        :return: Fake streaming response
        """
        self.payload = json
        return _FakeResponse()


def test_ollama_uses_remaining_context_without_explicit_output_cap() -> None:
    """
    Confirm uncapped generation receives the full context remaining after prompt.

    :return: None
    """
    prompt = "Extract records from this transcript segment."
    num_ctx = 16384
    session = _FakeSession()

    generate_text(
        session=session,
        base_url="http://127.0.0.1:11434",
        model="gemma4:e4b",
        prompt=prompt,
        num_ctx=num_ctx,
    )

    expected = num_ctx - estimate_provider_tokens(
        provider_name="ollama",
        model="gemma4:e4b",
        text=prompt,
    ) - 1
    assert session.payload["options"]["num_predict"] == expected
    assert expected > 2048


def test_ollama_preserves_narrow_explicit_output_cap() -> None:
    """
    Confirm explicit limits such as classifier caps remain effective.

    :return: None
    """
    session = _FakeSession()

    generate_text(
        session=session,
        base_url="http://127.0.0.1:11434",
        model="gemma4:e4b",
        prompt="Classify this meeting.",
        num_ctx=8192,
        max_output_tokens=64,
    )

    assert session.payload["options"]["num_predict"] == 64


def test_density_chunker_allows_only_sentence_boundary_overage() -> None:
    """
    Confirm density chunks may cross the target only to finish a sentence.

    :return: None
    """
    sentences = [
        "Alpha sentence has enough words to consume part of the target.",
        "Beta sentence completes the thought without being split awkwardly.",
        "Gamma sentence starts the following chunk cleanly.",
    ]
    text = " ".join(sentences)

    chunks = prepare_text_chunk_density(
        text=text,
        max_tokens=20,
        overlap_tokens=0,
        density_keywords=set(),
    )

    assert chunks[0] == " ".join(sentences[:2])
    assert chunks[1] == sentences[2]


def test_density_chunker_does_not_emit_overlap_only_final_chunk() -> None:
    """
    Confirm a final flush does not create a duplicate overlap-only artifact.

    :return: None
    """
    text = (
        "First sentence contains several ordinary words for testing. "
        "Second sentence also contains several ordinary words for testing."
    )

    chunks = prepare_text_chunk_density(
        text=text,
        max_tokens=20,
        overlap_tokens=3,
        density_keywords=set(),
    )

    assert len(chunks) == 1
    assert chunks[0] == text


def test_single_classifier_label_does_not_call_provider(monkeypatch) -> None:
    """
    Confirm one-label profiles bypass probabilistic classification entirely.

    :param monkeypatch: Pytest monkeypatch fixture
    :return: None
    """
    def fail_if_called(*args, **kwargs):
        raise AssertionError("classifier provider should not be called")

    monkeypatch.setattr(
        "yatsee.intel.classifier.llm_generate_with_runtime",
        fail_if_called,
    )

    result = classify_transcript(
        session=None,
        runtime=None,
        prompt="",
        allowed_labels={"general"},
    )

    assert result == "general"
