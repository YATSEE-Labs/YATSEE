"""
Meeting classification helpers for YATSEE intelligence jobs.
"""

from __future__ import annotations

from collections.abc import Collection

import requests

from yatsee.intel.llm import LLMRuntime, llm_generate_with_runtime


def resolve_single_classifier_label(
    allowed_labels: Collection[str],
) -> str | None:
    """
    Return the configured label when classification has only one possible result.

    Profiles with one allowed meeting type do not need probabilistic routing.
    Resolving that case locally avoids an unnecessary provider call and prevents
    classification token usage from being reported for work that was not done.

    :param allowed_labels: Configured classification labels
    :return: Normalized label when exactly one exists, otherwise ``None``
    """
    normalized_labels = {
        str(label).strip().lower()
        for label in allowed_labels
        if str(label).strip()
    }
    if len(normalized_labels) == 1:
        return next(iter(normalized_labels))
    return None


def classify_transcript(
    *,
    session: requests.Session,
    runtime: LLMRuntime,
    prompt: str,
    allowed_labels: set[str],
) -> str:
    """
    Classify a transcript into one of the configured meeting labels.

    A profile with exactly one allowed label is resolved deterministically and
    never sent to the model. Otherwise, the model response is normalized to
    lowercase and matched against the allowed label set using simple substring
    checks. If classification is unavailable or no allowed label is found, the
    function returns ``general``.

    :param session: Shared requests session
    :param runtime: Resolved provider runtime
    :param prompt: Classification prompt text
    :param allowed_labels: Set of accepted classification labels
    :return: Matched lowercase label or ``general`` if classification is unavailable or unmatched
    :raises ProviderError: If provider execution fails
    """
    single_label = resolve_single_classifier_label(allowed_labels)
    if single_label:
        return single_label

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
