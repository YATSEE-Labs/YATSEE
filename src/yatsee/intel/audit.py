"""
Deterministic quality auditing for YATSEE records artifacts.

This module inspects existing records output without invoking an LLM or
modifying source artifacts. It converts structural failures, validation loss,
alignment weakness, likely adjacent-chunk duplicates, and prompt-contract
violations into a ranked corpus review queue.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Sequence

from yatsee.core.errors import ConfigError, ValidationError
from yatsee.core.io import ensure_directory, read_text, write_text_atomic
from yatsee.core.paths import (
    resolve_contained_path,
    sanitize_path_component,
    serialize_project_path,
)
from yatsee.intel.schema import ALLOWED_RECORD_TYPES
from yatsee.intel.resolve import resolve_audit_paths, resolve_record_paths

AUDIT_ARTIFACT_KIND = "records_audit"
AUDIT_SCHEMA_VERSION = "records_audit_v2"
AUDIT_PROFILE_BALANCED = "balanced"
AUDIT_PROFILE_STRICT = "strict"
AUDIT_JSON_NAME = "report.json"
AUDIT_CSV_NAME = "report.csv"
AUDIT_MARKDOWN_NAME = "report.md"
REBUILD_ARTIFACT_KIND = "records_rebuild_plan"
REBUILD_SCHEMA_VERSION = "records_rebuild_v1"
REBUILD_MANIFEST_NAME = "rebuild.json"
REBUILD_MODE_PLAN = "plan"
REBUILD_MODE_AUTO = "auto"
REBUILD_MIN_REJECTED_ITEMS = 3
REBUILD_MIN_REJECTION_RATE = 0.05

# Rebuild eligibility is intentionally narrower than finding severity. These
# codes describe structural artifact loss that another extraction attempt can
# plausibly repair. Heuristic quality findings never enter this policy.
_REBUILD_DIRECT_FINDING_CODES = {
    "failed_chunks",
    "incomplete_records_status",
    "invalid_required_field",
    "invalid_source_chunks",
    "missing_records_artifact",
    "missing_records_list",
    "non_object_record",
    "partial_chunks",
    "partial_records_artifact",
    "record_count_mismatch",
    "unreadable_records_artifact",
    "unsupported_record_type",
    "zero_records_from_nonempty_meeting",
}

SEVERITY_WEIGHTS = {
    "critical": 25,
    "high": 10,
    "medium": 4,
    "low": 1,
}


@dataclass(frozen=True)
class AuditProfile:
    """
    Define deterministic audit sensitivity without duplicating detector logic.

    Balanced mode is calibrated for useful best-effort records from small local
    models. Strict mode raises semantic expectations for higher-tier extraction
    runs while preserving the same structural hard-failure checks.

    :param name: Stable report and CLI profile name
    :param quantity_mode: material or expanded summary quantity checking
    :param duplicate_mode: conservative or expanded duplicate thresholds
    :param unknown_value_severity: Severity for explicit unknown optional values
    :param short_evidence_severity: Baseline severity for unusually short evidence
    :param filename_mismatch_severity: Severity for explicit filename routing mismatch
    :param weak_alignment_threshold: Rate below which alignment is weak
    :param low_alignment_threshold: Rate below which alignment is high risk
    :param context_identifier_severity: Severity for missing subject identifiers
    :param enable_detail_gap_check: Whether to flag detailed summaries with weak evidence
    """

    name: str
    quantity_mode: str
    duplicate_mode: str
    unknown_value_severity: str
    short_evidence_severity: str
    filename_mismatch_severity: str
    weak_alignment_threshold: float
    low_alignment_threshold: float
    context_identifier_severity: str
    enable_detail_gap_check: bool


AUDIT_PROFILES = {
    AUDIT_PROFILE_BALANCED: AuditProfile(
        name=AUDIT_PROFILE_BALANCED,
        quantity_mode="material",
        duplicate_mode="conservative",
        unknown_value_severity="low",
        short_evidence_severity="low",
        filename_mismatch_severity="medium",
        weak_alignment_threshold=0.95,
        low_alignment_threshold=0.90,
        context_identifier_severity="medium",
        enable_detail_gap_check=False,
    ),
    AUDIT_PROFILE_STRICT: AuditProfile(
        name=AUDIT_PROFILE_STRICT,
        quantity_mode="expanded",
        duplicate_mode="expanded",
        unknown_value_severity="medium",
        short_evidence_severity="low",
        filename_mismatch_severity="high",
        weak_alignment_threshold=0.98,
        low_alignment_threshold=0.95,
        context_identifier_severity="high",
        enable_detail_gap_check=True,
    ),
}

_REQUIRED_RECORD_FIELDS = ("record_type", "subject", "summary", "evidence")
_NUMBER_RE = re.compile(
    r"(?<![\w.])(?P<currency>\$)?(?P<number>-?\d[\d,]*(?:\.\d+)?)"
    r"(?P<percent>%?)(?:\s*(?P<scale>hundred|thousand|million|billion|grand))?",
    re.IGNORECASE,
)
_TIME_RE = re.compile(
    r"\b(?P<hour>\d{1,2})[:.](?P<minute>[0-5]\d)\s*"
    r"(?:a\.?m\.?|p\.?m\.?)?\b(?!\s*%)",
    re.IGNORECASE,
)
_COMPACT_TIME_RE = re.compile(
    r"(?:\bat\s+|\b)(?P<time>\d{3,4})\s*(?:a\.?m\.?|p\.?m\.?)\b",
    re.IGNORECASE,
)
_AT_COMPACT_TIME_RE = re.compile(
    r"\bat\s+(?P<time>\d{3,4})(?=\s+(?:a\.?m\.?|p\.?m\.?|at\b|"
    r"in the (?:morning|afternoon|evening)\b|on\b))",
    re.IGNORECASE,
)
_INTERRUPTED_SCALE_RE = re.compile(
    r"(?<![\w.])(?P<number>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s*,?\s*i\s+(?:think|believe)\s*,?\s*)"
    r"(?P<scale>hundred|thousand|million|billion|grand)\b",
    re.IGNORECASE,
)
_DOLLARS_CENTS_RE = re.compile(
    r"(?<![\w.])(?P<dollars>\d[\d,]*)"
    r"(?:\s+dollars?)?\s+and\s+(?P<cents>\d{1,2})\s+cents?\b",
    re.IGNORECASE,
)
_RANGE_SCALE_RE = re.compile(
    r"(?<![\w.])(?P<left>\d+(?:\.\d+)?)\s+(?:to|through|-)\s+"
    r"(?P<right>\d[\d,]*)(?:\s*(?P<scale>thousand|million|billion|grand))?\b",
    re.IGNORECASE,
)
_WORD_NUMBER_VALUES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "both": 2,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_NUMBER_SCALES = {
    "hundred": 100,
    "thousand": 1_000,
    "million": 1_000_000,
    "billion": 1_000_000_000,
    "grand": 1_000,
}
_NUMBER_WORDS = set(_WORD_NUMBER_VALUES) | set(_NUMBER_SCALES) | {"and", "a", "half"}
_NUMBER_WORD_RE = re.compile(
    r"\b(?:" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"
    r"(?:[ -]+(?:" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r"))*\b",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(
    r"\b(?P<value>\d+|" + "|".join(_WORD_NUMBER_VALUES) + r")\s+"
    r"(?P<unit>days?|weeks?|months?|years?)\s+remaining\b",
    re.IGNORECASE,
)
_ANOTHER_DURATION_RE = re.compile(
    r"\banother\s+(?P<value>\d+|" + "|".join(_WORD_NUMBER_VALUES) + r")\s+"
    r"(?P<unit>days?|weeks?|months?|years?)\b",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-z0-9]+")
_EMPTY_PUBLIC_COMMENT_RE = re.compile(
    r"\b(?:none signed up|no one signed up|nobody in the audience|"
    r"no public comments?|there were none)\b",
    re.IGNORECASE,
)
_MOTION_REQUEST_RE = re.compile(
    r"\b(?:do i have|is there|looking for|need|ask for|entertain)\s+(?:a\s+)?motion\b",
    re.IGNORECASE,
)
_EXPLICIT_MOTION_RE = re.compile(
    r"\b(?:motion by|moved by|so moved|motion made|i move|i make (?:a )?motion|"
    r"by\s+(?:mr|mrs|ms|miss|alderman|trustee|supervisor|chair(?:man|woman)?|commissioner)\.?)\b",
    re.IGNORECASE,
)
_VOTE_OUTCOME_RE = re.compile(
    r"\b(?:pass(?:es|ed)?|approv(?:e|es|ed)|adopt(?:s|ed)?|"
    r"carr(?:y|ies|ied)|fail(?:s|ed)?|defeat(?:s|ed)?|den(?:y|ies|ied)|"
    r"tabl(?:e|es|ed)|withdrawn|postponed|deferred|referred|"
    r"all in favor|ayes?|nays?|roll call|voice vote|unanimous|"
    r"meeting (?:is )?adjourned|\d+\s*(?:-|to)\s*\d+|"
    r"\d+\s+yes(?:\s+votes?)?)\b",
    re.IGNORECASE,
)
_STANDALONE_SECOND_RE = re.compile(
    r"^(?:second(?:ed)?(?:\s+by)?|by\s+(?:mr|mrs|ms|miss|alderman|trustee|"
    r"supervisor|chair(?:man|woman)?|commissioner)\.?)\b",
    re.IGNORECASE,
)
_ACTUAL_MOTION_RE = re.compile(
    r"\b(?:motion by|moved by|so moved|motion made|i move|"
    r"i make (?:a )?motion|make (?:a )?motion to)\b",
    re.IGNORECASE,
)
_SECONDING_CONTEXT_RE = re.compile(
    r"\b(?:second(?:ed|ing)(?:\s+by)?|second\s+by|a second was offered|"
    r"second for|motion seconded|is there a second)\b",
    re.IGNORECASE,
)
_MATERIAL_IDENTIFIER_RE = re.compile(
    r"\b(?:fy|fiscal year|item|section|chapter|article|ordinance|resolution|"
    r"route|highway|fund|district|hd|dhg|pin|number|no\.?)\s*#?\s*"
    r"(?P<identifier>[a-z]*[-.]?\d[a-z0-9.-]*)",
    re.IGNORECASE,
)
_MATERIAL_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_MATERIAL_MONTH_DATE_RE = re.compile(
    r"\b(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)
_MATERIAL_MEASUREMENT_RE = re.compile(
    r"(?<![\w.])(?P<number>-?\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>acres?|feet|foot|miles?|hours?|days?|weeks?|months?|years?|"
    r"units?|steps?|percent|%)\b",
    re.IGNORECASE,
)
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "were",
    "with",
}
_GENERIC_IDENTITY_TOKENS = _STOPWORDS | {
    "action",
    "agenda",
    "amendment",
    "approval",
    "approve",
    "approved",
    "board",
    "committee",
    "consent",
    "county",
    "discussion",
    "final",
    "item",
    "meeting",
    "motion",
    "national",
    "presentation",
    "proclamation",
    "record",
    "report",
    "resolution",
    "township",
    "vote",
}
_OFFICIAL_ROLE_TERMS = {
    "administrator",
    "alderman",
    "assessor",
    "attorney",
    "chair",
    "chairman",
    "chairwoman",
    "clerk",
    "commissioner",
    "director",
    "manager",
    "mayor",
    "president",
    "superintendent",
    "supervisor",
    "treasurer",
    "trustee",
}
_SUBSTANTIVE_CONTEXT_TYPES = {
    "contract",
    "financial",
    "metric",
    "ordinance",
    "project",
    "resolution",
}

_FILENAME_MEETING_TYPE_ALIASES = (
    ("finance_committee_of_the_whole", "committee_of_the_whole"),
    ("planning_and_development_committee", "planning_development_committee"),
    ("public_property_and_safety_committee", "public_property_safety_committee"),
    ("annual_township_meeting", "annual_township_meeting"),
    ("public_property_safety_committee", "public_property_safety_committee"),
    ("planning_development_committee", "planning_development_committee"),
    ("administration_committee", "administration_committee"),
    ("nursing_center_committee", "nursing_center_committee"),
    ("court_services_committee", "court_services_committee"),
    ("committee_of_the_whole", "committee_of_the_whole"),
    ("public_works_committee", "public_works_committee"),
    ("finance_committee", "finance_committee"),
    ("zoning_committee", "zoning_committee"),
    ("township_board", "township_board"),
    ("county_board", "county_board"),
    ("city_council", "city_council"),
)


def _resolve_audit_profile(value: str) -> AuditProfile:
    """
    Resolve one explicit audit sensitivity profile.

    Profile selection is never inferred from model names because parameter
    counts, quantization, providers, and fine-tunes are not reliably encoded in
    a model tag.

    :param value: Requested profile name
    :return: Immutable audit profile configuration
    :raises ValidationError: If the profile is unsupported
    """
    normalized = str(value or AUDIT_PROFILE_BALANCED).strip().lower()
    profile = AUDIT_PROFILES.get(normalized)
    if profile is None:
        supported = ", ".join(sorted(AUDIT_PROFILES))
        raise ValidationError(
            f"Unsupported audit profile '{value}'. Supported profiles: {supported}"
        )
    return profile


def _project_root(global_config_path: str) -> str:
    """
    Resolve the project root from the active global configuration path.

    :param global_config_path: Path to yatsee.toml
    :return: Absolute project root directory
    """
    return os.path.dirname(os.path.abspath(global_config_path)) or os.getcwd()


def _normalize_text(value: Any) -> str:
    """
    Normalize untrusted artifact text for deterministic comparisons.

    :param value: Arbitrary artifact value
    :return: Lowercase alphanumeric comparison text
    """
    return " ".join(_WORD_RE.findall(str(value or "").lower()))


def _comparison_tokens(value: Any) -> set[str]:
    """
    Build a compact token set for fuzzy duplicate comparisons.

    :param value: Text-like artifact value
    :return: Significant lowercase tokens
    """
    return {
        token
        for token in _WORD_RE.findall(str(value or "").lower())
        if len(token) > 2 and token not in _STOPWORDS
    }


def _similarity(left: Any, right: Any) -> float:
    """
    Calculate deterministic sequence similarity between two values.

    :param left: First text-like value
    :param right: Second text-like value
    :return: Similarity ratio between zero and one
    """
    left_text = _normalize_text(left)
    right_text = _normalize_text(right)
    if not left_text or not right_text:
        return 0.0
    return SequenceMatcher(None, left_text, right_text).ratio()


def _jaccard_similarity(left: Any, right: Any) -> float:
    """
    Calculate token-set overlap for two values.

    :param left: First text-like value
    :param right: Second text-like value
    :return: Jaccard similarity between zero and one
    """
    left_tokens = _comparison_tokens(left)
    right_tokens = _comparison_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _record_chunks(record: Dict[str, Any]) -> List[int]:
    """
    Return valid source chunk identifiers from one record.

    :param record: Records artifact entry
    :return: Sorted unique positive chunk identifiers
    """
    raw_chunks = record.get("source_chunks")
    if not isinstance(raw_chunks, list):
        return []
    return sorted(
        {
            chunk
            for chunk in raw_chunks
            if isinstance(chunk, int) and not isinstance(chunk, bool) and chunk > 0
        }
    )


def _chunks_are_adjacent(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    """
    Determine whether two records came from the same or adjacent chunks.

    :param left: First records artifact entry
    :param right: Second records artifact entry
    :return: True when any source chunks differ by at most one
    """
    left_chunks = _record_chunks(left)
    right_chunks = _record_chunks(right)
    if not left_chunks or not right_chunks:
        return False
    return any(abs(left_chunk - right_chunk) <= 1 for left_chunk in left_chunks for right_chunk in right_chunks)


def _normalized_number(value: str) -> str:
    """
    Normalize one numeric value for quantity comparison.

    :param value: Number-like token
    :return: Canonical decimal token
    """
    normalized = value.lower().replace(",", "").lstrip("$")
    try:
        numeric = Decimal(normalized)
    except InvalidOperation:
        return normalized
    if not numeric.is_finite():
        return normalized
    if numeric == 0:
        return "0"
    if numeric == numeric.to_integral_value():
        return format(numeric.quantize(Decimal("1")), "f")
    return format(numeric.normalize(), "f")


def _scaled_number(value: str, scale: str | None) -> str:
    """
    Apply a recognized magnitude word to one numeric token.

    :param value: Numeric token
    :param scale: Optional magnitude word
    :return: Canonical scaled value
    """
    numeric = Decimal(_normalized_number(value))
    if scale:
        numeric *= Decimal(_NUMBER_SCALES[scale.lower()])
    return _normalized_number(format(numeric, "f"))


def _word_number_value(value: str) -> float | None:
    """
    Parse a compact English number phrase without external dependencies.

    The parser intentionally supports the civic quantities observed in records:
    whole numbers through billions plus ``and a half`` fractions. Invalid or
    connector-only phrases return ``None`` rather than inventing a value.

    :param value: English number phrase
    :return: Numeric value or None
    """
    tokens = [token for token in _WORD_RE.findall(value.lower()) if token]
    if not tokens:
        return None

    # ``a`` is only numeric in phrases such as ``a hundred`` or ``a half``.
    # Treating ordinary article phrases like ``a four-year term`` as five is a
    # serious audit false positive, especially in small-model summaries.
    for index, token in enumerate(tokens):
        if token != "a":
            continue
        next_token = tokens[index + 1] if index + 1 < len(tokens) else ""
        if next_token not in _NUMBER_SCALES and next_token != "half":
            return None

    has_number_word = any(token in _WORD_NUMBER_VALUES for token in tokens)
    has_qualified_a = "a" in tokens and any(
        token in _NUMBER_SCALES or token == "half" for token in tokens
    )
    if not has_number_word and not has_qualified_a:
        return None

    fraction = 0.0
    if len(tokens) >= 3 and tokens[-3:] == ["and", "a", "half"]:
        fraction = 0.5
        tokens = tokens[:-3]
    elif tokens[-2:] == ["a", "half"]:
        fraction = 0.5
        tokens = tokens[:-2]

    total = 0
    current = 0
    for token in tokens:
        if token == "and":
            continue
        if token == "a":
            current += 1
            continue
        if token in _WORD_NUMBER_VALUES:
            current += _WORD_NUMBER_VALUES[token]
            continue
        if token == "hundred":
            current = max(current, 1) * 100
            continue
        if token in {"thousand", "million", "billion"}:
            total += max(current, 1) * _NUMBER_SCALES[token]
            current = 0
            continue
        return None
    return float(total + current) + fraction


def _quantity_token(kind: str, value: str | float | Decimal) -> str:
    """
    Build one stable typed quantity token.

    :param kind: number, currency, percent, or time
    :param value: Canonical numeric value
    :return: Typed quantity token
    """
    return f"{kind}:{_normalized_number(str(value))}"


def _extract_quantities(value: Any) -> set[str]:
    """
    Extract normalized numeric, currency, percentage, and time quantities.

    Spoken number phrases and common transcript time formats are normalized so
    formatting alone does not create a high-risk finding.

    :param value: Text-like artifact value
    :return: Typed canonical quantity tokens
    """
    text = str(value or "")
    quantities: set[str] = set()
    excluded_spans: list[tuple[int, int]] = []

    for match in _DOLLARS_CENTS_RE.finditer(text):
        amount = Decimal(match.group("dollars").replace(",", "")) + (
            Decimal(match.group("cents")) / Decimal("100")
        )
        quantities.add(_quantity_token("currency", amount))
        excluded_spans.append(match.span())

    for match in _RANGE_SCALE_RE.finditer(text):
        right_value = Decimal(match.group("right").replace(",", ""))
        scale = match.group("scale")
        if scale:
            right_value *= Decimal(_NUMBER_SCALES[scale.lower()])
        left_value = Decimal(match.group("left"))
        if not scale and right_value >= 1000 and left_value < 1000:
            magnitude = 10 ** max(0, len(str(int(right_value))) - len(str(int(left_value))))
            left_value *= Decimal(magnitude)
        elif scale:
            left_value *= Decimal(_NUMBER_SCALES[scale.lower()])
        quantities.add(_quantity_token("number", left_value))
        quantities.add(_quantity_token("number", right_value))

    for pattern in (_TIME_RE, _COMPACT_TIME_RE, _AT_COMPACT_TIME_RE):
        for match in pattern.finditer(text):
            if "hour" in match.groupdict():
                compact = int(match.group("hour")) * 100 + int(match.group("minute"))
            else:
                compact = int(match.group("time"))
            quantities.add(_quantity_token("time", compact))
            excluded_spans.append(match.span())

    for match in _INTERRUPTED_SCALE_RE.finditer(text):
        quantities.add(_quantity_token("number", _scaled_number(match.group("number"), match.group("scale"))))
        excluded_spans.append(match.span())

    for match in _NUMBER_RE.finditer(text):
        if any(start <= match.start() < end for start, end in excluded_spans):
            continue
        kind = "currency" if match.group("currency") else "percent" if match.group("percent") else "number"
        scaled = _scaled_number(match.group("number"), match.group("scale"))
        quantities.add(_quantity_token(kind, scaled))

    for match in _NUMBER_WORD_RE.finditer(text):
        numeric = _word_number_value(match.group(0))
        if numeric is None:
            continue
        suffix = text[match.end(): match.end() + 16].lower()
        prefix = text[max(0, match.start() - 12): match.start()].lower()
        kind = (
            "percent"
            if re.match(r"\s*percent\b", suffix)
            else "currency"
            if "$" in prefix or re.match(r"\s*dollars?\b", suffix)
            else "number"
        )
        quantities.add(_quantity_token(kind, numeric))

    return quantities


def _extract_material_quantities(value: Any) -> set[str]:
    """
    Extract only quantities likely to be materially reviewable.

    Balanced auditing intentionally ignores free-standing small counts because
    smaller local extraction models often compress or restate those details
    without creating a materially misleading record. Currency,
    percentages, explicit times, dates, identifiers, and measured values remain
    reviewable.

    :param value: Text-like artifact value
    :return: Material typed quantity tokens
    """
    text = str(value or "")
    all_quantities = _extract_quantities(text)
    material = {
        token
        for token in all_quantities
        if _quantity_kind(token) in {"currency", "percent", "time"}
    }

    for match in _MATERIAL_IDENTIFIER_RE.finditer(text):
        material.update(
            token
            for token in _extract_quantities(match.group("identifier"))
            if _quantity_kind(token) == "number"
        )
    for match in _MATERIAL_YEAR_RE.finditer(text):
        material.add(_quantity_token("number", match.group(0)))
    for match in _MATERIAL_MONTH_DATE_RE.finditer(text):
        material.add(_quantity_token("number", match.group("day")))
    for match in _MATERIAL_MEASUREMENT_RE.finditer(text):
        material.add(_quantity_token("number", match.group("number")))
    return material


def _profile_quantities(value: Any, profile: AuditProfile) -> set[str]:
    """
    Extract quantities using the selected audit sensitivity.

    :param value: Text-like artifact value
    :param profile: Active audit profile
    :return: Profile-calibrated typed quantity tokens
    """
    if profile.quantity_mode == "expanded":
        return _extract_quantities(value)
    return _extract_material_quantities(value)


def _quantity_kind(token: str) -> str:
    """
    Return the type prefix from a canonical quantity token.

    :param token: Typed quantity token
    :return: Quantity kind
    """
    return token.partition(":")[0]

def _quantity_value(token: str) -> str:
    """
    Return the canonical numeric component from a quantity token.

    :param token: Typed quantity token
    :return: Canonical numeric value
    """
    return token.partition(":")[2]


def _currency_scale_is_supported(token: str, context: set[str]) -> bool:
    """
    Recognize shorthand currency values that inherit a nearby magnitude.

    Civic speakers commonly say ``$1.5 million`` and then shorten the comparison
    to ``$1.1``. The second value is treated as scaled only when the same record
    context contains both the unscaled decimal and another currency amount large
    enough to establish the magnitude.

    :param token: Summary currency token
    :param context: Supporting context quantity tokens
    :return: True when a shared magnitude supports the summary value
    """
    if _quantity_kind(token) != "currency":
        return False
    try:
        target = Decimal(_quantity_value(token))
    except InvalidOperation:
        return False

    context_currency_values: list[Decimal] = []
    for context_token in context:
        if _quantity_kind(context_token) != "currency":
            continue
        try:
            context_currency_values.append(Decimal(_quantity_value(context_token)))
        except InvalidOperation:
            continue

    for candidate in context_currency_values:
        if candidate == 0:
            continue
        ratio = target / candidate
        for scale in (1_000, 1_000_000, 1_000_000_000):
            decimal_scale = Decimal(scale)
            if ratio != decimal_scale:
                continue
            if any(value >= decimal_scale for value in context_currency_values):
                return True
    return False


def _unsupported_quantities(summary: set[str], context: set[str]) -> set[str]:
    """
    Find summary quantities not represented in supporting record context.

    Equivalent numeric values support currency and percentage formatting even
    when the transcript omits a symbol or the word ``percent``. Time quantities
    remain type-specific to avoid treating dates or identifiers as clock times.

    :param summary: Summary quantity tokens
    :param context: Subject, evidence, and attribute quantity tokens
    :return: Unsupported summary quantities
    """
    context_values = {_quantity_value(token) for token in context}
    unsupported: set[str] = set()
    for token in summary:
        if token in context:
            continue
        if _quantity_kind(token) != "time" and _quantity_value(token) in context_values:
            continue
        if _currency_scale_is_supported(token, context):
            continue
        unsupported.add(token)
    return unsupported


def _duration_remaining_values(value: Any) -> Dict[str, set[int]]:
    """
    Extract explicit duration-remaining claims grouped by unit.

    This narrow relation check catches cases where a summary preserves a number
    but assigns it to the wrong remaining duration in the evidence.

    :param value: Text-like artifact value
    :return: Unit-to-value mapping
    """
    durations: Dict[str, set[int]] = {}
    text = str(value or "")
    for pattern in (_DURATION_RE, _ANOTHER_DURATION_RE):
        for match in pattern.finditer(text):
            raw_value = match.group("value").lower()
            numeric_value = (
                int(raw_value)
                if raw_value.isdigit()
                else _WORD_NUMBER_VALUES[raw_value]
            )
            unit = match.group("unit").lower().rstrip("s")
            durations.setdefault(unit, set()).add(numeric_value)
    return durations


def _flatten_values(value: Any) -> Iterable[str]:
    """
    Yield only string values from nested configuration data.

    :param value: Nested configuration value
    :return: Iterator of configured values
    """
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _flatten_values(item)
        return
    if isinstance(value, list):
        for item in value:
            yield from _flatten_values(item)


def _flatten_people(value: Any) -> Iterable[str]:
    """
    Yield configured person names and aliases without group labels.

    :param value: Nested people or participants configuration
    :return: Iterator of names and aliases
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(item, dict):
                yield str(key).replace("_", " ")
            yield from _flatten_people(item)
        return
    if isinstance(value, list):
        for item in value:
            yield from _flatten_people(item)
        return
    if isinstance(value, str):
        yield value


def _official_terms(entity_cfg: Dict[str, Any]) -> List[str]:
    """
    Resolve known participant names and official titles from entity config.

    The result is advisory only. Matching a configured official produces a
    review candidate rather than rewriting the record type.

    :param entity_cfg: Merged entity configuration
    :return: Longest-first normalized official terms
    """
    terms: set[str] = set()
    configured_values = [
        *_flatten_people(entity_cfg.get("people", {})),
        *_flatten_people(entity_cfg.get("participants", {})),
        *_flatten_values(entity_cfg.get("titles", {})),
    ]
    for value in configured_values:
        normalized = _normalize_text(value)
        if len(normalized) >= 4:
            terms.add(normalized)
    return sorted(terms, key=lambda item: (-len(item), item))


def _contains_official_attribution(value: Any, official_terms: Sequence[str]) -> str | None:
    """
    Match a configured official only against an explicit attribution field.

    Single common words in subjects or evidence create severe false positives,
    so advisory matching requires an exact attribution, a multi-word configured
    name, or a recognized official role used as part of the attribution.

    :param value: Explicit record attribution
    :param official_terms: Normalized known official names and titles
    :return: Matching term or None
    """
    normalized = _normalize_text(value)
    if not normalized:
        return None
    padded = f" {normalized} "
    for term in official_terms:
        if normalized == term:
            return term
        if " " in term and f" {term} " in padded:
            return term
        if term in _OFFICIAL_ROLE_TERMS and re.search(rf"(?:^| )({re.escape(term)})(?: |$)", normalized):
            return term
    return None

def _contains_unknown(value: Any, path: str = "") -> List[str]:
    """
    Find exact ``Unknown`` sentinel values in nested record data.

    :param value: Nested record value
    :param path: Current field path
    :return: Paths containing an unknown sentinel
    """
    matches: List[str] = []
    if isinstance(value, str) and value.strip().lower() == "unknown":
        matches.append(path or "record")
    elif isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            matches.extend(_contains_unknown(item, child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            child_path = f"{path}[{index}]" if path else f"[{index}]"
            matches.extend(_contains_unknown(item, child_path))
    return matches


def _meeting_type_from_filename(meeting_name: str) -> str | None:
    """
    Infer an explicit formal meeting type encoded in an artifact directory name.

    This check is intentionally narrow and only recognizes known profile labels.
    It is used to nominate classifier mismatches, not to rewrite artifacts.

    :param meeting_name: Meeting artifact directory name
    :return: Expected meeting type or None
    """
    normalized = re.sub(r"[^a-z0-9]+", "_", meeting_name.lower()).strip("_")
    padded = f"_{normalized}_"
    for alias, meeting_type in _FILENAME_MEETING_TYPE_ALIASES:
        if f"_{alias}_" in padded:
            return meeting_type
    return None


def _new_meeting_result(meeting_name: str, meeting_dir: str, project_root: str) -> Dict[str, Any]:
    """
    Create the mutable audit result for one meeting directory.

    :param meeting_name: Meeting artifact directory name
    :param meeting_dir: Absolute meeting artifact directory
    :param project_root: Project root for portable path serialization
    :return: Initial meeting audit result
    """
    return {
        "meeting": meeting_name,
        "meeting_dir": serialize_project_path(meeting_dir, project_root),
        "artifact_path": None,
        "status": "missing",
        "meeting_type": None,
        "job_profile": None,
        "chunk_style": None,
        "timestamp_alignment_present": False,
        "chunk_count": 0,
        "successful_chunk_count": 0,
        "partial_chunk_count": 0,
        "failed_chunk_count": 0,
        "record_count": 0,
        "validation_warning_count": 0,
        "normalized_record_count": 0,
        "rejected_record_count": 0,
        "rejected_line_count": 0,
        "alignment_rate": None,
        "alignment_matched": 0,
        "alignment_ambiguous": 0,
        "alignment_unmatched": 0,
        "duplicate_candidates": 0,
        "summary_evidence_number_mismatches": 0,
        "official_public_comment_candidates": 0,
        "unknown_attribute_values": 0,
        "risk_score": 0,
        "severity_counts": {severity: 0 for severity in SEVERITY_WEIGHTS},
        "findings": [],
    }


def _add_finding(
    result: Dict[str, Any],
    *,
    code: str,
    severity: str,
    message: str,
    record_indices: Sequence[int] | None = None,
    details: Dict[str, Any] | None = None,
) -> None:
    """
    Add one normalized finding to a meeting audit result.

    :param result: Mutable meeting audit result
    :param code: Stable machine-readable finding code
    :param severity: critical, high, medium, or low
    :param message: Human-readable review guidance
    :param record_indices: Optional one-based record positions
    :param details: Optional structured diagnostic details
    :return: None
    :raises ValidationError: If an unsupported severity is supplied
    """
    if severity not in SEVERITY_WEIGHTS:
        raise ValidationError(f"Unsupported audit severity '{severity}'")

    finding: Dict[str, Any] = {
        "code": code,
        "severity": severity,
        "message": message,
    }
    if record_indices:
        finding["record_indices"] = list(record_indices)
    if details:
        finding["details"] = details
    result["findings"].append(finding)


def _load_json_object(path: str) -> Dict[str, Any]:
    """
    Read one JSON object with project-specific error reporting.

    :param path: JSON file path
    :return: Parsed JSON object
    :raises ConfigError: If the file is unreadable or not a JSON object
    """
    try:
        parsed = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Failed to parse JSON artifact '{path}': {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError(f"JSON artifact '{path}' must contain an object")
    return parsed


def _artifact_candidates(meeting_dir: str) -> tuple[List[str], List[str]]:
    """
    Find complete and partial records artifacts in one meeting directory.

    :param meeting_dir: Meeting artifact directory
    :return: Complete and partial artifact path lists
    """
    directory = Path(meeting_dir)
    complete = sorted(str(path) for path in directory.glob("*.records.json"))
    partial = sorted(str(path) for path in directory.glob("*.records.partial.json"))
    return complete, partial


def _validation_file_counts(meeting_dir: str) -> Dict[str, Any]:
    """
    Sum validation outcomes persisted below a meeting's errors directory.

    Unreadable validation files are counted separately so the corpus audit can
    expose diagnostic loss without aborting the full scan.

    :param meeting_dir: Meeting artifact directory
    :return: Validation file and issue counters
    """
    counts = {
        "files": 0,
        "unreadable": 0,
        "warnings": 0,
        "normalized": 0,
        "rejected_records": 0,
        "rejected_lines": 0,
        "issues": [],
    }
    errors_dir = Path(meeting_dir) / "errors"
    if not errors_dir.is_dir():
        return counts

    for path in sorted(errors_dir.glob("*.validation.json")):
        counts["files"] += 1
        try:
            artifact = _load_json_object(str(path))
        except ConfigError:
            counts["unreadable"] += 1
            continue
        counts["warnings"] += _non_negative_int(artifact.get("validation_warning_count"))
        counts["normalized"] += _non_negative_int(artifact.get("normalized_record_count"))
        counts["rejected_records"] += _non_negative_int(artifact.get("rejected_record_count"))
        counts["rejected_lines"] += _non_negative_int(artifact.get("rejected_line_count"))
        issues = artifact.get("validation_issues")
        if isinstance(issues, list):
            for issue in issues:
                if isinstance(issue, dict):
                    counts["issues"].append(
                        {
                            "file": path.name,
                            "issue": _preview_text(
                                json.dumps(issue, ensure_ascii=False, sort_keys=True),
                                limit=1000,
                            ),
                        }
                    )
    return counts


def _non_negative_int(value: Any) -> int:
    """
    Convert a trusted artifact counter into a non-negative integer.

    :param value: Candidate counter
    :return: Non-negative integer or zero
    """
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _alignment_metrics(artifact: Dict[str, Any]) -> Dict[str, Any]:
    """
    Read timestamp alignment counters from a records artifact.

    :param artifact: Parsed records artifact
    :return: Normalized alignment metrics
    """
    metadata = artifact.get("timestamp_alignment")
    if not isinstance(metadata, dict):
        return {
            "present": False,
            "rate": None,
            "matched": 0,
            "ambiguous": 0,
            "unmatched": 0,
        }

    rate = metadata.get("match_rate")
    normalized_rate = float(rate) if isinstance(rate, (int, float)) else None
    return {
        "present": True,
        "rate": normalized_rate,
        "matched": _non_negative_int(metadata.get("matched_record_count")),
        "ambiguous": _non_negative_int(metadata.get("ambiguous_record_count")),
        "unmatched": _non_negative_int(metadata.get("unmatched_record_count")),
    }


def _identity_tokens(value: Any) -> set[str]:
    """
    Extract substantive identity tokens for duplicate comparisons.

    :param value: Subject or identity text
    :return: Non-boilerplate identity tokens
    """
    return {
        token
        for token in _comparison_tokens(value)
        if token not in _GENERIC_IDENTITY_TOKENS
    }


def _record_identity_text(record: Dict[str, Any]) -> str:
    """
    Combine record subject and explicit identifiers for identity matching.

    :param record: Records artifact entry
    :return: Identity comparison text
    """
    attributes = record.get("attributes") if isinstance(record.get("attributes"), dict) else {}
    identifiers = attributes.get("identifiers") if isinstance(attributes, dict) else None
    identifier_text = " ".join(_flatten_values(identifiers)) if identifiers else ""
    return f"{record.get('subject', '')} {identifier_text}".strip()


def _identity_numbers(record: Dict[str, Any]) -> set[str]:
    """
    Extract plain numeric identity tokens from subject and identifiers.

    :param record: Records artifact entry
    :return: Canonical identifier numbers
    """
    return {
        token
        for token in _extract_quantities(_record_identity_text(record))
        if _quantity_kind(token) == "number"
    }


def _is_boilerplate_evidence(value: Any) -> bool:
    """
    Identify low-information parliamentary evidence.

    :param value: Evidence text
    :return: True when evidence is almost entirely procedural boilerplate
    """
    normalized = _normalize_text(value)
    if not normalized:
        return True
    stripped = re.sub(
        r"\b(?:seeing none|all right|please|go ahead|cast your vote|vote for|"
        r"that|also|and|the|motion|approval|approved|passes?|passed|unanimously|"
        r"ayes?|nays?|any opposed|any discussion|thank you|very much|to|zero)\b",
        " ",
        normalized,
    )
    stripped = re.sub(r"\b\d+\b", " ", stripped)
    return not _normalize_text(stripped)


def _duplicate_candidate(
    left: Dict[str, Any],
    right: Dict[str, Any],
    profile: AuditProfile,
) -> Dict[str, Any] | None:
    """
    Determine whether two adjacent-chunk records are likely duplicates.

    Repeated vote language is treated as boilerplate. Balanced mode requires
    stronger shared identity so small-model procedural repetition does not flood
    the report. Strict mode lowers the semantic thresholds for higher-tier model
    review while retaining identifier-conflict protection.

    :param left: First records artifact entry
    :param right: Second records artifact entry
    :param profile: Active audit profile
    :return: Similarity details or None
    """
    if not _chunks_are_adjacent(left, right):
        return None

    left_type = str(left.get("record_type", ""))
    right_type = str(right.get("record_type", ""))
    if {left_type, right_type} in ({"motion", "vote"}, {"procedural_action", "vote"}):
        return None

    subject_ratio = _similarity(left.get("subject"), right.get("subject"))
    summary_ratio = _similarity(left.get("summary"), right.get("summary"))
    evidence_ratio = _similarity(left.get("evidence"), right.get("evidence"))
    combined_left = f"{left.get('subject', '')} {left.get('summary', '')}"
    combined_right = f"{right.get('subject', '')} {right.get('summary', '')}"
    combined_ratio = _similarity(combined_left, combined_right)
    token_overlap = _jaccard_similarity(combined_left, combined_right)

    left_identity = _identity_tokens(_record_identity_text(left))
    right_identity = _identity_tokens(_record_identity_text(right))
    shared_identity = left_identity & right_identity
    identity_union = left_identity | right_identity
    identity_overlap = len(shared_identity) / len(identity_union) if identity_union else 0.0

    left_numbers = _identity_numbers(left)
    right_numbers = _identity_numbers(right)
    if left_numbers and right_numbers and left_numbers.isdisjoint(right_numbers):
        return None

    procedural_types = {"discussion", "motion", "presentation", "procedural_action", "vote"}
    cross_type_evidence_threshold = 0.96 if profile.duplicate_mode == "conservative" else 0.92
    if (
        left_type != right_type
        and left_type in procedural_types
        and right_type in procedural_types
        and evidence_ratio < cross_type_evidence_threshold
    ):
        return None

    boilerplate = _is_boilerplate_evidence(left.get("evidence")) and _is_boilerplate_evidence(
        right.get("evidence")
    )
    boilerplate_identity_threshold = 0.75 if profile.duplicate_mode == "conservative" else 0.65
    if boilerplate and identity_overlap < boilerplate_identity_threshold:
        return None

    exact_subject = _normalize_text(left.get("subject")) == _normalize_text(right.get("subject"))
    exact_evidence = _normalize_text(left.get("evidence")) == _normalize_text(right.get("evidence"))

    if profile.duplicate_mode == "conservative":
        likely = (
            (exact_subject and summary_ratio >= 0.72)
            or (
                subject_ratio >= 0.93
                and summary_ratio >= 0.84
                and identity_overlap >= 0.60
            )
            or (
                combined_ratio >= 0.93
                and token_overlap >= 0.76
                and len(shared_identity) >= 2
            )
            or (
                evidence_ratio >= 0.96
                and summary_ratio >= 0.78
                and identity_overlap >= 0.30
            )
            or (
                evidence_ratio >= 0.90
                and summary_ratio >= 0.82
                and len(shared_identity) >= 3
            )
            or (
                summary_ratio >= 0.88
                and evidence_ratio >= 0.84
                and identity_overlap >= 0.45
            )
            or (exact_evidence and bool(shared_identity))
        )
    else:
        likely = (
            (exact_subject and summary_ratio >= 0.65)
            or (
                subject_ratio >= 0.90
                and summary_ratio >= 0.80
                and identity_overlap >= 0.50
            )
            or (
                combined_ratio >= 0.90
                and token_overlap >= 0.70
                and bool(shared_identity)
            )
            or (
                evidence_ratio >= 0.92
                and summary_ratio >= 0.72
                and identity_overlap >= 0.25
            )
            or (
                evidence_ratio >= 0.84
                and summary_ratio >= 0.76
                and len(shared_identity) >= 2
            )
            or (exact_evidence and bool(shared_identity))
        )
    if not likely:
        return None
    return {
        "subject_similarity": round(subject_ratio, 4),
        "summary_similarity": round(summary_ratio, 4),
        "evidence_similarity": round(evidence_ratio, 4),
        "combined_similarity": round(combined_ratio, 4),
        "token_overlap": round(token_overlap, 4),
        "identity_overlap": round(identity_overlap, 4),
        "shared_identity_tokens": sorted(shared_identity),
        "audit_profile": profile.name,
    }

def _preview_text(value: Any, limit: int = 600) -> str:
    """
    Limit untrusted artifact text embedded in audit diagnostics.

    :param value: Text-like artifact value
    :param limit: Maximum characters retained
    :return: Normalized preview text
    """
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _record_preview(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a bounded human-review preview for one record.

    :param record: Records artifact entry
    :return: Compact record preview
    """
    preview = {
        "record_type": record.get("record_type"),
        "subject": _preview_text(record.get("subject")),
        "summary": _preview_text(record.get("summary")),
        "evidence": _preview_text(record.get("evidence")),
        "source_chunks": _record_chunks(record),
    }
    attribution = record.get("attribution")
    if isinstance(attribution, str) and attribution.strip():
        preview["attribution"] = _preview_text(attribution, limit=160)
    attributes = record.get("attributes")
    if isinstance(attributes, dict) and attributes:
        preview["attributes"] = attributes
    return preview


def _audit_record(
    result: Dict[str, Any],
    record: Dict[str, Any],
    record_index: int,
    official_terms: Sequence[str],
    profile: AuditProfile,
) -> None:
    """
    Apply deterministic field and semantic heuristics to one record.

    :param result: Mutable meeting audit result
    :param record: Parsed record entry
    :param record_index: One-based record position
    :param official_terms: Configured official names and titles
    :param profile: Active audit profile
    :return: None
    """
    finding_start = len(result["findings"])

    for field in _REQUIRED_RECORD_FIELDS:
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            _add_finding(
                result,
                code="invalid_required_field",
                severity="critical",
                message=f"Record {record_index} lacks a non-empty '{field}' string.",
                record_indices=[record_index],
                details={"field": field},
            )

    record_type = record.get("record_type")
    if isinstance(record_type, str) and record_type not in ALLOWED_RECORD_TYPES:
        _add_finding(
            result,
            code="unsupported_record_type",
            severity="high",
            message=f"Record {record_index} uses unsupported record_type '{record_type}'.",
            record_indices=[record_index],
        )

    source_chunks = record.get("source_chunks")
    if not _record_chunks(record) or not isinstance(source_chunks, list):
        _add_finding(
            result,
            code="invalid_source_chunks",
            severity="medium",
            message=f"Record {record_index} lacks valid source chunk provenance.",
            record_indices=[record_index],
        )

    evidence = str(record.get("evidence", ""))
    summary = str(record.get("summary", ""))
    subject = str(record.get("subject", ""))
    attribution = str(record.get("attribution", ""))
    attributes = record.get("attributes") if isinstance(record.get("attributes"), dict) else {}

    unknown_paths = _contains_unknown(
        {
            "attribution": record.get("attribution"),
            "attributes": attributes,
        }
    )
    if unknown_paths:
        result["unknown_attribute_values"] += len(unknown_paths)
        _add_finding(
            result,
            code="unknown_optional_value",
            severity=profile.unknown_value_severity,
            message=f"Record {record_index} retains explicit Unknown optional values.",
            record_indices=[record_index],
            details={"paths": unknown_paths},
        )

    if 0 < len(evidence.strip()) < 16:
        _add_finding(
            result,
            code="generic_short_evidence",
            severity=profile.short_evidence_severity,
            message=f"Record {record_index} has unusually short evidence.",
            record_indices=[record_index],
            details={"evidence": evidence},
        )
    if len(evidence) > 1200:
        _add_finding(
            result,
            code="very_long_evidence",
            severity="low",
            message=f"Record {record_index} has very long evidence that may weaken alignment.",
            record_indices=[record_index],
            details={"evidence_length": len(evidence)},
        )

    if record_type == "public_comment":
        official_match = _contains_official_attribution(attribution, official_terms)
        if official_match:
            result["official_public_comment_candidates"] += 1
            _add_finding(
                result,
                code="official_public_comment_candidate",
                severity="medium",
                message=f"Record {record_index} classifies a configured official as public comment.",
                record_indices=[record_index],
                details={"matched_official_term": official_match},
            )

    normalized_evidence = _normalize_text(evidence)
    normalized_summary = _normalize_text(summary)
    normalized_subject = _normalize_text(subject)
    has_maker = bool(attributes.get("made_by")) and str(attributes.get("made_by")).strip().lower() != "unknown"
    has_seconder = (
        bool(attributes.get("seconded_by"))
        and str(attributes.get("seconded_by")).strip().lower() != "unknown"
    )
    actual_motion_in_evidence = _ACTUAL_MOTION_RE.search(evidence) is not None
    procedural_second_context = (
        _SECONDING_CONTEXT_RE.search(summary) is not None
        or _SECONDING_CONTEXT_RE.search(subject) is not None
        or any(
            term in normalized_summary or term in normalized_subject
            for term in ("motion", "approve", "approval", "adjourn", "amend", "agenda")
        )
    )
    standalone_second = (
        _STANDALONE_SECOND_RE.search(normalized_evidence) is not None
        and len(normalized_evidence.split()) <= 8
        and not actual_motion_in_evidence
    ) or (
        procedural_second_context
        and _SECONDING_CONTEXT_RE.search(f"{summary} {subject}") is not None
        and len(normalized_evidence.split()) <= 8
        and not actual_motion_in_evidence
    )
    if record_type in {"discussion", "motion", "procedural_action"} and (
        standalone_second
        or (
            has_seconder
            and not has_maker
            and len(normalized_evidence.split()) <= 8
            and not actual_motion_in_evidence
        )
    ):
        _add_finding(
            result,
            code="standalone_seconder_motion",
            severity="high",
            message=f"Record {record_index} appears to emit a seconder as a standalone record.",
            record_indices=[record_index],
        )

    explicit_motion = has_maker or _EXPLICIT_MOTION_RE.search(evidence) is not None
    if record_type == "motion" and _MOTION_REQUEST_RE.search(evidence) and not explicit_motion:
        _add_finding(
            result,
            code="motion_request_not_motion",
            severity="medium",
            message=f"Record {record_index} may classify a bare request for a motion as the motion itself.",
            record_indices=[record_index],
        )

    if record_type == "procedural_action" and _MOTION_REQUEST_RE.search(evidence) and not explicit_motion:
        _add_finding(
            result,
            code="motion_request_as_procedural_action",
            severity="medium",
            message=(
                f"Record {record_index} appears to encode a bare request for a motion "
                "as a procedural action."
            ),
            record_indices=[record_index],
        )

    if record_type == "presentation" and _EMPTY_PUBLIC_COMMENT_RE.search(evidence):
        _add_finding(
            result,
            code="empty_public_comment_as_presentation",
            severity="medium",
            message=f"Record {record_index} appears to encode an empty public-comment period as a presentation.",
            record_indices=[record_index],
        )

    if record_type == "vote" and not attributes.get("vote_outcome") and not _VOTE_OUTCOME_RE.search(evidence):
        _add_finding(
            result,
            code="vote_without_outcome_evidence",
            severity="medium",
            message=f"Record {record_index} is typed as a vote without clear outcome evidence.",
            record_indices=[record_index],
        )

    summary_quantities = _profile_quantities(summary, profile)
    support_context = " ".join(
        [
            subject,
            evidence,
            json.dumps(attributes, ensure_ascii=False, sort_keys=True),
        ]
    )
    # Profiles control which summary claims are reviewable. Supporting context
    # remains broad so formatting differences such as ``$80,000`` versus
    # ``80,000`` or ``1.68%`` versus ``1.68 percent`` do not become findings.
    context_quantities = _extract_quantities(support_context)
    introduced_quantities = sorted(_unsupported_quantities(summary_quantities, context_quantities))
    summary_durations = _duration_remaining_values(summary)
    evidence_durations = _duration_remaining_values(evidence)
    duration_conflicts = {
        unit: {
            "summary": sorted(values),
            "evidence": sorted(evidence_durations.get(unit, set())),
        }
        for unit, values in summary_durations.items()
        if evidence_durations.get(unit) and values != evidence_durations[unit]
    }
    if introduced_quantities or duration_conflicts:
        result["summary_evidence_number_mismatches"] += 1
        details: Dict[str, Any] = {}
        if introduced_quantities:
            details["summary_only_quantities"] = introduced_quantities
        if duration_conflicts:
            details["remaining_duration_conflicts"] = duration_conflicts
        material_claim_language = re.search(
            r"\b(?:amount|budget|cost|fee|funding|paid|pay|price|salary|total|value)\b",
            summary,
            re.IGNORECASE,
        ) is not None
        severe_quantity = any(
            _quantity_kind(token) in {"currency", "percent"}
            for token in introduced_quantities
        ) and (
            record_type in {"contract", "financial", "metric", "project"}
            or material_claim_language
        )
        severity = "high" if duration_conflicts or severe_quantity else "medium"
        _add_finding(
            result,
            code="summary_evidence_quantity_mismatch",
            severity=severity,
            message=(
                f"Record {record_index} introduces or reassigns quantities not supported "
                "by its subject, evidence, or attributes."
            ),
            record_indices=[record_index],
            details=details,
        )

    if profile.enable_detail_gap_check:
        summary_words = len(_WORD_RE.findall(_normalize_text(summary)))
        evidence_words = len(_WORD_RE.findall(_normalize_text(evidence)))
        if summary_words >= 24 and (
            evidence_words <= 10 or _is_boilerplate_evidence(evidence)
        ):
            _add_finding(
                result,
                code="summary_evidence_detail_gap",
                severity="medium",
                message=(
                    f"Record {record_index} has a detailed summary supported by "
                    "very limited evidence."
                ),
                record_indices=[record_index],
                details={
                    "summary_word_count": summary_words,
                    "evidence_word_count": evidence_words,
                },
            )

    if record_type in _SUBSTANTIVE_CONTEXT_TYPES and len(evidence) >= 160:
        subject_identity = _identity_tokens(subject)
        evidence_identity = _identity_tokens(evidence)
        shared_identity = subject_identity & evidence_identity
        identity_union = subject_identity | evidence_identity
        identity_overlap = len(shared_identity) / len(identity_union) if identity_union else 0.0
        subject_identifiers = {
            token
            for token in _extract_material_quantities(subject)
            if _quantity_kind(token) == "number"
        }
        evidence_identifiers = {
            token
            for token in _extract_material_quantities(evidence)
            if _quantity_kind(token) == "number"
        }
        missing_subject_identifiers = sorted(subject_identifiers - evidence_identifiers)
        subject_only = subject_identity - evidence_identity
        evidence_only = evidence_identity - subject_identity
        competing_context = (
            identity_overlap < 0.08
            and len(subject_only) >= 3
            and len(evidence_only) >= 5
            and bool(evidence_identifiers - subject_identifiers)
        )
        if missing_subject_identifiers and len(subject_identity) >= 4 and identity_overlap < 0.12:
            severity = "high" if competing_context else profile.context_identifier_severity
            message = (
                f"Record {record_index} may associate substantive evidence with the wrong subject."
                if competing_context
                else f"Record {record_index} evidence does not repeat material subject identifiers."
            )
            _add_finding(
                result,
                code="subject_evidence_context_mismatch",
                severity=severity,
                message=message,
                record_indices=[record_index],
                details={
                    "missing_subject_identifiers": missing_subject_identifiers,
                    "identity_overlap": round(identity_overlap, 4),
                    "competing_context": competing_context,
                    "competing_evidence_tokens": sorted(evidence_only)[:12],
                },
            )

    preview = _record_preview(record)
    for finding in result["findings"][finding_start:]:
        finding.setdefault("details", {})["record"] = preview

def _audit_duplicates(
    result: Dict[str, Any],
    records: Sequence[Dict[str, Any]],
    profile: AuditProfile,
) -> None:
    """
    Nominate likely fuzzy duplicates from the same or adjacent chunks.

    Same-chunk comparisons are bounded because LLM duplicates normally occur
    near one another, while distant repeated parliamentary language is usually
    unrelated agenda activity.

    :param result: Mutable meeting audit result
    :param records: Valid record objects in source order
    :param profile: Active audit profile
    :return: None
    """
    for left_index, left in enumerate(records):
        left_chunks = set(_record_chunks(left))
        for right_index in range(left_index + 1, len(records)):
            right = records[right_index]
            right_chunks = set(_record_chunks(right))
            if left_chunks == right_chunks and right_index - left_index > 8:
                continue
            similarity = _duplicate_candidate(left, right, profile)
            if similarity is None:
                continue
            result["duplicate_candidates"] += 1
            _add_finding(
                result,
                code="adjacent_chunk_duplicate_candidate",
                severity="medium",
                message=(
                    f"Records {left_index + 1} and {right_index + 1} are likely "
                    "same/adjacent-chunk duplicates."
                ),
                record_indices=[left_index + 1, right_index + 1],
                details={
                    **similarity,
                    "left_record": _record_preview(left),
                    "right_record": _record_preview(right),
                },
            )

def _finalize_meeting_result(result: Dict[str, Any]) -> None:
    """
    Calculate raw severity totals and a repetition-aware triage score.

    Detailed findings remain unchanged, but repeated instances of one heuristic
    receive diminishing risk weight so a meeting is not dominated by boilerplate
    or one recurring schema-cleanliness issue.

    :param result: Mutable meeting audit result
    :return: None
    """
    severity_counts = Counter(finding["severity"] for finding in result["findings"])
    code_counts = Counter(finding["code"] for finding in result["findings"])
    result["severity_counts"] = {
        severity: severity_counts.get(severity, 0)
        for severity in SEVERITY_WEIGHTS
    }
    result["finding_code_counts"] = dict(sorted(code_counts.items()))

    risk_score = 0
    seen_codes: Counter[str] = Counter()
    for finding in result["findings"]:
        code = finding["code"]
        weight = SEVERITY_WEIGHTS[finding["severity"]]
        occurrence = seen_codes[code]
        if occurrence == 0:
            risk_score += weight
        elif occurrence <= 4:
            risk_score += max(1, weight // 4)
        seen_codes[code] += 1
    result["risk_score"] = risk_score

def audit_meeting_directory(
    meeting_dir: str,
    *,
    entity_cfg: Dict[str, Any],
    project_root: str,
    profile: AuditProfile,
) -> Dict[str, Any]:
    """
    Audit one records meeting directory without changing its artifacts.

    :param meeting_dir: Meeting artifact directory
    :param entity_cfg: Merged entity configuration
    :param project_root: Project root for portable path serialization
    :param profile: Active audit profile
    :return: Meeting audit result
    """
    meeting_name = os.path.basename(os.path.abspath(meeting_dir))
    result = _new_meeting_result(meeting_name, meeting_dir, project_root)
    complete_paths, partial_paths = _artifact_candidates(meeting_dir)

    if len(complete_paths) > 1:
        _add_finding(
            result,
            code="multiple_complete_artifacts",
            severity="critical",
            message="Meeting directory contains multiple complete records artifacts.",
            details={"paths": [serialize_project_path(path, project_root) for path in complete_paths]},
        )
    if len(partial_paths) > 1:
        _add_finding(
            result,
            code="multiple_partial_artifacts",
            severity="high",
            message="Meeting directory contains multiple partial records artifacts.",
            details={"paths": [serialize_project_path(path, project_root) for path in partial_paths]},
        )

    artifact_path = complete_paths[0] if complete_paths else (partial_paths[0] if partial_paths else None)
    if artifact_path is None:
        _add_finding(
            result,
            code="missing_records_artifact",
            severity="critical",
            message="Meeting directory has no complete or partial records JSON artifact.",
        )
        _finalize_meeting_result(result)
        return result

    result["artifact_path"] = serialize_project_path(artifact_path, project_root)
    if not complete_paths:
        _add_finding(
            result,
            code="partial_records_artifact",
            severity="critical",
            message="Meeting has only a partial records artifact.",
        )

    try:
        artifact = _load_json_object(artifact_path)
    except ConfigError as exc:
        _add_finding(
            result,
            code="unreadable_records_artifact",
            severity="critical",
            message=str(exc),
        )
        _finalize_meeting_result(result)
        return result

    result["status"] = str(artifact.get("status", "unknown"))
    result["meeting_type"] = artifact.get("meeting_type")
    result["job_profile"] = (
        artifact.get("job_profile")
        if isinstance(artifact.get("job_profile"), str)
        and artifact.get("job_profile").strip()
        else None
    )
    result["chunk_style"] = (
        artifact.get("chunk_style")
        if artifact.get("chunk_style") in {"word", "sentence", "density"}
        else None
    )
    result["timestamp_alignment_present"] = (
        isinstance(artifact.get("timestamp_alignment"), dict)
        or any(Path(meeting_dir).glob("*.records.alignment.json"))
    )
    for field in (
        "chunk_count",
        "successful_chunk_count",
        "partial_chunk_count",
        "failed_chunk_count",
        "record_count",
        "validation_warning_count",
        "normalized_record_count",
        "rejected_record_count",
        "rejected_line_count",
    ):
        result[field] = _non_negative_int(artifact.get(field))

    filename_meeting_type = _meeting_type_from_filename(meeting_name)
    if (
        filename_meeting_type
        and result["meeting_type"] not in {filename_meeting_type, "general"}
    ):
        _add_finding(
            result,
            code="meeting_type_filename_mismatch",
            severity=profile.filename_mismatch_severity,
            message=(
                f"Filename indicates '{filename_meeting_type}' but the artifact "
                f"was routed as '{result['meeting_type']}'."
            ),
            details={
                "filename_meeting_type": filename_meeting_type,
                "artifact_meeting_type": result["meeting_type"],
            },
        )

    if result["status"] != "complete":
        _add_finding(
            result,
            code="incomplete_records_status",
            severity="critical",
            message=f"Records artifact status is '{result['status']}', not complete.",
        )
    if result["failed_chunk_count"]:
        _add_finding(
            result,
            code="failed_chunks",
            severity="critical",
            message=f"Artifact reports {result['failed_chunk_count']} failed chunk(s).",
        )
    if result["partial_chunk_count"]:
        _add_finding(
            result,
            code="partial_chunks",
            severity="high",
            message=f"Artifact reports {result['partial_chunk_count']} partial chunk(s).",
        )
    if result["rejected_record_count"]:
        _add_finding(
            result,
            code="rejected_records",
            severity="high",
            message=f"Artifact rejected {result['rejected_record_count']} record(s).",
        )
    if result["rejected_line_count"]:
        _add_finding(
            result,
            code="rejected_jsonl_lines",
            severity="high",
            message=f"Artifact rejected {result['rejected_line_count']} JSONL line(s).",
        )
    if result["normalized_record_count"]:
        _add_finding(
            result,
            code="normalized_records",
            severity="low",
            message=f"Artifact normalized {result['normalized_record_count']} record type(s).",
        )
    if result["validation_warning_count"]:
        _add_finding(
            result,
            code="validation_warnings",
            severity="medium",
            message=f"Artifact reports {result['validation_warning_count']} validation warning(s).",
        )

    validation_counts = _validation_file_counts(meeting_dir)
    if validation_counts["unreadable"]:
        _add_finding(
            result,
            code="unreadable_validation_files",
            severity="high",
            message=f"{validation_counts['unreadable']} validation diagnostic file(s) are unreadable.",
        )
    if validation_counts["files"]:
        message = f"{validation_counts['files']} validation diagnostic file(s) are available."
        if not result["validation_warning_count"]:
            message += " The canonical warning count is zero."
        _add_finding(
            result,
            code="validation_diagnostics_present",
            severity="low",
            message=message,
            details={
                "issue_count": len(validation_counts["issues"]),
                "issue_samples": validation_counts["issues"][:20],
            },
        )

    records_value = artifact.get("records")
    if not isinstance(records_value, list):
        _add_finding(
            result,
            code="missing_records_list",
            severity="critical",
            message="Records artifact does not contain a records list.",
        )
        _finalize_meeting_result(result)
        return result

    valid_records: List[Dict[str, Any]] = []
    official_terms = _official_terms(entity_cfg)
    for record_index, record in enumerate(records_value, start=1):
        if not isinstance(record, dict):
            _add_finding(
                result,
                code="non_object_record",
                severity="critical",
                message=f"Record {record_index} is not a JSON object.",
                record_indices=[record_index],
            )
            continue
        valid_records.append(record)
        _audit_record(result, record, record_index, official_terms, profile)

    if result["record_count"] != len(records_value):
        _add_finding(
            result,
            code="record_count_mismatch",
            severity="high",
            message=(
                f"record_count is {result['record_count']} but the artifact contains "
                f"{len(records_value)} record entries."
            ),
        )
    if not records_value and result["chunk_count"]:
        _add_finding(
            result,
            code="zero_records_from_nonempty_meeting",
            severity="high",
            message="Meeting has chunks but no extracted records.",
        )

    _audit_duplicates(result, valid_records, profile)

    alignment = _alignment_metrics(artifact)
    result["alignment_rate"] = alignment["rate"]
    result["alignment_matched"] = alignment["matched"]
    result["alignment_ambiguous"] = alignment["ambiguous"]
    result["alignment_unmatched"] = alignment["unmatched"]
    if not alignment["present"]:
        _add_finding(
            result,
            code="timestamp_alignment_missing",
            severity="low",
            message="Records artifact has no timestamp alignment metadata.",
        )
    else:
        if (
            alignment["rate"] is not None
            and alignment["rate"] < profile.low_alignment_threshold
        ):
            _add_finding(
                result,
                code="low_timestamp_alignment_rate",
                severity="high",
                message=f"Timestamp alignment rate is {alignment['rate']:.2%}.",
            )
        elif (
            alignment["rate"] is not None
            and alignment["rate"] < profile.weak_alignment_threshold
        ):
            _add_finding(
                result,
                code="weak_timestamp_alignment_rate",
                severity="medium",
                message=f"Timestamp alignment rate is {alignment['rate']:.2%}.",
            )
        if alignment["unmatched"] > 2:
            _add_finding(
                result,
                code="unmatched_timestamp_records",
                severity="medium",
                message=f"Timestamp alignment left {alignment['unmatched']} records unmatched.",
            )
        if alignment["ambiguous"] > 2:
            _add_finding(
                result,
                code="ambiguous_timestamp_records",
                severity="low",
                message=f"Timestamp alignment marked {alignment['ambiguous']} records ambiguous.",
            )

    _finalize_meeting_result(result)
    return result


def _quartiles(values: Sequence[float]) -> tuple[float, float]:
    """
    Calculate lower and upper quartiles with a median-of-halves method.

    :param values: Numeric values
    :return: First and third quartiles
    :raises ValidationError: If fewer than four values are supplied
    """
    ordered = sorted(values)
    if len(ordered) < 4:
        raise ValidationError("At least four values are required for quartiles")
    midpoint = len(ordered) // 2
    lower = ordered[:midpoint]
    upper = ordered[midpoint:] if len(ordered) % 2 == 0 else ordered[midpoint + 1 :]
    return float(median(lower)), float(median(upper))


def _apply_distribution_findings(meetings: Sequence[Dict[str, Any]]) -> None:
    """
    Add low-severity corpus distribution outliers to meeting results.

    :param meetings: Mutable meeting audit result dictionaries
    :return: None
    """
    eligible = [
        meeting
        for meeting in meetings
        if meeting["status"] == "complete" and meeting["chunk_count"] > 0
    ]
    if len(eligible) < 8:
        return

    ratios = [meeting["record_count"] / meeting["chunk_count"] for meeting in eligible]
    q1, q3 = _quartiles(ratios)
    iqr = q3 - q1
    lower_bound = max(0.0, q1 - 1.5 * iqr)
    upper_bound = q3 + 1.5 * iqr

    for meeting in eligible:
        ratio = meeting["record_count"] / meeting["chunk_count"]
        if ratio < lower_bound or ratio > upper_bound:
            _add_finding(
                meeting,
                code="records_per_chunk_outlier",
                severity="low",
                message=f"Records-per-chunk ratio {ratio:.2f} is a corpus outlier.",
                details={
                    "ratio": round(ratio, 4),
                    "lower_bound": round(lower_bound, 4),
                    "upper_bound": round(upper_bound, 4),
                },
            )
            _finalize_meeting_result(meeting)


def _discover_meeting_directories(
    records_dir: str,
    *,
    output_dir: str,
    meeting_filter: str | None,
    allow_empty: bool = False,
) -> List[str]:
    """
    Discover immediate meeting artifact directories beneath a records root.

    :param records_dir: Model-specific records root
    :param output_dir: Audit output directory to exclude from discovery
    :param meeting_filter: Optional case-insensitive directory-name substring
    :param allow_empty: Whether an empty records root is valid for rebuild planning
    :return: Sorted meeting directory paths
    :raises ValidationError: If no matching meeting directories exist
    """
    records_path = Path(records_dir).resolve()
    output_path = Path(output_dir).resolve()
    if not records_path.is_dir():
        if allow_empty:
            return []
        raise ValidationError(f"Records directory not found: {records_path}")

    normalized_filter = meeting_filter.lower() if meeting_filter else None
    directories = []
    for path in sorted(records_path.iterdir(), key=lambda item: item.name.lower()):
        if (
            not path.is_dir()
            or path.resolve() == output_path
            or path.name == "audit"
            or path.name.startswith(".")
        ):
            continue
        if normalized_filter and normalized_filter not in path.name.lower():
            continue
        directories.append(str(path.resolve()))

    if not directories and not allow_empty:
        if meeting_filter:
            raise ValidationError(
                f"No meeting directories matching '{meeting_filter}' found in: {records_path}"
            )
        raise ValidationError(f"No meeting artifact directories found in: {records_path}")
    return directories


def _finding_code_counts(meeting: Dict[str, Any]) -> str:
    """
    Render compact finding-code counts for CSV output.

    :param meeting: Meeting audit result
    :return: Semicolon-delimited code counts
    """
    counts = Counter(finding["code"] for finding in meeting["findings"])
    return ";".join(f"{code}:{counts[code]}" for code in sorted(counts))


def render_audit_csv(report: Dict[str, Any]) -> str:
    """
    Render a flat CSV inventory from a records audit report.

    :param report: Complete records audit report
    :return: CSV text
    """
    fieldnames = [
        "audit_profile",
        "meeting",
        "status",
        "meeting_type",
        "chunk_count",
        "successful_chunk_count",
        "partial_chunk_count",
        "failed_chunk_count",
        "record_count",
        "validation_warning_count",
        "normalized_record_count",
        "rejected_record_count",
        "rejected_line_count",
        "alignment_rate",
        "alignment_matched",
        "alignment_ambiguous",
        "alignment_unmatched",
        "duplicate_candidates",
        "summary_evidence_number_mismatches",
        "official_public_comment_candidates",
        "unknown_attribute_values",
        "risk_score",
        "critical_findings",
        "high_findings",
        "medium_findings",
        "low_findings",
        "finding_codes",
        "artifact_path",
    ]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for meeting in report["meetings"]:
        writer.writerow(
            {
                **{field: meeting.get(field) for field in fieldnames},
                "audit_profile": report["audit_profile"],
                "critical_findings": meeting["severity_counts"]["critical"],
                "high_findings": meeting["severity_counts"]["high"],
                "medium_findings": meeting["severity_counts"]["medium"],
                "low_findings": meeting["severity_counts"]["low"],
                "finding_codes": _finding_code_counts(meeting),
            }
        )
    return output.getvalue()


def _markdown_escape(value: Any) -> str:
    """
    Escape table-sensitive characters for Markdown rendering.

    :param value: Value to render
    :return: Markdown-safe text
    """
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def _format_alignment_rate(value: Any) -> str:
    """
    Format an optional alignment rate for human review.

    :param value: Optional numeric alignment rate
    :return: Percentage or dash
    """
    if isinstance(value, (int, float)):
        return f"{float(value):.1%}"
    return "—"


def _append_record_preview(
    lines: List[str],
    label: str,
    preview: Dict[str, Any],
) -> None:
    """
    Append one bounded record preview to Markdown report lines.

    :param lines: Mutable Markdown lines
    :param label: Human-readable preview label
    :param preview: Bounded record preview
    :return: None
    """
    lines.append(
        f"  - {label}: `{_markdown_escape(preview.get('record_type') or 'unknown')}` "
        f"— **{_markdown_escape(preview.get('subject') or 'Untitled record')}**"
    )
    if preview.get("attribution"):
        lines.append(f"    - Attribution: {_markdown_escape(preview['attribution'])}")
    if preview.get("attributes"):
        lines.append(
            "    - Attributes: `"
            + json.dumps(preview["attributes"], ensure_ascii=False, sort_keys=True)
            .replace("`", "\\`")
            + "`"
        )
    if preview.get("summary"):
        lines.append(f"    - Summary: {_markdown_escape(preview['summary'])}")
    if preview.get("evidence"):
        lines.append(f"    - Evidence: “{_markdown_escape(preview['evidence'])}”")
    if preview.get("source_chunks"):
        chunks = ", ".join(str(chunk) for chunk in preview["source_chunks"])
        lines.append(f"    - Source chunks: {chunks}")


def render_audit_markdown(
    report: Dict[str, Any],
    *,
    top: int,
    min_risk: int,
) -> str:
    """
    Render a ranked human-review report from corpus audit data.

    :param report: Complete records audit report
    :param top: Maximum meetings to render with detailed findings
    :param min_risk: Minimum risk score for detailed rendering
    :return: Markdown report text
    """
    summary = report["summary"]
    lines = [
        "# Records Audit",
        "",
        f"Entity: `{report['entity']}`",
        "",
        f"Model: `{report['model']}`",
        "",
        f"Audit profile: `{report['audit_profile']}`",
        "",
        f"Records directory: `{report['records_dir']}`",
        "",
        "This report is deterministic and read-only. Findings are review candidates, "
        "not automatic corrections.",
        "",
        "## Corpus Summary",
        "",
        f"- Meetings audited: {summary['meeting_count']}",
        f"- Meetings with findings: {summary['meetings_with_findings']}",
        f"- Complete artifacts: {summary['complete_artifacts']}",
        f"- Partial or missing artifacts: {summary['partial_or_missing_artifacts']}",
        f"- Total records: {summary['record_count']}",
        f"- Duplicate candidates: {summary['duplicate_candidates']}",
        f"- Quantity mismatch candidates: {summary['summary_evidence_number_mismatches']}",
        f"- Official/public-comment candidates: {summary['official_public_comment_candidates']}",
        "",
        "## Meeting Inventory",
        "",
        "| Risk | Meeting | Status | Type | Chunks | Records | Alignment | Findings |",
        "|---:|---|---|---|---:|---:|---:|---:|",
    ]
    for meeting in report["meetings"]:
        lines.append(
            "| "
            f"{meeting['risk_score']} | "
            f"{_markdown_escape(meeting['meeting'])} | "
            f"{_markdown_escape(meeting['status'])} | "
            f"{_markdown_escape(meeting['meeting_type'] or '—')} | "
            f"{meeting['successful_chunk_count']}/{meeting['chunk_count']} | "
            f"{meeting['record_count']} | "
            f"{_format_alignment_rate(meeting['alignment_rate'])} | "
            f"{len(meeting['findings'])} |"
        )

    lines.extend(["", "## Highest-Risk Meetings", ""])
    detailed = [meeting for meeting in report["meetings"] if meeting["risk_score"] >= min_risk]
    if top >= 0:
        detailed = detailed[:top]
    if not detailed:
        lines.extend(["No meetings met the detailed-review threshold.", ""])
        return "\n".join(lines).strip() + "\n"

    for rank, meeting in enumerate(detailed, start=1):
        lines.extend(
            [
                f"### {rank}. {meeting['meeting']}",
                "",
                f"Risk score: **{meeting['risk_score']}**",
                "",
                f"Artifact: `{meeting['artifact_path'] or 'missing'}`",
                "",
            ]
        )
        for finding in meeting["findings"]:
            record_text = ""
            if finding.get("record_indices"):
                record_text = " Records: " + ", ".join(
                    str(index) for index in finding["record_indices"]
                ) + "."
            lines.append(
                f"- **{finding['severity'].upper()} — {finding['code']}**: "
                f"{finding['message']}{record_text}"
            )
            details = dict(finding.get("details") or {})
            record_preview = details.pop("record", None)
            left_preview = details.pop("left_record", None)
            right_preview = details.pop("right_record", None)
            if isinstance(record_preview, dict):
                _append_record_preview(lines, "Record", record_preview)
            if isinstance(left_preview, dict):
                _append_record_preview(lines, "Left record", left_preview)
            if isinstance(right_preview, dict):
                _append_record_preview(lines, "Right record", right_preview)
            if details:
                lines.append(
                    "  - Diagnostic: `"
                    + json.dumps(details, ensure_ascii=False, sort_keys=True)
                    .replace("`", "\\`")
                    + "`"
                )
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _report_summary(meetings: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """
    Aggregate corpus-level counters from meeting audit results.

    :param meetings: Ranked meeting audit results
    :return: Corpus summary counters
    """
    return {
        "meeting_count": len(meetings),
        "meetings_with_findings": sum(bool(meeting["findings"]) for meeting in meetings),
        "complete_artifacts": sum(meeting["status"] == "complete" for meeting in meetings),
        "partial_or_missing_artifacts": sum(meeting["status"] != "complete" for meeting in meetings),
        "record_count": sum(meeting["record_count"] for meeting in meetings),
        "duplicate_candidates": sum(meeting["duplicate_candidates"] for meeting in meetings),
        "summary_evidence_number_mismatches": sum(
            meeting["summary_evidence_number_mismatches"] for meeting in meetings
        ),
        "official_public_comment_candidates": sum(
            meeting["official_public_comment_candidates"] for meeting in meetings
        ),
        "unknown_attribute_values": sum(meeting["unknown_attribute_values"] for meeting in meetings),
        "critical_findings": sum(meeting["severity_counts"]["critical"] for meeting in meetings),
        "high_findings": sum(meeting["severity_counts"]["high"] for meeting in meetings),
        "medium_findings": sum(meeting["severity_counts"]["medium"] for meeting in meetings),
        "low_findings": sum(meeting["severity_counts"]["low"] for meeting in meetings),
    }


def _most_common_text(values: Iterable[Any], default: str) -> str:
    """
    Resolve one stable corpus default from artifact provenance.

    :param values: Candidate text values
    :param default: Fallback when no usable values are present
    :return: Most common non-empty text value
    """
    normalized = [
        str(value).strip()
        for value in values
        if isinstance(value, str) and str(value).strip()
    ]
    if not normalized:
        return default
    counts = Counter(normalized)
    return sorted(counts, key=lambda item: (-counts[item], item))[0]


def _resolve_rebuild_source_files(
    *,
    global_config_path: str,
    entity: str,
    model: str | None,
    meeting_filter: str | None,
) -> tuple[List[str], str | None]:
    """
    Discover normalized transcripts used to verify rebuild eligibility.

    Rebuild planning compares source transcripts with the records directory so a
    missing meeting directory is visible. Ordinary audit mode remains records-only.

    :param global_config_path: Path to global yatsee.toml
    :param entity: Entity handle
    :param model: Optional configured records model override
    :param meeting_filter: Optional case-insensitive meeting substring
    :return: Matching transcript paths and an optional discovery error
    """
    try:
        resolved = resolve_record_paths(
            global_config_path,
            entity=entity,
            model=model,
        )
    except ValidationError as exc:
        return [], str(exc)

    normalized_filter = meeting_filter.lower() if meeting_filter else None
    files = []
    for path in resolved["file_list"]:
        base_name = sanitize_path_component(
            os.path.splitext(os.path.basename(path))[0]
        )
        if normalized_filter and normalized_filter not in base_name.lower():
            continue
        files.append(os.path.abspath(path))
    return files, None


def _source_file_map(source_files: Sequence[str]) -> Dict[str, str]:
    """
    Map sanitized records meeting names to normalized transcript paths.

    :param source_files: Normalized transcript paths
    :return: Meeting name to absolute transcript path mapping
    :raises ValidationError: If two sources collapse to the same artifact name
    """
    mapped: Dict[str, str] = {}
    for source_path in source_files:
        meeting = sanitize_path_component(
            os.path.splitext(os.path.basename(source_path))[0]
        )
        existing = mapped.get(meeting)
        if existing and os.path.abspath(existing) != os.path.abspath(source_path):
            raise ValidationError(
                "Multiple normalized transcripts resolve to the same records "
                f"meeting name '{meeting}'"
            )
        mapped[meeting] = os.path.abspath(source_path)
    return mapped


def _rebuild_reasons(meeting: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Select only structural findings for which extraction is a rational remedy.

    Finding severity is intentionally not consulted. Quantity heuristics,
    duplicates, attribution quality, public-comment typing, timestamp alignment,
    and other model-quality findings can never trigger this policy.

    :param meeting: One meeting audit result
    :return: Stable rebuild reason dictionaries
    """
    reasons: List[Dict[str, Any]] = []
    seen_codes: set[str] = set()
    for finding in meeting.get("findings", []):
        code = finding.get("code")
        if code not in _REBUILD_DIRECT_FINDING_CODES or code in seen_codes:
            continue
        seen_codes.add(code)
        reasons.append(
            {
                "code": code,
                "message": str(finding.get("message") or code),
            }
        )

    rejected_count = (
        _non_negative_int(meeting.get("rejected_record_count"))
        + _non_negative_int(meeting.get("rejected_line_count"))
    )
    attempted_count = _non_negative_int(meeting.get("record_count")) + rejected_count
    rejection_rate = rejected_count / attempted_count if attempted_count else 0.0
    threshold = max(
        REBUILD_MIN_REJECTED_ITEMS,
        math.ceil(attempted_count * REBUILD_MIN_REJECTION_RATE),
    )
    if rejected_count >= threshold:
        reasons.append(
            {
                "code": "excessive_validation_rejection",
                "message": (
                    f"Extraction rejected {rejected_count} of {attempted_count} "
                    f"record or JSONL candidates ({rejection_rate:.1%})."
                ),
                "rejected_count": rejected_count,
                "attempted_count": attempted_count,
                "rejection_rate": round(rejection_rate, 6),
            }
        )
    return reasons


def build_rebuild_plan(
    report: Dict[str, Any],
    *,
    source_files: Sequence[str],
    project_root: str,
    source_discovery_error: str | None = None,
) -> tuple[Dict[str, Any], Dict[str, str]]:
    """
    Build a deterministic records rebuild plan from one audit report.

    The plan is a recommendation only. It contains no heuristic content-quality
    findings and records whether each candidate has a normalized source available
    for an automatic attempt.

    :param report: Complete records audit report
    :param source_files: Matching normalized transcript paths
    :param project_root: Project root for portable path serialization
    :param source_discovery_error: Optional transcript discovery failure
    :return: Rebuild plan and runtime-only meeting-to-source mapping
    """
    source_map = _source_file_map(source_files)
    meeting_map = {
        meeting["meeting"]: meeting
        for meeting in report.get("meetings", [])
    }
    default_job_profile = _most_common_text(
        (meeting.get("job_profile") for meeting in meeting_map.values()),
        "civic",
    )
    default_chunk_style = _most_common_text(
        (meeting.get("chunk_style") for meeting in meeting_map.values()),
        "density",
    )
    alignment_values = [
        bool(meeting.get("timestamp_alignment_present"))
        for meeting in meeting_map.values()
        if meeting.get("status") == "complete"
    ]
    default_align_timestamps = (
        sum(alignment_values) > len(alignment_values) / 2
        if alignment_values
        else False
    )

    candidate_names = {
        name
        for name, meeting in meeting_map.items()
        if _rebuild_reasons(meeting)
    }
    candidate_names.update(set(source_map) - set(meeting_map))

    candidates: List[Dict[str, Any]] = []
    runtime_sources: Dict[str, str] = {}
    for meeting_name in sorted(candidate_names, key=str.lower):
        meeting = meeting_map.get(meeting_name)
        if meeting is None:
            reasons = [
                {
                    "code": "missing_meeting_artifact_directory",
                    "message": (
                        "Normalized transcript has no matching records meeting "
                        "artifact directory."
                    ),
                }
            ]
        else:
            reasons = _rebuild_reasons(meeting)
        if not reasons:
            continue

        source_path = source_map.get(meeting_name)
        blockers = [] if source_path else ["normalized_transcript_missing"]
        if source_path:
            runtime_sources[meeting_name] = source_path

        meeting_status = meeting.get("status") if meeting else "missing"
        candidate_align_timestamps = (
            bool(meeting.get("timestamp_alignment_present"))
            if meeting and meeting_status == "complete"
            else default_align_timestamps
        )
        candidate = {
            "meeting": meeting_name,
            "rebuildable": source_path is not None,
            "blockers": blockers,
            "source_transcript": (
                serialize_project_path(source_path, project_root)
                if source_path
                else None
            ),
            "artifact_path": meeting.get("artifact_path") if meeting else None,
            "status": meeting_status,
            "record_count": _non_negative_int(
                meeting.get("record_count") if meeting else 0
            ),
            "reasons": reasons,
            "settings": {
                "job_profile": (
                    meeting.get("job_profile")
                    if meeting and meeting.get("job_profile")
                    else default_job_profile
                ),
                "chunk_style": (
                    meeting.get("chunk_style")
                    if meeting and meeting.get("chunk_style")
                    else default_chunk_style
                ),
                "meeting_type": (
                    meeting.get("meeting_type")
                    if meeting
                    and isinstance(meeting.get("meeting_type"), str)
                    and meeting.get("meeting_type").strip()
                    else None
                ),
                "align_timestamps": candidate_align_timestamps,
            },
        }
        candidates.append(candidate)

    rebuildable_count = sum(candidate["rebuildable"] for candidate in candidates)
    plan = {
        "artifact_kind": REBUILD_ARTIFACT_KIND,
        "schema_version": REBUILD_SCHEMA_VERSION,
        "mode": REBUILD_MODE_PLAN,
        "entity": report["entity"],
        "model": report["model"],
        "audit_profile": report["audit_profile"],
        "records_dir": report["records_dir"],
        "meeting_filter": report.get("meeting_filter"),
        "candidate_count": len(candidates),
        "rebuildable_count": rebuildable_count,
        "blocked_count": len(candidates) - rebuildable_count,
        "source_discovery_error": source_discovery_error,
        "policy": {
            "direct_finding_codes": sorted(_REBUILD_DIRECT_FINDING_CODES),
            "minimum_rejected_items": REBUILD_MIN_REJECTED_ITEMS,
            "minimum_rejection_rate": REBUILD_MIN_REJECTION_RATE,
            "heuristic_findings_trigger_rebuild": False,
            "maximum_attempts_per_invocation": 1,
        },
        "candidates": candidates,
    }
    return plan, runtime_sources


def _write_rebuild_manifest(
    plan: Dict[str, Any],
    *,
    output_dir: str,
) -> str:
    """
    Atomically write one rebuild plan or execution manifest.

    :param plan: Rebuild plan dictionary
    :param output_dir: Audit output directory
    :return: Absolute manifest path
    """
    ensure_directory(output_dir)
    path = os.path.join(output_dir, REBUILD_MANIFEST_NAME)
    write_text_atomic(path, json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    return path


def _execute_rebuild_plan(
    plan: Dict[str, Any],
    *,
    runtime_sources: Dict[str, str],
    global_config_path: str,
    entity: str,
    model: str,
    records_dir: str,
) -> List[Dict[str, Any]]:
    """
    Attempt each rebuildable candidate exactly once.

    The records stage keeps an existing complete artifact until a replacement is
    complete and atomically writable. Failures are isolated per meeting so one
    provider or model error does not consume repeated attempts or stop the batch.

    :param plan: Rebuild plan dictionary
    :param runtime_sources: Absolute source paths keyed by meeting name
    :param global_config_path: Path to global yatsee.toml
    :param entity: Entity handle
    :param model: Resolved records model
    :param records_dir: Model-specific records output directory
    :return: Per-meeting execution results
    """
    from yatsee.intel.record import run_records_stage

    executions: List[Dict[str, Any]] = []
    for candidate in plan.get("candidates", []):
        meeting = candidate["meeting"]
        if not candidate.get("rebuildable"):
            executions.append(
                {
                    "meeting": meeting,
                    "status": "blocked",
                    "message": "Normalized transcript is unavailable.",
                }
            )
            continue

        settings = candidate.get("settings", {})
        try:
            stage_result = run_records_stage(
                global_config_path=global_config_path,
                entity=entity,
                input_path=runtime_sources[meeting],
                output_dir=records_dir,
                model=model,
                job_profile=str(settings.get("job_profile") or "civic"),
                chunk_style=str(settings.get("chunk_style") or "density"),
                meeting_type=settings.get("meeting_type"),
                force=True,
                align_timestamps=bool(settings.get("align_timestamps")),
            )
            item = stage_result["results"][0]
            completed = (
                item.get("status") == "complete"
                and item.get("failed_chunk_count", 0) == 0
                and item.get("partial_chunk_count", 0) == 0
            )
            executions.append(
                {
                    "meeting": meeting,
                    "status": "complete" if completed else "incomplete",
                    "artifact_path": item.get("output_path"),
                    "record_count": item.get("record_count", 0),
                    "failed_chunk_count": item.get("failed_chunk_count", 0),
                    "partial_chunk_count": item.get("partial_chunk_count", 0),
                    "validation_warning_count": item.get(
                        "validation_warning_count",
                        0,
                    ),
                }
            )
        except Exception as exc:
            # Automatic mode is an operator convenience boundary. Isolate model,
            # provider, and filesystem failures per meeting and never retry here.
            executions.append(
                {
                    "meeting": meeting,
                    "status": "failed",
                    "message": str(exc),
                }
            )
    return executions


def _report_filenames(profile: AuditProfile) -> Dict[str, str]:
    """
    Resolve report filenames without overwriting balanced results in strict mode.

    Balanced mode preserves the established ``report.*`` filenames. Strict mode
    appends its profile name so both reports can coexist in one audit directory.

    :param profile: Active audit profile
    :return: Mapping of report format to filename
    """
    if profile.name == AUDIT_PROFILE_BALANCED:
        return {
            "json": AUDIT_JSON_NAME,
            "csv": AUDIT_CSV_NAME,
            "markdown": AUDIT_MARKDOWN_NAME,
        }
    return {
        "json": f"report.{profile.name}.json",
        "csv": f"report.{profile.name}.csv",
        "markdown": f"report.{profile.name}.md",
    }


def run_records_audit(
    *,
    global_config_path: str,
    entity: str,
    model: str | None = None,
    output_dir: str | None = None,
    meeting_filter: str | None = None,
    output_format: str = "all",
    top: int = 20,
    min_risk: int = 1,
    audit_profile: str = AUDIT_PROFILE_BALANCED,
    rebuild_mode: str | None = None,
    _allow_empty_records: bool = False,
) -> Dict[str, Any]:
    """
    Audit a model-specific entity records corpus and write review reports.

    Normal and strict audit modes remain read-only. Optional rebuild planning
    applies a separate structural policy that ignores heuristic model-quality
    findings. Automatic mode attempts each rebuildable meeting once and then
    reruns the audit to report the resulting state.

    :param global_config_path: Path to global yatsee.toml
    :param entity: Entity handle
    :param model: Optional configured records model override
    :param output_dir: Optional audit report directory override
    :param meeting_filter: Optional meeting directory substring
    :param output_format: all, json, csv, or markdown
    :param top: Maximum detailed meetings in Markdown; zero disables details
    :param min_risk: Minimum risk score for Markdown detail
    :param audit_profile: Explicit balanced or strict audit profile
    :param rebuild_mode: None, plan, or auto
    :param _allow_empty_records: Internal support for post-rebuild empty roots
    :return: Audit execution metadata, report paths, and optional rebuild data
    :raises ValidationError: If arguments or records paths are invalid
    """
    profile = _resolve_audit_profile(audit_profile)
    if output_format not in {"all", "json", "csv", "markdown"}:
        raise ValidationError(f"Unsupported audit output format '{output_format}'")
    if top < 0:
        raise ValidationError("Audit --top must be zero or greater")
    if min_risk < 0:
        raise ValidationError("Audit --min-risk must be zero or greater")
    if rebuild_mode not in {None, REBUILD_MODE_PLAN, REBUILD_MODE_AUTO}:
        raise ValidationError(
            "Audit rebuild mode must be 'plan', 'auto', or omitted"
        )

    resolved = resolve_audit_paths(
        global_config_path,
        entity=entity,
        model=model,
        output_dir=output_dir,
    )
    if meeting_filter and output_dir is None:
        # A focused audit must not replace the canonical full-corpus report.
        # Store filtered reports beneath a stable, contained meeting namespace.
        report_component = sanitize_path_component(meeting_filter)
        resolved["output_dir"] = resolve_contained_path(
            resolved["output_dir"],
            "meetings",
            report_component,
        )
    project_root = _project_root(global_config_path)
    meeting_dirs = _discover_meeting_directories(
        resolved["records_dir"],
        output_dir=resolved["output_dir"],
        meeting_filter=meeting_filter,
        allow_empty=bool(rebuild_mode) or _allow_empty_records,
    )
    meetings = [
        audit_meeting_directory(
            meeting_dir,
            entity_cfg=resolved["entity_cfg"],
            project_root=project_root,
            profile=profile,
        )
        for meeting_dir in meeting_dirs
    ]
    _apply_distribution_findings(meetings)
    meetings.sort(key=lambda item: (-item["risk_score"], item["meeting"].lower()))

    report = {
        "artifact_kind": AUDIT_ARTIFACT_KIND,
        "schema_version": AUDIT_SCHEMA_VERSION,
        "entity": entity,
        "model": resolved["model"],
        "audit_profile": profile.name,
        "records_dir": serialize_project_path(resolved["records_dir"], project_root),
        "meeting_filter": meeting_filter,
        "summary": _report_summary(meetings),
        "meetings": meetings,
    }

    ensure_directory(resolved["output_dir"])
    report_filenames = _report_filenames(profile)
    paths = {
        report_format: os.path.join(resolved["output_dir"], filename)
        for report_format, filename in report_filenames.items()
    }
    written_paths: Dict[str, str] = {}
    if output_format in {"all", "json"}:
        write_text_atomic(
            paths["json"],
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        )
        written_paths["json"] = paths["json"]
    if output_format in {"all", "csv"}:
        write_text_atomic(paths["csv"], render_audit_csv(report))
        written_paths["csv"] = paths["csv"]
    if output_format in {"all", "markdown"}:
        write_text_atomic(
            paths["markdown"],
            render_audit_markdown(report, top=top, min_risk=min_risk),
        )
        written_paths["markdown"] = paths["markdown"]

    result = {
        "entity": entity,
        "model": resolved["model"],
        "audit_profile": profile.name,
        "records_dir": resolved["records_dir"],
        "output_dir": resolved["output_dir"],
        "meeting_count": report["summary"]["meeting_count"],
        "meetings_with_findings": report["summary"]["meetings_with_findings"],
        "critical_findings": report["summary"]["critical_findings"],
        "high_findings": report["summary"]["high_findings"],
        "medium_findings": report["summary"]["medium_findings"],
        "low_findings": report["summary"]["low_findings"],
        "report_paths": written_paths,
        "report": report,
    }
    if rebuild_mode is None:
        return result

    source_files, source_discovery_error = _resolve_rebuild_source_files(
        global_config_path=global_config_path,
        entity=entity,
        model=resolved["model"],
        meeting_filter=meeting_filter,
    )
    plan, runtime_sources = build_rebuild_plan(
        report,
        source_files=source_files,
        project_root=project_root,
        source_discovery_error=source_discovery_error,
    )
    plan["mode"] = rebuild_mode
    manifest_path = _write_rebuild_manifest(
        plan,
        output_dir=resolved["output_dir"],
    )
    result.update(
        {
            "rebuild_mode": rebuild_mode,
            "rebuild_candidate_count": plan["candidate_count"],
            "rebuildable_count": plan["rebuildable_count"],
            "rebuild_blocked_count": plan["blocked_count"],
            "rebuild_manifest_path": manifest_path,
            "rebuild_plan": plan,
        }
    )
    if rebuild_mode == REBUILD_MODE_PLAN or plan["rebuildable_count"] == 0:
        return result

    executions = _execute_rebuild_plan(
        plan,
        runtime_sources=runtime_sources,
        global_config_path=global_config_path,
        entity=entity,
        model=resolved["model"],
        records_dir=resolved["records_dir"],
    )
    execution_counts = Counter(execution["status"] for execution in executions)
    plan["execution"] = executions
    plan["execution_summary"] = {
        "attempted": sum(
            execution["status"] != "blocked" for execution in executions
        ),
        "complete": execution_counts.get("complete", 0),
        "incomplete": execution_counts.get("incomplete", 0),
        "failed": execution_counts.get("failed", 0),
        "blocked": execution_counts.get("blocked", 0),
    }
    _write_rebuild_manifest(plan, output_dir=resolved["output_dir"])

    # Re-audit exactly once after the batch. This measures whether structural
    # rebuild reasons cleared without creating a retry loop.
    post_result = run_records_audit(
        global_config_path=global_config_path,
        entity=entity,
        model=resolved["model"],
        output_dir=output_dir,
        meeting_filter=meeting_filter,
        output_format=output_format,
        top=top,
        min_risk=min_risk,
        audit_profile=profile.name,
        rebuild_mode=None,
        _allow_empty_records=True,
    )
    post_plan, _ = build_rebuild_plan(
        post_result["report"],
        source_files=source_files,
        project_root=project_root,
        source_discovery_error=source_discovery_error,
    )
    plan["remaining_candidate_count"] = post_plan["candidate_count"]
    plan["remaining_rebuildable_count"] = post_plan["rebuildable_count"]
    plan["remaining_candidates"] = [
        {
            "meeting": candidate["meeting"],
            "rebuildable": candidate["rebuildable"],
            "reasons": candidate["reasons"],
        }
        for candidate in post_plan["candidates"]
    ]
    _write_rebuild_manifest(plan, output_dir=resolved["output_dir"])

    post_result.update(
        {
            "rebuild_mode": rebuild_mode,
            "rebuild_candidate_count": plan["candidate_count"],
            "rebuildable_count": plan["rebuildable_count"],
            "rebuild_blocked_count": plan["blocked_count"],
            "rebuild_manifest_path": manifest_path,
            "rebuild_plan": plan,
            "rebuild_execution": executions,
            "rebuild_execution_summary": plan["execution_summary"],
            "remaining_rebuild_candidate_count": post_plan["candidate_count"],
        }
    )
    return post_result
