"""
Meeting classification helpers for YATSEE intelligence jobs.
"""

from __future__ import annotations

import requests

from yatsee.intel.llm import LLMRuntime, llm_generate_with_runtime


def classify_transcript(
    *,
    session: requests.Session,
    runtime: LLMRuntime,
    prompt: str,
    allowed_labels: set[str],
) -> str:
    """
    Classify a transcript into one of the configured meeting labels.

    The model response is normalized to lowercase and matched against the
    allowed label set using simple substring checks. If classification is not
    usable or no allowed label is found, the function returns ``general``.

    :param session: Shared requests session
    :param runtime: Resolved provider runtime
    :param prompt: Classification prompt text
    :param allowed_labels: Set of accepted classification labels
    :return: Matched lowercase label or ``general`` if classification is unavailable or unmatched
    :raises ProviderError: If provider execution fails
    """
    if not allowed_labels or not prompt:
        return "general"

    raw_response = llm_generate_with_runtime(
        session=session,
        runtime=runtime,
        prompt=prompt,
        max_output_tokens=64,
    ).strip().lower()

    for allowed_label in sorted(allowed_labels, key=len, reverse=True):
        normalized_label = allowed_label.lower()
        if normalized_label in raw_response:
            return normalized_label

    return "general"
