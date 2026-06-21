"""
Rules-based meeting signal extraction for YATSEE.

This module builds a deterministic Markdown "Meeting Signals" artifact from
normalized transcript text. It is intentionally generic: city councils, county
boards, committees, townships, and other public meetings all flow through the
same evidence pipeline.

The extractor does not verify claims, prove absence, resolve disputes, or treat
ASR output as fact. It surfaces candidate evidence lines for downstream review
and for optional LLM-assisted civic record generation.

The important boundary is intentional:
- this module emits deterministic meeting signals
- a separate record-generation pass should produce the final civic record
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from yatsee.core.config import load_entity_config, load_global_config
from yatsee.core.discovery import discover_files
from yatsee.core.errors import ConfigError, ValidationError

SUPPORTED_INPUT_EXTENSIONS = (".txt",)
DEFAULT_OUTPUT_DIRNAME = "meeting_signals"
OUTPUT_SUFFIX = ".signals.md"
ARTIFACT_KIND = "meeting_signals"
ARTIFACT_SCHEMA_VERSION = "experimental_meeting_signals_v1"

MAX_ACTIONS = 40
MAX_MONEY = 40
MAX_CIVIC_OBJECTS = 45
MAX_QUESTIONS = 30
MAX_LOW_CONFIDENCE = 25
MAX_ROLL_CALLS = 25
MAX_PEOPLE = 40

RESULT_PATTERNS = (
    r"\bmotion\s+(?:carries|carried|passes|passed|fails|failed)\b",
    r"\b(?:passes|passed|adopted|approved|failed)\s+\d+\s*(?:-|to)\s*\d+\b",
    r"\b(?:approved|passes|adopted)\s+unanimously\b",
    r"\bthat\s+(?:is\s+)?(?:approved|adopted|passed)\b",
    r"\bthat\s+motion\s+(?:passes|passed|fails|failed)\b",
    r"\bthe\s+(?:resolution|ordinance|motion)\s+is\s+(?:adopted|approved|passed|failed)\b",
    r"\bresolution\s+(?:is\s+)?(?:adopted|approved|passed|failed)\b",
    r"\bordinance\s+(?:is\s+)?(?:adopted|approved|passed|failed)\b",
    r"\bdoes\s+not\s+pass\b",
    r"\bfails?\s+for\s+lack\s+of\b",
)

VOICE_VOTE_PATTERNS = (
    r"\ball\s+(?:those\s+)?in\s+favor\b",
    r"\bsame\s+sign\b",
    r"\bopposed\??\b",
)

ACTION_PATTERNS = (
    r"\b(?:approval|adoption)\s+of\b",
    r"\bapprove\s+(?:the\s+)?(?:agenda|minutes|claims|bills|payables?)\b",
    r"\b(?:motion|move|moved)\s+(?:to|for)\s+",
    r"\bdo\s+i\s+have\s+a\s+motion\b",
    r"\bis\s+there\s+a\s+motion\b",
    r"\b(?:first|second|final)\s+reading\b",
    r"\bstaff\s+recommends?\s+(?:approval|moving\s+forward|to\s+approve)\b",
    r"\broll\s+call\s+vote\b",
)

CIVIC_OBJECT_PATTERNS: Dict[str, Tuple[str, ...]] = {
    "ordinance": (
        r"\bordinance\s+(?:no\.?\s*)?\d{2,4}[-– ]\d+\b",
        r"\b(?:first|second|final)\s+reading\s+of\s+ordinance\b",
        r"\bordinance\s+(?:approving|amending|authorizing|granting|abating|establishing)\b",
    ),
    "resolution": (
        r"\bresolution\s+(?:no\.?\s*)?\d{2,4}[-– ]\d+\b",
        r"\b(?:adoption|approval)\s+of\s+resolution\b",
        r"\bresolution\s+(?:approving|authorizing|ratifying|establishing|amending|declaring)\b",
    ),
    "agreement": (
        r"\b(?:contract|settlement agreement|lease agreement|payment agreement)\b",
        r"\bintergovernmental agreement\b",
        r"\bservice provider agreement\b",
        r"\bagreement\s+(?:with|between)\s+(?:the\s+)?(?:city|county|township|village|state|department|board|vendor|contractor|provider)\b",
        r"\bmemorandum\s+of\s+(?:agreement|understanding)\b",
        r"\bMOU\b",
        r"\bcollective\s+bargaining\b",
    ),
    "procurement": (
        r"\bRFP\b",
        r"\bRFQ\b",
        r"\bbid(?:s|ding)?\b",
        r"\blow\s+bidder\b",
        r"\bvendor\s+selection\b",
    ),
    "permit_zoning": (
        r"\bspecial\s+use\s+permit\b",
        r"\btemporary\s+use\s+permit\b",
        r"\bzoning\s+map\s+amendment\b",
        r"\brezon(?:e|ing)\b",
        r"\bvariance\s+application\b",
        r"\bzoning\s+ordinance\b",
    ),
    "tax_bond": (
        r"\btax\s+levy\b",
        r"\bcorporate\s+levy\b",
        r"\bbond\s+abatement\b",
        r"\bgeneral\s+obligation\s+bonds?\b",
        r"\bPTEL\b",
    ),
    "executive_session": (
        r"\bexecutive\s+session\b",
        r"\bclosed\s+session\b",
        r"\b\d+\s*ILCS\b",
    ),
    "compliance": (
        r"\bADA\s+compliance\b",
        r"\bADA\s+compliant\b",
        r"\bNPDES\s+permit\b",
        r"\bIEPA\b",
        r"\bEPA\s+permit\b",
        r"\bPFAS\b",
    ),
    "grant_program": (
        r"\bHUD\b",
        r"\bCDBG\b",
        r"\bgrant\b",
    ),
}

CIVIC_OBJECT_LABELS = {
    "ordinance": "Ordinance Reference",
    "resolution": "Resolution Reference",
    "agreement": "Contract / Agreement Reference",
    "procurement": "Procurement Reference",
    "permit_zoning": "Permit / Zoning Reference",
    "tax_bond": "Tax / Levy / Bond Reference",
    "executive_session": "Executive Session Reference",
    "compliance": "Compliance Reference",
    "grant_program": "Grant / Program Reference",
}

LOW_VALUE_LINES = {
    "aye",
    "yes",
    "no",
    "nay",
    "present",
    "opposed",
    "same sign",
    "all in favor",
    "all those in favor",
    "thank you",
    "thanks",
    "none",
    "seeing none",
    "any questions",
    "anything else",
}

SHORT_GENERIC_QUESTIONS = {
    "what",
    "what else",
    "questions",
    "any questions",
    "anything",
    "anything else",
    "correct",
    "second",
    "all opposed",
    "any discussion",
    "any discussions",
    "do we have a second",
}

PROCEDURAL_QUESTION_PATTERNS = (
    r"\btake\s+the\s+roll\b",
    r"\btake\s+the\s+role\b",
    r"\bplease\s+read\s+this\b",
    r"\bcould\s+you\s+please\s+read\b",
    r"\bleading?\s+the\s+pledge\b",
    r"\bgive\s+the\s+invocation\b",
    r"\bplease\s+give\s+the\s+invocation\b",
    r"\bis\s+there\s+a\s+motion\b",
    r"\bdo\s+i\s+have\s+a\s+motion\b",
    r"\bcan\s+i\s+get\s+a\s+motion\b",
    r"\bmay\s+i\s+be\s+recognized\b",
    r"\bany\s+questions?\b",
    r"\bdiscussion\s+on\s+(?:this\s+)?(?:ordinance|resolution)\b",
)

QUESTION_CONTEXT_PATTERN = re.compile(
    r"\b("
    r"cost|fund|funding|budget|grant|invoice|payment|payroll|bid|rfp|rfq|quote|"
    r"contract|agreement|permit|ordinance|resolution|policy|tax|levy|rate|"
    r"road|bridge|street|sewer|water|utility|property|zoning|project|program|"
    r"staff|department|board|committee|district|county|city|township|village|"
    r"service|public|data|report|timeline|deadline|renewal|fee|purchase|"
    r"equipment|vehicle|building|facility|compliance|epa|iepa|ada"
    r")\b",
    re.IGNORECASE,
)

MONEY_CONTEXT_PATTERN = re.compile(
    r"\b("
    r"bill|bills|payable|payables|payroll|claim|claims|invoice|payment|paid|"
    r"cost|price|bid|quote|rfp|rfq|grant|fund|funding|budget|appropriation|"
    r"revenue|expense|expenditure|settlement|contract|agreement|purchase|"
    r"tax|levy|rate|fee|fees|bond|loan|audit|treasurer|reimbursement|"
    r"donation|award|awarded|insurance|salary|wage|overtime"
    r")\b",
    re.IGNORECASE,
)

PERSON_ROLE_PATTERN = re.compile(
    r"\b(?:Mr\.?|Ms\.?|Mrs\.?|Mayor|Alderman|Alderperson|Manager|Director|Chief|Clerk|Attorney|Administrator|Treasurer|Assessor|Sheriff|Chairman|Chair|Trustee|Supervisor)\s+"
    r"[A-Z][A-Za-z'-]{1,30}(?:\s+[A-Z][A-Za-z'-]{1,30})?\b"
)

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9$.'-]+")
DOLLAR_AMOUNT_PATTERN = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:million|billion|thousand))?",
    re.IGNORECASE,
)
REPEATED_WORD_PATTERN = re.compile(r"\b(\w+)\b(?:[,.]?\s+\1\b){3,}", re.IGNORECASE)
ROLL_CALL_NAME_PATTERN = re.compile(r"^((?:And\s+)?[A-Z][A-Za-z' -]{1,36})\?$")
ROLL_CALL_VOTE_PATTERN = re.compile(r"^(Aye|Yes|No|Nay|Present|Abstain)\.?$", re.IGNORECASE)


@dataclass(frozen=True)
class LineRecord:
    """
    Normalized transcript line with convenience fields.

    Numbering preserves both the source file line number and the compact
    non-empty record number. The rendered Markdown does not currently expose
    either number, but keeping both avoids misleading downstream consumers.

    :param number: One-based non-empty record number in the normalized transcript
    :param source_line_number: One-based source file line number
    :param text: Whitespace-normalized line text
    :param lowered: Lowercase line text for matching
    :param tokens: Simple word tokens derived from the line
    """

    number: int
    source_line_number: int
    text: str
    lowered: str
    tokens: Tuple[str, ...]


@dataclass(frozen=True)
class Signal:
    """
    Candidate deterministic signal emitted by the extractor.

    :param kind: Signal class such as action, civic_object, or question
    :param label: Human-readable label
    :param evidence: Source transcript evidence
    :param line_number: One-based source line number
    :param confidence: Confidence string rendered downstream
    :param result: Nearby result text when applicable
    """

    kind: str
    label: str
    evidence: str
    line_number: int
    confidence: str = "detected"
    result: str = ""


@dataclass(frozen=True)
class RollCall:
    """
    Detected roll-call vote block.

    :param context: Nearby source context
    :param votes: Name/vote pairs
    :param result: Nearby result text, if found
    :param start_line: One-based source starting line
    """

    context: str
    votes: List[Tuple[str, str]]
    result: str
    start_line: int


@dataclass(frozen=True)
class MoneyReference:
    """
    Transcript-derived money reference.

    :param amount: Dollar amount as written in the transcript
    :param context: Source line containing the amount
    :param line_number: One-based source line number
    """

    amount: str
    context: str
    line_number: int


@dataclass
class ExtractedSignals:
    """
    Deterministic signal extract for one normalized transcript.

    :param base_name: Source transcript basename without extension
    :param actions: Action-like candidate lines
    :param roll_calls: Detected roll-call blocks
    :param money: Money reference lines
    :param civic_objects: Civic object reference lines
    :param questions: Candidate question lines
    :param low_confidence: Lines likely needing transcript review
    :param people: Possible people detected internally for metrics
    """

    base_name: str
    actions: List[Signal] = field(default_factory=list)
    roll_calls: List[RollCall] = field(default_factory=list)
    money: List[MoneyReference] = field(default_factory=list)
    civic_objects: List[Signal] = field(default_factory=list)
    questions: List[str] = field(default_factory=list)
    low_confidence: List[str] = field(default_factory=list)
    people: List[str] = field(default_factory=list)


def load_text(path: str) -> str:
    """
    Read a UTF-8 text file.

    :param path: File path to read
    :return: File contents
    :raises ConfigError: When the file cannot be read
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise ConfigError(f"Failed to read transcript '{path}': {exc}") from exc


def write_text(path: str, content: str) -> None:
    """
    Write UTF-8 text to disk.

    :param path: File path to write
    :param content: Content to write
    :raises ConfigError: When the file cannot be written
    """
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
    except OSError as exc:
        raise ConfigError(f"Failed to write meeting signals '{path}': {exc}") from exc


def normalize_line(line: str) -> str:
    """
    Collapse whitespace and trim a line.

    :param line: Source line
    :return: Normalized line
    """
    return re.sub(r"\s+", " ", line).strip()


def split_lines(text: str) -> List[str]:
    """
    Return non-empty normalized lines.

    This helper preserves the previous public behavior for callers that only
    need line text. Use make_line_records() when source line numbers matter.

    :param text: Transcript text
    :return: Normalized non-empty lines
    """
    return [clean for line in text.splitlines() if (clean := normalize_line(line))]


def make_line_records(text: str) -> List[LineRecord]:
    """
    Convert transcript text into normalized line records.

    Blank source lines are skipped for extraction, but source file line numbers
    are preserved on each emitted record for downstream traceability.

    :param text: Transcript text
    :return: Line records with one-based numbering
    """
    records: List[LineRecord] = []
    record_number = 0

    for source_line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = normalize_line(raw_line)
        if not line:
            continue

        record_number += 1
        lowered = line.lower()
        records.append(
            LineRecord(
                number=record_number,
                source_line_number=source_line_number,
                text=line,
                lowered=lowered,
                tokens=tuple(TOKEN_PATTERN.findall(lowered)),
            )
        )

    return records


def unique_preserve_order(items: Iterable[str]) -> List[str]:
    """
    Deduplicate strings while preserving first-seen order.

    :param items: Candidate strings
    :return: Deduplicated normalized strings
    """
    seen = set()
    output: List[str] = []
    for item in items:
        clean = normalize_line(str(item))
        key = clean.lower()
        if clean and key not in seen:
            output.append(clean)
            seen.add(key)
    return output


def truncate_line(line: str, limit: int = 240) -> str:
    """
    Trim long evidence lines for readable Markdown.

    :param line: Source line
    :param limit: Maximum rendered length
    :return: Possibly truncated line
    """
    clean = normalize_line(line)
    return clean if len(clean) <= limit else clean[: limit - 3].rstrip() + "..."


def md_escape_table(value: str) -> str:
    """
    Escape a Markdown table cell.

    :param value: Cell value
    :return: Escaped value
    """
    return normalize_line(value).replace("|", "\\|")


def matches_any(text: str, patterns: Sequence[str]) -> bool:
    """
    Return true when text matches any regular expression.

    :param text: Text to inspect
    :param patterns: Regex patterns
    :return: True when any pattern matches
    """
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def is_low_value_line(line: str) -> bool:
    """
    Detect bare procedural or vote-token lines.

    :param line: Source line
    :return: True when the line has little standalone signal value
    """
    lowered = normalize_line(line).strip(" .?").lower()
    return not lowered or lowered in LOW_VALUE_LINES


def nearby_context(records: Sequence[LineRecord], index: int, before: int = 2) -> str:
    """
    Return short context before a line.

    :param records: Line records
    :param index: Zero-based line index
    :param before: Number of prior lines to include
    :return: Joined context text
    """
    start = max(0, index - before)
    return truncate_line(" ".join(record.text for record in records[start : index + 1]), 220)


def find_nearby_result(records: Sequence[LineRecord], start_index: int, window: int = 14) -> str:
    """
    Find a nearby result or vote outcome after an action-like signal.

    :param records: Line records
    :param start_index: Zero-based starting index
    :param window: Forward line window
    :return: Result evidence or empty string
    """
    end = min(len(records), start_index + window + 1)
    for record in records[start_index + 1 : end]:
        if matches_any(record.text, RESULT_PATTERNS):
            return truncate_line(record.text, 200)
    for record in records[start_index + 1 : end]:
        if matches_any(record.text, VOICE_VOTE_PATTERNS):
            return truncate_line(record.text, 160)
    return ""


def label_action(line: str) -> str:
    """
    Assign a generic action signal label.

    The labels remain deliberately broad so this deterministic stage does not
    become a meeting-type-specific semantic classifier.

    :param line: Source line
    :return: Generic action label
    """
    lowered = normalize_line(line).lower()
    if "adjourn" in lowered:
        return "Adjournment Signal"
    if re.search(r"\bclaims?|bills?|payables?\b", lowered) and re.search(r"\bapprove|approval|motion\b", lowered):
        return "Claims / Payment Signal"
    if "ordinance" in lowered:
        return "Ordinance Action Signal"
    if "resolution" in lowered:
        return "Resolution Action Signal"
    if re.search(r"\bRFP\b|\bRFQ\b|\bbid", line, re.IGNORECASE):
        return "Procurement Action Signal"
    if re.search(r"\bcontract|agreement|MOU|memorandum\b", line, re.IGNORECASE):
        return "Agreement Action Signal"
    if "motion" in lowered or "moved" in lowered:
        return "Motion / Vote Signal"
    if "approval" in lowered or "approve" in lowered:
        return "Approval Signal"
    return "Action-Like Signal"


def extract_action_signals(records: Sequence[LineRecord]) -> List[Signal]:
    """
    Extract generic action, approval, motion, or vote-like signals.

    :param records: Line records
    :return: Action-like signals
    """
    signals: List[Signal] = []
    seen = set()

    for index, record in enumerate(records):
        if is_low_value_line(record.text):
            continue
        if not matches_any(record.text, ACTION_PATTERNS):
            continue

        label = label_action(record.text)
        result = find_nearby_result(records, index)
        key = (label.lower(), record.text.lower())
        if key in seen:
            continue

        seen.add(key)
        signals.append(
            Signal(
                kind="action",
                label=label,
                evidence=truncate_line(record.text, 240),
                line_number=record.source_line_number,
                confidence="detected",
                result=result,
            )
        )

    return signals[:MAX_ACTIONS]


def extract_roll_calls(records: Sequence[LineRecord]) -> List[RollCall]:
    """
    Extract simple name/vote roll-call blocks.

    :param records: Line records
    :return: Roll-call signal blocks
    """
    calls: List[RollCall] = []
    index = 0

    while index < len(records) - 1:
        start = index
        votes: List[Tuple[str, str]] = []

        while index < len(records) - 1:
            name_match = ROLL_CALL_NAME_PATTERN.match(records[index].text)
            vote_match = ROLL_CALL_VOTE_PATTERN.match(records[index + 1].text)
            if not name_match or not vote_match:
                break

            name = re.sub(r"^And\s+", "", name_match.group(1), flags=re.IGNORECASE).strip().title()
            votes.append((name, vote_match.group(1).title()))
            index += 2

        if len(votes) >= 3:
            result = find_nearby_result(records, max(index - 1, 0), 8)
            calls.append(
                RollCall(
                    nearby_context(records, start),
                    votes,
                    result,
                    records[start].source_line_number,
                )
            )
        else:
            index = start + 1

    return calls[:MAX_ROLL_CALLS]


def extract_money_references(records: Sequence[LineRecord]) -> List[MoneyReference]:
    """
    Extract dollar references with generic civic-finance context.

    A dollar amount alone is not enough to become a signal. This avoids
    promoting anecdotes, garbled oath text, or casual discussion into civic
    finance evidence.

    :param records: Line records
    :return: Money references
    """
    results: List[MoneyReference] = []
    seen = set()

    for index, record in enumerate(records):
        context = nearby_context(records, index, before=2)

        if not MONEY_CONTEXT_PATTERN.search(context):
            continue

        for match in DOLLAR_AMOUNT_PATTERN.finditer(record.text):
            local_context = record.text[match.start() : min(len(record.text), match.end() + 8)]
            if re.search(r",\s+\d", local_context):
                continue

            amount = normalize_line(match.group(0).replace("$ ", "$"))
            key = (amount.lower(), context.lower())

            if key in seen:
                continue

            seen.add(key)
            results.append(
                MoneyReference(
                    amount,
                    truncate_line(context, 260),
                    record.source_line_number,
                )
            )

    return results[:MAX_MONEY]


def match_civic_object(line: str) -> Tuple[str, str]:
    """
    Match a durable civic object reference in a line.

    :param line: Source line
    :return: Tuple of object key and label, or empty strings
    """
    for key, patterns in CIVIC_OBJECT_PATTERNS.items():
        if matches_any(line, patterns):
            return key, CIVIC_OBJECT_LABELS[key]
    return "", ""


def extract_civic_object_signals(records: Sequence[LineRecord]) -> List[Signal]:
    """
    Extract ordinance, resolution, contract, procurement, permit, and similar signals.

    :param records: Line records
    :return: Civic object signals
    """
    signals: List[Signal] = []
    seen = set()

    for record in records:
        if is_low_value_line(record.text):
            continue

        key, label = match_civic_object(record.text)
        if not key:
            continue

        dedupe_key = (label.lower(), record.text.lower())
        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        signals.append(
            Signal(
                kind=key,
                label=label,
                evidence=truncate_line(record.text, 280),
                line_number=record.source_line_number,
            )
        )

    return signals[:MAX_CIVIC_OBJECTS]


def is_useful_question(line: str) -> bool:
    """
    Keep substantive civic questions and reject meeting-procedure prompts.

    This is intentionally generic. It filters common meeting mechanics without
    encoding entity-specific terms.

    :param line: Source line
    :return: True when question is worth surfacing
    """
    clean = normalize_line(line).strip(" .?")
    lowered = clean.lower()

    if lowered in SHORT_GENERIC_QUESTIONS:
        return False

    if matches_any(clean, PROCEDURAL_QUESTION_PATTERNS):
        return False

    return QUESTION_CONTEXT_PATTERN.search(clean) is not None


def extract_question_signals(records: Sequence[LineRecord]) -> List[str]:
    """
    Extract substantive question lines.

    Questions are signals only when they are explicit questions and contain
    generic civic context.

    :param records: Line records
    :return: Question evidence strings
    """
    items: List[str] = []

    for record in records:
        if "?" not in record.text:
            continue
        if is_low_value_line(record.text):
            continue
        if is_useful_question(record.text):
            items.append(truncate_line(record.text, 260))

    return unique_preserve_order(items)[:MAX_QUESTIONS]


def extract_low_confidence_items(records: Sequence[LineRecord]) -> List[str]:
    """
    Extract likely ASR-damaged fragments, not ordinary questions.

    :param records: Line records
    :return: Low-confidence evidence strings
    """
    items: List[str] = []

    for record in records:
        line = record.text
        lowered = record.lowered
        if is_low_value_line(line):
            continue
        if REPEATED_WORD_PATTERN.search(line) or any(
            marker in lowered for marker in ("this is this is", "78. 78", "lorem ipsum")
        ):
            items.append(truncate_line(line, 260))
        elif len(line) > 260 and ("?" in line or "..." in line):
            items.append(truncate_line(line, 260))

    return unique_preserve_order(items)[:MAX_LOW_CONFIDENCE]


def configured_string_values(value: Any) -> List[str]:
    """
    Extract explicit string values from config-like structures.

    :param value: Config value
    :return: String values
    """
    output: List[str] = []

    if isinstance(value, str):
        output.append(value)
    elif isinstance(value, list):
        for item in value:
            output.extend(configured_string_values(item))
    elif isinstance(value, dict):
        for item in value.values():
            output.extend(configured_string_values(item))

    return [clean for item in output if (clean := normalize_line(item))]


def extract_people(records: Sequence[LineRecord], entity_cfg: Dict[str, Any]) -> List[str]:
    """
    Extract conservative possible people references for internal QA counts.

    :param records: Line records
    :param entity_cfg: Entity configuration
    :return: Possible people/speaker names
    """
    candidates: List[str] = []
    full_text = "\n".join(record.text for record in records).lower()

    people_cfg = entity_cfg.get("people", {}) if isinstance(entity_cfg, dict) else {}
    for item in configured_string_values(people_cfg):
        if len(item.split()) >= 2 and item.lower() in full_text:
            candidates.append(item)

    participants_cfg = entity_cfg.get("participants", {}) if isinstance(entity_cfg, dict) else {}
    for item in configured_string_values(participants_cfg):
        if len(item.split()) >= 2 and item.lower() in full_text:
            candidates.append(item)

    for record in records:
        for match in PERSON_ROLE_PATTERN.finditer(record.text):
            candidates.append(match.group(0).strip())

    return unique_preserve_order(candidates)[:MAX_PEOPLE]


def extract_meeting_signals(
    *,
    transcript_text: str,
    base_name: str,
    entity_cfg: Dict[str, Any],
) -> ExtractedSignals:
    """
    Extract generic deterministic meeting signals.

    This function coordinates the deterministic signal extractors for one transcript.

    :param transcript_text: Normalized transcript text
    :param base_name: Source transcript basename without extension
    :param entity_cfg: Entity configuration
    :return: Extracted signal record
    """
    records = make_line_records(transcript_text)
    return ExtractedSignals(
        base_name=base_name,
        actions=extract_action_signals(records),
        roll_calls=extract_roll_calls(records),
        money=extract_money_references(records),
        civic_objects=extract_civic_object_signals(records),
        questions=extract_question_signals(records),
        low_confidence=extract_low_confidence_items(records),
        people=extract_people(records, entity_cfg),
    )


def render_limited_list(
    items: Sequence[str],
    max_items: int,
    empty_text: str = "No matching lines detected by deterministic rules.",
) -> List[str]:
    """
    Render a capped bullet list with omission count.

    :param items: Items to render
    :param max_items: Maximum number to render
    :param empty_text: Text for empty lists
    :return: Markdown lines
    """
    if not items:
        return [empty_text, ""]

    shown = list(items[:max_items])
    lines = [f"- {item}" for item in shown]
    omitted = len(items) - len(shown)
    if omitted > 0:
        lines.append(f"- _{omitted} additional item(s) omitted from this signal view._")
    return lines + [""]


def render_signal_table(actions: Sequence[Signal]) -> List[str]:
    """
    Render action-like signal rows.

    :param actions: Action-like evidence rows
    :return: Markdown table lines
    """
    if not actions:
        return ["No matching lines detected by deterministic rules.", ""]

    lines = [
        "| Signal | Evidence | Nearby Result | Confidence |",
        "|---|---|---|---|",
    ]
    for action in actions:
        lines.append(
            "| "
            + " | ".join(
                [
                    md_escape_table(action.label),
                    md_escape_table(truncate_line(action.evidence, 220)),
                    md_escape_table(action.result or "No nearby result line detected"),
                    md_escape_table(action.confidence),
                ]
            )
            + " |"
        )
    lines.append("")
    return lines


def render_roll_calls(roll_calls: Sequence[RollCall]) -> List[str]:
    """
    Render detected roll-call vote blocks.

    :param roll_calls: Detected roll-call blocks
    :return: Markdown lines
    """
    if not roll_calls:
        return ["No matching roll-call blocks detected by deterministic rules.", ""]

    lines: List[str] = []
    for idx, roll_call in enumerate(roll_calls, start=1):
        lines.extend(
            [
                f"### Roll Call Signal {idx}: {roll_call.context}",
                "",
                "| Name | Vote |",
                "|---|---|",
            ]
        )
        for name, vote in roll_call.votes:
            lines.append(f"| {md_escape_table(name)} | {md_escape_table(vote)} |")
        if roll_call.result:
            lines.extend(["", f"Nearby result line: {roll_call.result}"])
        lines.append("")
    return lines


def render_money_table(items: Sequence[MoneyReference]) -> List[str]:
    """
    Render money signal rows.

    :param items: Money references extracted from transcript text
    :return: Markdown table lines
    """
    if not items:
        return ["No matching lines detected by deterministic rules.", ""]

    lines = ["| Amount | Context |", "|---:|---|"]
    for item in items:
        lines.append(f"| {md_escape_table(item.amount)} | {md_escape_table(item.context)} |")
    lines.append("")
    return lines


def render_civic_object_table(items: Sequence[Signal]) -> List[str]:
    """
    Render civic object reference rows.

    :param items: Civic object signals
    :return: Markdown table lines
    """
    if not items:
        return ["No matching lines detected by deterministic rules.", ""]

    lines = ["| Type | Evidence |", "|---|---|"]
    for item in items:
        lines.append(f"| {md_escape_table(item.label)} | {md_escape_table(item.evidence)} |")
    lines.append("")
    return lines


def render_signals(record: ExtractedSignals) -> str:
    """
    Render deterministic meeting signals as Markdown.

    :param record: Extracted signal record
    :return: Markdown document
    """
    lines: List[str] = [
        "# Meeting Signals",
        "",
        f"Source transcript: `{record.base_name}.txt`",
        "",
        "Generated from normalized transcript text using deterministic pattern matching.",
        "",
        "This document surfaces candidate evidence lines only. It does not prove absence, "
        "verify claims, resolve disputes, interpret intent, or produce the final civic record.",
        "",
        "Public-comment content is not extracted in this deterministic signal pass "
        "because comment boundaries are transcript-dependent and better handled by "
        "the downstream record-generation pass.",
        "",
        "Use these signals as review scaffolding for a later record-generation pass, "
        "not as authoritative meeting minutes.",
        "",
        "## Motion / Vote Signals",
        "",
    ]

    lines.extend(render_signal_table(record.actions))
    lines.extend(["## Roll Call Signals", ""])
    lines.extend(render_roll_calls(record.roll_calls))
    lines.extend(["## Money Signals", ""])
    lines.extend(render_money_table(record.money))
    lines.extend(["## Civic Object Signals", ""])
    lines.extend(render_civic_object_table(record.civic_objects))
    lines.extend(["## Question Signals", ""])
    lines.extend(render_limited_list(record.questions, MAX_QUESTIONS))
    lines.extend(["## Low-Confidence Transcript Lines", ""])
    lines.extend(render_limited_list(record.low_confidence, MAX_LOW_CONFIDENCE))

    lines.extend(
        [
            "## Internal Extraction Counts",
            "",
            "These counts are included for QA and pipeline observability. They should not be read as proof that a category was absent from the meeting.",
            "",
            "| Category | Count |",
            "|---|---:|",
            f"| Action-like signals | {len(record.actions)} |",
            f"| Roll-call blocks | {len(record.roll_calls)} |",
            f"| Money references | {len(record.money)} |",
            f"| Civic object references | {len(record.civic_objects)} |",
            f"| Question lines | {len(record.questions)} |",
            f"| Possible people detected internally | {len(record.people)} |",
            f"| Low-confidence lines | {len(record.low_confidence)} |",
            "",
            "## Extraction Notes",
            "",
            "- This artifact is intentionally generic across public meeting types.",
            "- It may miss actions, votes, speakers, topics, or context.",
            "- It may include false positives caused by transcription errors.",
            "- Empty sections mean no matching lines were detected, not that the event did not happen.",
            "- Money references are transcript-derived and should not be treated as verified amounts.",
            "- Important claims should be verified against the original recording and official meeting records.",
            "",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def resolve_signal_paths(
    global_config_path: str,
    entity: str | None,
    input_path: str | None,
    output_dir: str | None,
) -> Dict[str, Any]:
    """
    Resolve config, input files, and output directory.

    :param global_config_path: Global config path
    :param entity: Entity handle
    :param input_path: Optional input path override
    :param output_dir: Optional output directory override
    :return: Resolved paths and config
    :raises ValidationError: When required inputs are missing
    """
    global_cfg = load_global_config(global_config_path)
    entity_cfg: Dict[str, Any] = load_entity_config(global_cfg, entity) if entity else {}

    if not entity and (not input_path or not output_dir):
        raise ValidationError("Without --entity, both --input-path and --output-dir must be defined")

    data_path = entity_cfg.get("data_path")
    if entity and not data_path:
        raise ValidationError(f"Entity '{entity}' does not define data_path")

    resolved_input = input_path or os.path.join(data_path, "normalized")
    resolved_output = output_dir or os.path.join(data_path, DEFAULT_OUTPUT_DIRNAME)
    files = discover_files(
        resolved_input,
        SUPPORTED_INPUT_EXTENSIONS,
        exclude_suffixes=("punct.txt",),
    )
    return {
        "entity_cfg": entity_cfg,
        "input_path": resolved_input,
        "output_dir": resolved_output,
        "file_list": files,
    }


def empty_result(base_name: str, out_path: str, written: bool) -> Dict[str, Any]:
    """
    Stable per-file result for skipped or empty signal outputs.

    :param base_name: Transcript basename
    :param out_path: Output path
    :param written: Whether output was written
    :return: Result dictionary
    """
    return {
        "artifact_kind": ARTIFACT_KIND,
        "base_name": base_name,
        "output_path": out_path,
        "written": written,
        "action_count": 0,
        "money_count": 0,
        "question_count": 0,
        "civic_object_count": 0,
        "people_count": 0,
        "low_confidence_count": 0,
        "roll_call_count": 0,
    }


def signal_result(
    base_name: str,
    out_path: str,
    written: bool,
    record: ExtractedSignals,
) -> Dict[str, Any]:
    """
    Stable per-file result from extracted meeting signals.

    :param base_name: Transcript basename
    :param out_path: Output path
    :param written: Whether output was written
    :param record: Extracted signals
    :return: Result dictionary
    """
    result = empty_result(base_name, out_path, written)
    result.update(
        {
            "action_count": len(record.actions),
            "money_count": len(record.money),
            "question_count": len(record.questions),
            "civic_object_count": len(record.civic_objects),
            "people_count": len(record.people),
            "low_confidence_count": len(record.low_confidence),
            "roll_call_count": len(record.roll_calls),
        }
    )
    return result


def run_signals_stage(
    global_config_path: str,
    entity: str | None = None,
    input_path: str | None = None,
    output_dir: str | None = None,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Run deterministic meeting signal extraction.

    :param global_config_path: Global config path
    :param entity: Optional entity handle
    :param input_path: Optional input path override
    :param output_dir: Optional output path override
    :param force: Whether to overwrite existing outputs
    :return: Stage result dictionary
    """
    resolved = resolve_signal_paths(global_config_path, entity, input_path, output_dir)
    input_files = resolved["file_list"]
    output_directory = resolved["output_dir"]
    entity_cfg = resolved["entity_cfg"]
    os.makedirs(output_directory, exist_ok=True)

    if not input_files:
        return {
            "artifact_kind": ARTIFACT_KIND,
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "input_path": resolved["input_path"],
            "output_dir": output_directory,
            "discovered": 0,
            "written": 0,
            "skipped": 0,
            "results": [],
            "messages": [f"No normalized transcript files found at {resolved['input_path']}"],
        }

    written = 0
    skipped = 0
    messages: List[str] = []
    results: List[Dict[str, Any]] = []

    for src_path in input_files:
        base_name = os.path.splitext(os.path.basename(src_path))[0]
        out_path = os.path.join(output_directory, f"{base_name}{OUTPUT_SUFFIX}")

        if os.path.exists(out_path) and not force:
            skipped += 1
            messages.append(f"Skipped existing meeting signals: {out_path}")
            results.append(empty_result(base_name, out_path, written=False))
            continue

        signals = extract_meeting_signals(
            transcript_text=load_text(src_path),
            base_name=base_name,
            entity_cfg=entity_cfg,
        )
        write_text(out_path, render_signals(signals))
        written += 1
        messages.append(f"Wrote meeting signals: {out_path}")
        results.append(signal_result(base_name, out_path, True, signals))

    return {
        "artifact_kind": ARTIFACT_KIND,
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "input_path": resolved["input_path"],
        "output_dir": output_directory,
        "discovered": len(input_files),
        "written": written,
        "skipped": skipped,
        "results": results,
        "messages": messages,
    }