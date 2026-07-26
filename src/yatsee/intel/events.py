"""
Deterministic civic event generation for YATSEE records artifacts.

This module converts atomic records into conservative civic events without
invoking an LLM or modifying the source records artifacts. It collapses only
strong adjacent duplicates, groups local motion/second/vote sequences, and
preserves every contributing record for review and downstream publication.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Sequence

from yatsee.core.errors import ConfigError, ValidationError
from yatsee.core.format import format_markdown_inline, format_timestamp
from yatsee.core.io import ensure_directory, read_text, write_text_atomic
from yatsee.core.paths import sanitize_path_component, serialize_project_path
from yatsee.intel.resolve import resolve_event_paths

EVENT_ARTIFACT_KIND = "events"
EVENT_SCHEMA_VERSION = "civic_events_v1"
EVENT_GENERATION_VERSION = "events_v1"
EVENT_INDEX_ARTIFACT_KIND = "events_index"
EVENT_INDEX_SCHEMA_VERSION = "civic_events_index_v1"
EVENTS_JSON_SUFFIX = ".events.json"
EVENTS_MARKDOWN_SUFFIX = ".events.md"
EVENTS_JSONL_NAME = "events.jsonl"
MEETINGS_JSON_NAME = "meetings.json"

_REQUIRED_RECORD_FIELDS = ("record_type", "subject", "summary", "evidence")
_WORD_RE = re.compile(r"[a-z0-9]+")
_SPACE_RE = re.compile(r"\s+")
_MOTION_REQUEST_RE = re.compile(
    r"\b(?:do i have|is there|looking for|need|ask for|entertain|request)\s+"
    r"(?:a\s+)?motion\b",
    re.IGNORECASE,
)
_EXPLICIT_MOTION_RE = re.compile(
    r"\b(?:motion by|moved by|so moved|motion made|i move|"
    r"i make (?:a )?motion|make (?:a )?motion to|move to approve|"
    r"move to adopt|move to adjourn)\b",
    re.IGNORECASE,
)
_SECOND_RE = re.compile(
    r"\b(?:seconded(?:\s+by)?|seconding|second\s+by|second\s+for|"
    r"a second was offered|motion was seconded|motion seconded|"
    r"is there a second|i(?:'ll| will)? do a second)\b",
    re.IGNORECASE,
)
_SECOND_START_RE = re.compile(
    r"^(?:second(?:ed|ing)?(?:\s+by|\s+for)?|is there a second|"
    r"i(?:'ll| will)? do a second)\b",
    re.IGNORECASE,
)
_OUTCOME_PATTERNS = (
    ("passed", re.compile(r"\b(?:passes|passed|carries|carried|unanimous)\b", re.IGNORECASE)),
    ("approved", re.compile(r"\b(?:approves|approved)\b", re.IGNORECASE)),
    ("adopted", re.compile(r"\b(?:adopts|adopted)\b", re.IGNORECASE)),
    ("failed", re.compile(r"\b(?:fails|failed|defeats|defeated)\b", re.IGNORECASE)),
    ("denied", re.compile(r"\b(?:denies|denied)\b", re.IGNORECASE)),
    ("tabled", re.compile(r"\btabled\b", re.IGNORECASE)),
    ("deferred", re.compile(r"\b(?:defers|deferred|postpones|postponed)\b", re.IGNORECASE)),
    ("referred", re.compile(r"\b(?:refers|referred)\b", re.IGNORECASE)),
    ("withdrawn", re.compile(r"\bwithdrawn\b", re.IGNORECASE)),
)
_OUTCOME_COMPATIBILITY = {
    "passed": "successful",
    "approved": "successful",
    "adopted": "successful",
}
_DATE_YMD_RE = re.compile(
    r"(?<!\d)(?P<year>20\d{2})[-_.](?P<month>\d{1,2})[-_.](?P<day>\d{1,2})(?!\d)"
)
_DATE_MDY_RE = re.compile(
    r"(?<!\d)(?P<month>\d{1,2})[-_](?P<day>\d{1,2})[-_]"
    r"(?P<year>20\d{2}|\d{2})(?!\d)"
)
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_OUTCOME_RECORD_TYPES = {
    "motion",
    "vote",
    "procedural_action",
    "ordinance",
    "resolution",
    "financial",
    "contract",
    "project",
    "appointment",
}

_SUBJECT_STOP_WORDS = {
    "a",
    "an",
    "and",
    "approval",
    "approve",
    "approved",
    "for",
    "final",
    "motion",
    "of",
    "on",
    "regarding",
    "the",
    "to",
    "vote",
}

_EVENT_SECTION_ORDER = (
    "Decisions and Votes",
    "Procedural Actions",
    "Ordinances and Resolutions",
    "Financial and Contract Events",
    "Projects",
    "Appointments",
    "Presentations and Reports",
    "Metrics",
    "Discussion",
    "Public Comment",
    "Proclamations and Recognitions",
    "Follow-ups",
    "Attendance",
    "Other",
)

_EVENT_TYPE_SECTIONS = {
    "decision": "Decisions and Votes",
    "motion": "Decisions and Votes",
    "vote": "Decisions and Votes",
    "procedural_action": "Procedural Actions",
    "ordinance": "Ordinances and Resolutions",
    "resolution": "Ordinances and Resolutions",
    "financial": "Financial and Contract Events",
    "contract": "Financial and Contract Events",
    "project": "Projects",
    "appointment": "Appointments",
    "presentation": "Presentations and Reports",
    "metric": "Metrics",
    "discussion": "Discussion",
    "public_comment": "Public Comment",
    "proclamation": "Proclamations and Recognitions",
    "recognition": "Proclamations and Recognitions",
    "follow_up": "Follow-ups",
    "attendance": "Attendance",
    "other": "Other",
}


@dataclass
class RecordNode:
    """
    Hold one deduplicated record node during event generation.

    A node may contain multiple adjacent source records when deterministic
    duplicate rules establish that they represent the same extracted item.

    :param records: Source records with one-based ``record_index`` metadata
    :param rules: Duplicate rules used to merge source records into this node
    """

    records: List[Dict[str, Any]]
    rules: List[str] = field(default_factory=list)

    @property
    def first_index(self) -> int:
        """
        Return the first source record index represented by this node.

        :return: One-based source record index
        """
        return min(int(record["record_index"]) for record in self.records)

    @property
    def last_index(self) -> int:
        """
        Return the last source record index represented by this node.

        :return: One-based source record index
        """
        return max(int(record["record_index"]) for record in self.records)

    @property
    def primary(self) -> Dict[str, Any]:
        """
        Return the earliest source record represented by this node.

        :return: Source record dictionary
        """
        return min(self.records, key=lambda record: int(record["record_index"]))


def _normalize_text(value: Any) -> str:
    """
    Normalize human text for conservative deterministic comparisons.

    :param value: Arbitrary value to normalize
    :return: Lowercase whitespace-normalized text
    """
    text = str(value or "").strip().lower()
    text = _SPACE_RE.sub(" ", text)
    return text


def _subject_tokens(value: Any) -> set[str]:
    """
    Return distinctive normalized subject tokens.

    Common action boilerplate is removed so related subjects such as
    ``Approval of DHG 2612`` and ``DHG 2612`` can match without using an LLM.

    :param value: Subject text
    :return: Distinctive subject token set
    """
    return {
        token
        for token in _WORD_RE.findall(_normalize_text(value))
        if token not in _SUBJECT_STOP_WORDS
    }


def _subjects_related(left: Any, right: Any) -> bool:
    """
    Decide whether two subjects are strongly related.

    The rule intentionally favors false negatives over false joins. Exact
    normalized matches pass immediately. Otherwise at least two distinctive
    tokens and substantial token overlap are required, except that a shared
    numeric identifier may support a shorter match.

    :param left: First subject
    :param right: Second subject
    :return: ``True`` when the subjects are safe to group
    """
    left_text = _normalize_text(left)
    right_text = _normalize_text(right)
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True

    left_tokens = _subject_tokens(left_text)
    right_tokens = _subject_tokens(right_text)
    if not left_tokens or not right_tokens:
        return False

    shared = left_tokens & right_tokens
    union = left_tokens | right_tokens
    shared_numbers = {token for token in shared if token.isdigit()}
    left_numbers = {token for token in left_tokens if token.isdigit()}
    right_numbers = {token for token in right_tokens if token.isdigit()}

    # Conflicting explicit identifiers are a strong reason not to combine two
    # actions even when generic wording overlaps.
    if left_numbers and right_numbers and not shared_numbers:
        return False

    overlap = len(shared) / len(union)
    if len(shared) >= 3 and overlap >= 0.45:
        return True
    if len(shared) >= 2 and overlap >= 0.60:
        return True
    if shared_numbers and len(shared) >= 2 and overlap >= 0.40:
        return True
    return False


def _source_chunks(record: Dict[str, Any]) -> List[int]:
    """
    Return validated source chunk identifiers from a record.

    :param record: Source record
    :return: Sorted unique positive chunk identifiers
    """
    raw_chunks = record.get("source_chunks", [])
    if not isinstance(raw_chunks, list):
        return []
    return sorted(
        {
            int(chunk)
            for chunk in raw_chunks
            if isinstance(chunk, int) and not isinstance(chunk, bool) and chunk > 0
        }
    )


def _chunks_are_adjacent(left: RecordNode, right: RecordNode) -> bool:
    """
    Check whether two nodes originate from overlapping or adjacent chunks.

    :param left: Earlier record node
    :param right: Later record node
    :return: ``True`` when chunk provenance is compatible with boundary duplication
    """
    left_chunks = sorted(
        {chunk for record in left.records for chunk in _source_chunks(record)}
    )
    right_chunks = sorted(
        {chunk for record in right.records for chunk in _source_chunks(record)}
    )
    if not left_chunks or not right_chunks:
        return True
    return min(abs(a - b) for a in left_chunks for b in right_chunks) <= 1


def _duplicate_rule(left: RecordNode, right: RecordNode) -> str | None:
    """
    Return the deterministic duplicate rule joining two nearby nodes.

    Different record types are never collapsed because identical evidence can
    legitimately support multiple civic facts, such as a metric and a
    recognition. Records also must remain close in source order and chunk
    provenance.

    :param left: Earlier record node
    :param right: Later record node
    :return: Stable duplicate rule name or ``None``
    """
    if right.first_index - left.last_index > 3:
        return None
    if not _chunks_are_adjacent(left, right):
        return None

    left_record = left.primary
    right_record = right.primary
    if _normalize_text(left_record.get("record_type")) != _normalize_text(
        right_record.get("record_type")
    ):
        return None

    left_subject_tokens = _subject_tokens(left_record.get("subject"))
    right_subject_tokens = _subject_tokens(right_record.get("subject"))
    shared_subject_tokens = left_subject_tokens & right_subject_tokens
    left_numbers = {token for token in left_subject_tokens if token.isdigit()}
    right_numbers = {token for token in right_subject_tokens if token.isdigit()}
    conflicting_numbers = bool(
        left_numbers and right_numbers and not (left_numbers & right_numbers)
    )

    left_evidence = _normalize_text(left_record.get("evidence"))
    right_evidence = _normalize_text(right_record.get("evidence"))
    if left_evidence and left_evidence == right_evidence:
        if not conflicting_numbers and (
            _subjects_related(
                left_record.get("subject"),
                right_record.get("subject"),
            )
            or len(shared_subject_tokens) >= 2
        ):
            return "identical_evidence"
        return None

    if not _subjects_related(left_record.get("subject"), right_record.get("subject")):
        return None

    left_subject = _normalize_text(left_record.get("subject"))
    right_subject = _normalize_text(right_record.get("subject"))
    left_summary = _normalize_text(left_record.get("summary"))
    right_summary = _normalize_text(right_record.get("summary"))
    if left_subject == right_subject and left_summary and left_summary == right_summary:
        return "identical_subject_summary"
    if left_summary and left_summary == right_summary:
        return "identical_summary_related_subject"
    return None


def _validate_records_artifact(artifact: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Validate the minimum records contract required by event generation.

    :param artifact: Parsed records artifact
    :return: Validated source records
    :raises ValidationError: If the artifact is not a complete records artifact
    """
    if not isinstance(artifact, dict):
        raise ValidationError("Records artifact must be a JSON object")
    if artifact.get("artifact_kind") != "records":
        raise ValidationError("Events input must be a records artifact")
    if artifact.get("status") != "complete":
        raise ValidationError("Events input must have status 'complete'")

    records = artifact.get("records")
    if not isinstance(records, list):
        raise ValidationError("Records artifact must contain a records list")

    validated: List[Dict[str, Any]] = []
    for record_index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValidationError(f"Record {record_index} must be a JSON object")
        for field_name in _REQUIRED_RECORD_FIELDS:
            value = record.get(field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(
                    f"Record {record_index} field '{field_name}' must be a non-empty string"
                )
        source_chunks = record.get("source_chunks")
        if not isinstance(source_chunks, list):
            raise ValidationError(
                f"Record {record_index} source_chunks must be a list"
            )
        output = dict(record)
        output["record_index"] = record_index
        validated.append(output)
    return validated


def _deduplicate_records(records: Sequence[Dict[str, Any]]) -> List[RecordNode]:
    """
    Collapse only strong adjacent duplicate records into nodes.

    Each new record is compared with the three most recent nodes because known
    chunk-boundary duplicates may have one or two intervening records. This
    bounded lookback avoids corpus-wide accidental merging of repeated agenda
    language.

    :param records: Validated source records with record indices
    :return: Deduplicated record nodes in source order
    """
    nodes: List[RecordNode] = []
    for record in records:
        candidate = RecordNode(records=[record])
        matched_node: RecordNode | None = None
        matched_rule: str | None = None

        for existing in reversed(nodes[-3:]):
            rule = _duplicate_rule(existing, candidate)
            if rule is not None:
                matched_node = existing
                matched_rule = rule
                break

        if matched_node is None:
            nodes.append(candidate)
            continue

        matched_node.records.append(record)
        if matched_rule not in matched_node.rules:
            matched_node.rules.append(str(matched_rule))

    return nodes


def _combined_record_text(record: Dict[str, Any]) -> str:
    """
    Combine record summary and evidence for deterministic classification.

    :param record: Source record
    :return: Combined normalized text
    """
    return _normalize_text(
        f"{record.get('summary', '')} {record.get('evidence', '')}"
    )


def _is_motion_request(record: Dict[str, Any]) -> bool:
    """
    Identify a bare request for a motion rather than an actual motion.

    :param record: Source record
    :return: ``True`` for a likely request-only record
    """
    if _extract_outcome(record) is not None:
        return False
    text = _combined_record_text(record)
    return bool(_MOTION_REQUEST_RE.search(text)) and not bool(
        _EXPLICIT_MOTION_RE.search(text)
    )


def _is_standalone_second(record: Dict[str, Any]) -> bool:
    """
    Identify a seconding statement emitted as its own record.

    The check is intentionally narrow so ordinary phrases such as ``second
    truck`` or long substantive records that happen to mention a seconder are
    not converted into procedural components.

    :param record: Source record
    :return: ``True`` for a likely standalone second
    """
    record_type = _normalize_text(record.get("record_type"))
    if record_type not in {"motion", "procedural_action", "discussion", "vote"}:
        return False
    if _extract_outcome(record) is not None or _is_motion_request(record):
        return False

    summary = _normalize_text(record.get("summary"))
    evidence = _normalize_text(record.get("evidence"))
    if _EXPLICIT_MOTION_RE.search(f"{summary} {evidence}"):
        return False

    summary_is_second = bool(
        _SECOND_START_RE.search(summary)
        or re.search(r"\b(?:was|were) seconded\b", summary, re.IGNORECASE)
    )
    evidence_is_second = bool(_SECOND_START_RE.search(evidence))
    if not (summary_is_second or evidence_is_second):
        return False

    # Long evidence usually contains the surrounding action rather than only a
    # standalone second. Keep those records substantive unless the evidence
    # itself starts with an unmistakable seconding phrase.
    if len(evidence) > 220 and not evidence_is_second:
        return False
    return True


def _is_vote(record: Dict[str, Any]) -> bool:
    """
    Return whether a record is explicitly typed as a vote.

    :param record: Source record
    :return: Vote classification result
    """
    return _normalize_text(record.get("record_type")) == "vote"


def _is_motion(record: Dict[str, Any]) -> bool:
    """
    Return whether a record represents an actual motion.

    :param record: Source record
    :return: Actual-motion classification result
    """
    if _normalize_text(record.get("record_type")) != "motion":
        return False
    return not _is_motion_request(record) and not _is_standalone_second(record)


def _extract_outcome(record: Dict[str, Any]) -> str | None:
    """
    Extract a conservative normalized outcome from explicit record content.

    Attributes are preferred because prompts may encode vote outcomes there.
    Text matching is limited to explicit outcome verbs and does not infer an
    outcome merely from the presence of a vote.

    :param record: Source record
    :return: Normalized outcome or ``None``
    """
    attributes = record.get("attributes")
    if isinstance(attributes, dict):
        for key in ("vote_outcome", "outcome", "status"):
            value = attributes.get(key)
            if isinstance(value, str) and value.strip():
                normalized = _normalize_text(value)
                for outcome, pattern in _OUTCOME_PATTERNS:
                    if pattern.search(normalized):
                        return outcome
                if normalized not in {"unknown", "not stated", "not specified"}:
                    return normalized.replace(" ", "_")

    record_type = _normalize_text(record.get("record_type"))
    if record_type not in _OUTCOME_RECORD_TYPES:
        return None

    text = _combined_record_text(record)
    for outcome, pattern in _OUTCOME_PATTERNS:
        if pattern.search(text):
            return outcome
    return None


def _decision_component(record: Dict[str, Any]) -> str | None:
    """
    Classify a record's role in a decision sequence.

    :param record: Source record
    :return: ``motion``, ``second``, ``vote``, ``request``, ``outcome``, or ``None``
    """
    if _is_vote(record):
        return "vote"
    if _is_motion_request(record):
        return "request"
    if _is_standalone_second(record):
        return "second"
    if _is_motion(record):
        return "motion"
    if _extract_outcome(record) is not None and _normalize_text(
        record.get("record_type")
    ) == "procedural_action":
        return "outcome"
    return None


def _nodes_related(left_nodes: Sequence[RecordNode], right: RecordNode) -> bool:
    """
    Check whether a candidate node matches any subject in an open decision group.

    :param left_nodes: Existing decision group nodes
    :param right: Candidate node
    :return: ``True`` when a strong subject relationship exists
    """
    right_subjects = [record.get("subject") for record in right.records]
    for left_node in left_nodes:
        for left_record in left_node.records:
            for right_subject in right_subjects:
                if _subjects_related(left_record.get("subject"), right_subject):
                    return True
    return False


def _group_decision_nodes(nodes: Sequence[RecordNode]) -> List[List[RecordNode]]:
    """
    Group local motion, second, request, vote, and outcome nodes.

    A group ends at the first vote or explicit outcome. An unrelated decision
    component closes the current group so repeated generic agenda language does
    not bridge separate actions. Non-decision records may remain between related
    components, but the source-record gap may not exceed four.

    :param nodes: Deduplicated record nodes in source order
    :return: Node groups representing one event each
    """
    groups: List[List[RecordNode]] = []
    used: set[int] = set()

    for position, node in enumerate(nodes):
        if position in used:
            continue

        component = _decision_component(node.primary)
        if component is None:
            groups.append([node])
            used.add(position)
            continue

        group = [node]
        used.add(position)
        terminal = component in {"vote", "outcome"}

        if terminal and node.last_index > node.first_index:
            # A deduplicated vote node may span an intervening motion or second
            # from the next chunk. Include related decision components that
            # occur before the node's final duplicate source record.
            for candidate_position in range(position + 1, len(nodes)):
                candidate = nodes[candidate_position]
                if candidate.first_index >= node.last_index:
                    break
                candidate_component = _decision_component(candidate.primary)
                if candidate_component and _nodes_related(group, candidate):
                    group.append(candidate)
                    used.add(candidate_position)

        if not terminal:
            for candidate_position in range(position + 1, len(nodes)):
                candidate = nodes[candidate_position]
                if candidate_position in used:
                    continue
                if candidate.first_index - group[-1].last_index > 4:
                    break

                candidate_component = _decision_component(candidate.primary)
                if candidate_component is None:
                    continue
                if not _nodes_related(group, candidate):
                    break

                group.append(candidate)
                used.add(candidate_position)
                if candidate_component in {"vote", "outcome"}:
                    break

        groups.append(group)

    return groups


def _primary_record(group: Sequence[RecordNode]) -> Dict[str, Any]:
    """
    Select the best existing source record for event subject and summary.

    Actual motions are preferred for decision events because they usually carry
    the actionable subject. Votes are next because they preserve the explicit
    outcome when no motion was extracted. No new prose is generated.

    :param group: Event record nodes
    :return: Selected source record
    """
    records = sorted(
        (record for node in group for record in node.records),
        key=lambda record: int(record["record_index"]),
    )
    for predicate in (_is_motion, _is_vote):
        for record in records:
            if predicate(record):
                return record
    for record in records:
        if not _is_motion_request(record) and not _is_standalone_second(record):
            return record
    return records[0]


def _component_indices(records: Sequence[Dict[str, Any]]) -> Dict[str, List[int]]:
    """
    Build a compact map from decision component to source record indices.

    :param records: Source records in one event
    :return: Component index mapping
    """
    components: Dict[str, List[int]] = {}
    for record in records:
        component = _decision_component(record)
        if component is None:
            continue
        components.setdefault(component, []).append(int(record["record_index"]))
    return components


def _event_outcome(records: Sequence[Dict[str, Any]]) -> tuple[str | None, List[str]]:
    """
    Resolve an event outcome while preserving explicit conflicts.

    Successful civic disposition terms such as ``approved``, ``passed``, and
    ``adopted`` are compatible descriptions of the same completed decision.
    When compatible terms coexist, an explicit vote outcome is preferred for
    the event-level value while all observed terms remain available as
    provenance.

    :param records: Source records in one event
    :return: Primary outcome and all distinct outcomes
    """
    observed: List[tuple[Dict[str, Any], str]] = []
    outcomes: List[str] = []
    for record in records:
        outcome = _extract_outcome(record)
        if not outcome:
            continue
        observed.append((record, outcome))
        if outcome not in outcomes:
            outcomes.append(outcome)

    if not outcomes:
        return None, []
    if len(outcomes) == 1:
        return outcomes[0], outcomes

    compatibility = {
        _OUTCOME_COMPATIBILITY.get(outcome, outcome)
        for outcome in outcomes
    }
    if len(compatibility) == 1:
        for preferred_type in ("vote", "procedural_action"):
            for record, outcome in reversed(observed):
                if _normalize_text(record.get("record_type")) == preferred_type:
                    return outcome, outcomes
        return observed[-1][1], outcomes

    return "conflicting", outcomes


def _representative_timestamp(values: Sequence[float]) -> float | None:
    """
    Select an observed median timestamp for an event.

    Using the earliest timestamp lets one bad duplicate alignment pull an event
    link backward into the previous agenda item. The lower median remains an
    actual source timestamp, favors the start of a two-point sequence, and
    rejects isolated early or late outliers when several components agree.

    :param values: Observed source-record timestamps
    :return: Representative observed timestamp or ``None``
    """
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    return ordered[(len(ordered) - 1) // 2]


def _event_evidence(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Build deduplicated evidence entries with complete source references.

    Identical evidence text within one event is rendered once even when chunk
    overlap produced different timestamps. The entry keeps the earliest and
    latest accepted timestamps plus every contributing record index.

    :param records: Source records in one event
    :return: Evidence entries in source order
    """
    evidence_by_key: Dict[str, Dict[str, Any]] = {}
    evidence_timestamps: Dict[str, List[float]] = {}
    evidence_video_ids: Dict[str, set[str]] = {}

    for record in records:
        text = str(record.get("evidence", "")).strip()
        key = _normalize_text(text)
        item = evidence_by_key.get(key)
        if item is None:
            item = {
                "text": text,
                "source_record_indices": [],
            }
            evidence_by_key[key] = item
            evidence_timestamps[key] = []
            evidence_video_ids[key] = set()

        item["source_record_indices"].append(int(record["record_index"]))
        timestamp = record.get("timestamp")
        if isinstance(timestamp, (int, float)):
            evidence_timestamps[key].append(float(timestamp))
        video_id = record.get("video_id")
        if isinstance(video_id, str) and video_id:
            evidence_video_ids[key].add(video_id)

    for key, item in evidence_by_key.items():
        timestamps = evidence_timestamps[key]
        if timestamps:
            item["timestamp"] = min(timestamps)
            item["end_timestamp"] = max(timestamps)
        video_ids = evidence_video_ids[key]
        if len(video_ids) == 1:
            item["video_id"] = next(iter(video_ids))
    return list(evidence_by_key.values())


def _build_event(
    group: Sequence[RecordNode],
    *,
    event_number: int,
) -> Dict[str, Any]:
    """
    Convert one record-node group into a lossless event dictionary.

    :param group: Record nodes grouped into one event
    :param event_number: One-based event sequence number
    :return: Deterministic event dictionary
    """
    records = sorted(
        (dict(record) for node in group for record in node.records),
        key=lambda record: int(record["record_index"]),
    )
    primary = _primary_record(group)
    components = _component_indices(records)
    is_decision = bool(components)
    event_type = "decision" if is_decision else str(primary.get("record_type", "other"))
    outcome, outcome_values = _event_outcome(records)

    source_chunks = sorted(
        {chunk for record in records for chunk in _source_chunks(record)}
    )
    attributions: List[str] = []
    for record in records:
        attribution = record.get("attribution")
        if not isinstance(attribution, str):
            continue
        normalized = attribution.strip()
        if not normalized or normalized.lower() == "unknown":
            continue
        if normalized not in attributions:
            attributions.append(normalized)

    timestamps = [
        float(record["timestamp"])
        for record in records
        if isinstance(record.get("timestamp"), (int, float))
    ]
    video_ids = sorted(
        {
            str(record["video_id"])
            for record in records
            if isinstance(record.get("video_id"), str)
            and record.get("video_id")
        }
    )

    duplicate_rules = sorted(
        {rule for node in group for rule in node.rules}
    )
    grouping_method = "single_record"
    if len(group) > 1 and is_decision:
        grouping_method = "decision_sequence"
    elif duplicate_rules:
        grouping_method = "duplicate_consolidation"

    review_flags: List[str] = []
    if components.get("second") and not (
        components.get("motion") or components.get("vote")
    ):
        review_flags.append("unattached_second")
    if components.get("request") and not (
        components.get("motion") or components.get("vote")
    ):
        review_flags.append("unattached_motion_request")
    if outcome == "conflicting":
        review_flags.append("conflicting_outcomes")
    if len(video_ids) > 1:
        review_flags.append("mixed_video_ids")

    event = {
        "event_id": f"event_{event_number:04d}",
        "event_type": event_type,
        "subject": str(primary.get("subject", "")).strip(),
        "summary": str(primary.get("summary", "")).strip(),
        "grouping": {
            "method": grouping_method,
            "duplicate_rules": duplicate_rules,
        },
        "source_record_indices": [
            int(record["record_index"]) for record in records
        ],
        "source_chunks": source_chunks,
        "components": components,
        "evidence": _event_evidence(records),
        "source_records": records,
    }
    if outcome is not None:
        event["outcome"] = outcome
    if len(outcome_values) > 1:
        event["outcome_values"] = outcome_values
    if attributions:
        event["attributions"] = attributions
    representative_timestamp = _representative_timestamp(timestamps)
    primary_timestamp = primary.get("timestamp")
    if is_decision and isinstance(primary_timestamp, (int, float)):
        representative_timestamp = float(primary_timestamp)
    if representative_timestamp is not None:
        event["timestamp"] = representative_timestamp
        event["end_timestamp"] = max(
            representative_timestamp,
            max(timestamps, default=representative_timestamp),
        )
    if len(video_ids) == 1:
        event["video_id"] = video_ids[0]
    if review_flags:
        event["review_flags"] = review_flags
    return event


def _extract_meeting_date(*values: Any) -> str | None:
    """
    Extract an ISO meeting date from artifact paths or meeting names.

    :param values: Candidate strings containing a date
    :return: ISO ``YYYY-MM-DD`` value or ``None``
    """
    for value in values:
        text = str(value or "")
        for pattern in (_DATE_YMD_RE, _DATE_MDY_RE):
            match = pattern.search(text)
            if match is None:
                continue
            year = int(match.group("year"))
            if year < 100:
                year += 2000
            month = int(match.group("month"))
            day = int(match.group("day"))
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                continue
    return None


def build_events_artifact(
    records_artifact: Dict[str, Any],
    *,
    meeting: str | None = None,
    source_records_artifact: str | None = None,
    project_root: str | None = None,
) -> Dict[str, Any]:
    """
    Build one deterministic events artifact from a records artifact.

    The source artifact remains unchanged. Every record is retained inside one
    event's ``source_records`` list, including records hidden from standalone
    publication because they were attached as a second, request, or duplicate.

    :param records_artifact: Parsed complete records artifact
    :param meeting: Optional meeting identifier override
    :param source_records_artifact: Optional source artifact path
    :param project_root: Optional project root used for portable path metadata
    :return: Complete events artifact
    :raises ValidationError: If the records artifact is invalid
    """
    records = _validate_records_artifact(records_artifact)
    nodes = _deduplicate_records(records)
    groups = _group_decision_nodes(nodes)

    # Event IDs are assigned after source-order sorting so generation remains
    # deterministic even when a decision group skips over unrelated records.
    groups = sorted(groups, key=lambda group: min(node.first_index for node in group))
    events = [
        _build_event(group, event_number=index)
        for index, group in enumerate(groups, start=1)
    ]

    resolved_meeting = meeting or "meeting"
    if source_records_artifact:
        source_name = Path(source_records_artifact).name
        if source_name.endswith(".records.json"):
            resolved_meeting = source_name[: -len(".records.json")]

    duplicate_record_count = len(records) - len(nodes)
    decision_event_count = sum(event["event_type"] == "decision" for event in events)
    multi_record_event_count = sum(
        len(event["source_record_indices"]) > 1 for event in events
    )
    review_flag_counts: Dict[str, int] = {}
    for event in events:
        for flag in event.get("review_flags", []):
            review_flag_counts[flag] = review_flag_counts.get(flag, 0) + 1

    source_path = source_records_artifact
    if source_path and project_root:
        source_path = serialize_project_path(source_path, project_root)

    artifact = {
        "artifact_kind": EVENT_ARTIFACT_KIND,
        "schema_version": EVENT_SCHEMA_VERSION,
        "generation_version": EVENT_GENERATION_VERSION,
        "status": "complete",
        "meeting": resolved_meeting,
        "meeting_date": _extract_meeting_date(
            resolved_meeting,
            records_artifact.get("source_transcript"),
        ),
        "source_records_artifact": source_path,
        "source_transcript": records_artifact.get("source_transcript"),
        "source_sha256": records_artifact.get("source_sha256"),
        "meeting_type": records_artifact.get("meeting_type", "unknown"),
        "job_profile": records_artifact.get("job_profile"),
        "source_model": records_artifact.get("model"),
        "source_record_count": len(records),
        "event_count": len(events),
        "decision_event_count": decision_event_count,
        "multi_record_event_count": multi_record_event_count,
        "duplicate_record_count": duplicate_record_count,
        "review_flag_counts": review_flag_counts,
        "events": events,
    }
    return artifact



def _timestamp_markdown(event: Dict[str, Any]) -> str | None:
    """
    Build a timestamp display or timestamped YouTube link for an event.

    :param event: Event dictionary
    :return: Markdown timestamp or ``None``
    """
    timestamp = event.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        return None
    display = format_timestamp(float(timestamp))
    video_id = event.get("video_id")
    if isinstance(video_id, str) and _VIDEO_ID_RE.fullmatch(video_id):
        seconds = max(0, int(float(timestamp)))
        return (
            f"[{display}](https://www.youtube.com/watch?v={video_id}&t={seconds}s)"
        )
    return display


def _event_section(event_type: str) -> str:
    """
    Resolve the stable Markdown section for an event type.

    :param event_type: Event type
    :return: Human-readable section heading
    """
    return _EVENT_TYPE_SECTIONS.get(event_type, "Other")


def render_events_markdown(artifact: Dict[str, Any]) -> str:
    """
    Render one events artifact as deterministic publication-oriented Markdown.

    :param artifact: Events artifact
    :return: Markdown document
    :raises ValidationError: If the artifact lacks an events list
    """
    events = artifact.get("events")
    if not isinstance(events, list):
        raise ValidationError("Events artifact must contain an events list")

    grouped: Dict[str, List[Dict[str, Any]]] = {
        section: [] for section in _EVENT_SECTION_ORDER
    }
    for event in events:
        if not isinstance(event, dict):
            continue
        grouped[_event_section(str(event.get("event_type", "other")))].append(event)

    lines = [
        "# Meeting Events",
        "",
        f"Meeting: `{artifact.get('meeting', 'unknown')}`",
        "",
        f"Meeting date: `{artifact.get('meeting_date') or 'unknown'}`",
        "",
        f"Meeting type: `{artifact.get('meeting_type', 'unknown')}`",
        "",
        "This is a deterministic derived artifact, not official meeting minutes. "
        "Related records may be grouped for review while complete machine "
        "provenance remains in the JSON artifact.",
        "",
    ]

    for section in _EVENT_SECTION_ORDER:
        section_events = grouped[section]
        if not section_events:
            continue
        lines.extend([f"## {section}", ""])
        for event in section_events:
            subject = format_markdown_inline(event.get("subject", "Untitled event"))
            summary = format_markdown_inline(event.get("summary", ""))
            outcome = str(event.get("outcome", "")).strip()
            attributions = event.get("attributions", [])
            heading = f"- **{subject}**"
            if outcome and outcome != "conflicting":
                heading += f" — {outcome.replace('_', ' ').title()}"
            lines.append(heading)
            if summary:
                lines.append(f"  - **Summary:** {summary}")
            if outcome == "conflicting":
                rendered = ", ".join(
                    format_markdown_inline(value)
                    for value in event.get("outcome_values", [])
                )
                lines.append(f"  - Outcome requires review: {rendered}")
            if isinstance(attributions, list) and attributions:
                rendered_attributions = ", ".join(
                    format_markdown_inline(value) for value in attributions
                )
                lines.append(f"  - **Attribution:** {rendered_attributions}")
            evidence = event.get("evidence", [])
            if isinstance(evidence, list):
                for item in evidence:
                    if not isinstance(item, dict):
                        continue
                    text = format_markdown_inline(item.get("text", ""))
                    if text:
                        lines.append(f"  - **Evidence:** “{text}”")
            flags = event.get("review_flags", [])
            if isinstance(flags, list) and flags:
                lines.append(
                    "  - **Review flags:** "
                    + ", ".join(format_markdown_inline(flag) for flag in flags)
                )
            timestamp = _timestamp_markdown(event)
            if timestamp:
                lines.append(f"  - **Source timestamp:** {timestamp}")
            lines.append("")

    if not any(grouped.values()):
        lines.extend(["No events were generated.", ""])
    return "\n".join(lines).strip() + "\n"


def write_events_artifacts(
    artifact: Dict[str, Any],
    *,
    json_path: str,
    markdown_path: str,
) -> None:
    """
    Atomically write one meeting's JSON and Markdown event artifacts.

    :param artifact: Events artifact
    :param json_path: JSON destination
    :param markdown_path: Markdown destination
    :return: None
    """
    write_text_atomic(
        json_path,
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
    )
    write_text_atomic(markdown_path, render_events_markdown(artifact))


def _discover_records_artifacts(
    records_dir: str,
    *,
    meeting_filter: str | None,
) -> List[tuple[str, str]]:
    """
    Discover complete records artifact paths beneath a model directory.

    :param records_dir: Model-specific records root
    :param meeting_filter: Optional case-insensitive meeting substring
    :return: ``(meeting, artifact_path)`` pairs in stable order
    :raises ValidationError: If the records directory does not exist
    """
    root = Path(records_dir)
    if not root.is_dir():
        raise ValidationError(f"Records directory does not exist: {records_dir}")

    normalized_filter = _normalize_text(meeting_filter)
    discovered: List[tuple[str, str]] = []
    for child in sorted(root.iterdir(), key=lambda path: path.name.lower()):
        if not child.is_dir():
            continue
        meeting = child.name
        if normalized_filter and normalized_filter not in meeting.lower():
            continue
        artifact_path = child / f"{meeting}.records.json"
        if artifact_path.is_file():
            discovered.append((meeting, str(artifact_path)))
    return discovered


def _load_records_artifact(path: str) -> Dict[str, Any]:
    """
    Read and parse one records artifact with a contextual error.

    :param path: Records artifact path
    :return: Parsed JSON object
    :raises ConfigError: If the file cannot be read
    :raises ValidationError: If the JSON is invalid or not an object
    """
    try:
        parsed = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid records JSON '{path}': {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError(f"Records artifact '{path}' must contain an object")
    return parsed


def _meeting_output_paths(output_dir: str, meeting: str) -> Dict[str, str]:
    """
    Resolve safe output paths for one meeting's event artifacts.

    :param output_dir: Events output root
    :param meeting: Meeting identifier
    :return: Meeting directory and artifact paths
    """
    safe_meeting = sanitize_path_component(meeting)
    meeting_dir = os.path.join(output_dir, safe_meeting)
    return {
        "meeting_dir": meeting_dir,
        "json_path": os.path.join(meeting_dir, f"{safe_meeting}{EVENTS_JSON_SUFFIX}"),
        "markdown_path": os.path.join(
            meeting_dir,
            f"{safe_meeting}{EVENTS_MARKDOWN_SUFFIX}",
        ),
    }


def _write_indexes(
    *,
    output_dir: str,
    entity: str,
    model: str,
    results: Sequence[Dict[str, Any]],
    project_root: str,
) -> tuple[str, str]:
    """
    Write aggregate meeting and event indexes for downstream publication.

    :param output_dir: Events output root
    :param entity: Entity handle
    :param model: Source records model
    :param results: Per-meeting generation results
    :param project_root: Project root for portable paths
    :return: ``(events_jsonl_path, meetings_json_path)``
    """
    meetings: List[Dict[str, Any]] = []
    event_lines: List[str] = []

    for result in results:
        artifact = result["artifact"]
        meeting_entry = {
            "meeting": artifact["meeting"],
            "meeting_date": artifact.get("meeting_date"),
            "meeting_type": artifact.get("meeting_type", "unknown"),
            "source_record_count": artifact["source_record_count"],
            "event_count": artifact["event_count"],
            "decision_event_count": artifact["decision_event_count"],
            "duplicate_record_count": artifact["duplicate_record_count"],
            "review_flag_counts": artifact.get("review_flag_counts", {}),
            "events_artifact": serialize_project_path(
                result["json_path"],
                project_root,
            ),
            "events_markdown": serialize_project_path(
                result["markdown_path"],
                project_root,
            ),
            "source_records_artifact": artifact.get("source_records_artifact"),
        }
        meetings.append(meeting_entry)

        for event in artifact["events"]:
            indexed = {
                "entity": entity,
                "meeting": artifact["meeting"],
                "meeting_date": artifact.get("meeting_date"),
                "meeting_type": artifact.get("meeting_type", "unknown"),
                **event,
            }
            event_lines.append(json.dumps(indexed, ensure_ascii=False, sort_keys=True))

    events_jsonl_path = os.path.join(output_dir, EVENTS_JSONL_NAME)
    meetings_json_path = os.path.join(output_dir, MEETINGS_JSON_NAME)
    meetings_index = {
        "artifact_kind": EVENT_INDEX_ARTIFACT_KIND,
        "schema_version": EVENT_INDEX_SCHEMA_VERSION,
        "entity": entity,
        "source_model": model,
        "meeting_count": len(meetings),
        "event_count": sum(item["event_count"] for item in meetings),
        "decision_event_count": sum(
            item["decision_event_count"] for item in meetings
        ),
        "meetings": meetings,
    }

    write_text_atomic(
        events_jsonl_path,
        ("\n".join(event_lines) + "\n") if event_lines else "",
    )
    write_text_atomic(
        meetings_json_path,
        json.dumps(meetings_index, indent=2, ensure_ascii=False) + "\n",
    )
    return events_jsonl_path, meetings_json_path


def run_events_stage(
    global_config_path: str,
    *,
    entity: str,
    model: str | None = None,
    output_dir: str | None = None,
    meeting_filter: str | None = None,
) -> Dict[str, Any]:
    """
    Generate deterministic event artifacts for an entity's records corpus.

    The stage reads complete records artifacts, writes a separate model-specific
    events tree, and creates aggregate ``events.jsonl`` and ``meetings.json``
    indexes. No provider, embedding model, or network access is used.

    :param global_config_path: Path to global yatsee.toml
    :param entity: Entity handle
    :param model: Optional records model override
    :param output_dir: Optional events output directory override
    :param meeting_filter: Optional meeting-directory substring
    :return: Stage summary and per-meeting results
    :raises ValidationError: If no records artifacts are available
    """
    resolved = resolve_event_paths(
        global_config_path,
        entity=entity,
        model=model,
        output_dir=output_dir,
    )
    project_root = str(Path(global_config_path).resolve().parent)
    ensure_directory(resolved["output_dir"])

    discovered = _discover_records_artifacts(
        resolved["records_dir"],
        meeting_filter=meeting_filter,
    )
    if not discovered:
        raise ValidationError(
            f"No complete records artifacts found in: {resolved['records_dir']}"
        )

    results: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    for meeting, records_path in discovered:
        try:
            records_artifact = _load_records_artifact(records_path)
            artifact = build_events_artifact(
                records_artifact,
                meeting=meeting,
                source_records_artifact=records_path,
                project_root=project_root,
            )
            paths = _meeting_output_paths(resolved["output_dir"], meeting)
            ensure_directory(paths["meeting_dir"])
            write_events_artifacts(
                artifact,
                json_path=paths["json_path"],
                markdown_path=paths["markdown_path"],
            )
            results.append(
                {
                    "meeting": meeting,
                    "records_path": records_path,
                    "json_path": paths["json_path"],
                    "markdown_path": paths["markdown_path"],
                    "artifact": artifact,
                }
            )
        except (ConfigError, ValidationError) as exc:
            skipped.append({"meeting": meeting, "reason": str(exc)})

    if not results:
        reasons = "; ".join(
            f"{item['meeting']}: {item['reason']}" for item in skipped
        )
        raise ValidationError(f"No event artifacts were generated. {reasons}")

    events_jsonl_path, meetings_json_path = _write_indexes(
        output_dir=resolved["output_dir"],
        entity=entity,
        model=resolved["model"],
        results=results,
        project_root=project_root,
    )

    return {
        "artifact_kind": EVENT_ARTIFACT_KIND,
        "schema_version": EVENT_SCHEMA_VERSION,
        "entity": entity,
        "model": resolved["model"],
        "records_dir": resolved["records_dir"],
        "output_dir": resolved["output_dir"],
        "discovered": len(discovered),
        "processed": len(results),
        "skipped": len(skipped),
        "source_record_count": sum(
            result["artifact"]["source_record_count"] for result in results
        ),
        "event_count": sum(
            result["artifact"]["event_count"] for result in results
        ),
        "decision_event_count": sum(
            result["artifact"]["decision_event_count"] for result in results
        ),
        "duplicate_record_count": sum(
            result["artifact"]["duplicate_record_count"] for result in results
        ),
        "multi_record_event_count": sum(
            result["artifact"]["multi_record_event_count"] for result in results
        ),
        "events_jsonl_path": events_jsonl_path,
        "meetings_json_path": meetings_json_path,
        "results": results,
        "skipped_results": skipped,
    }
