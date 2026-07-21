"""
Prompt loading and routing helpers for YATSEE intelligence jobs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import toml

from yatsee.core.errors import ConfigError

_PROFILE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")

CLASSIFIER_PROMPT_FILENAME = "classifier.toml"
SUMMARY_PROMPT_FILENAME = "prompts.toml"
RECORDS_PROMPT_FILENAME = "records_prompt.toml"


DEFAULT_PROMPTS: Dict[str, str] = {
    "overview": (
        "You are an assistant that produces a high-level summary of a meeting transcript.\n\n"
        "Context: {context}\n\n"
        "Transcript:\n{text}\n\n"
        "Summarize the key points, main discussions, and outcomes in clear, concise language."
    ),
    "action_items": (
        "You are an assistant that extracts actionable items, motions, and decisions from a meeting transcript.\n\n"
        "Context: {context}\n\n"
        "Transcript:\n{text}\n\n"
        "List only the actionable items or decisions in plain language. Skip commentary or filler text."
    ),
}

DEFAULT_PROMPT_ROUTER: Dict[str, Dict[str, str]] = {
    "general": {
        "first": "overview",
        "multi": "overview",
        "final": "action_items",
    }
}


DEFAULT_CLASSIFIER_TYPES: Dict[str, List[str]] = {
    "allowed": [
        "city_council",
        "finance_committee",
        "committee_of_the_whole",
        "zoning_committee",
        "general",
    ]
}

DEFAULT_CLASSIFIER_PROMPT = """
You are a routing assistant. Classify this meeting by its formal meeting type.

Context: {context}

Transcript Snippet:
{text}

Return exactly one of:
- city_council
- finance_committee
- committee_of_the_whole
- zoning_committee
- general

Rules:
1. Classify the formal meeting body, not the subjects discussed.
2. Prefer an explicitly stated meeting title from Context or the transcript.
3. A Committee of the Whole meeting remains committee_of_the_whole even when
   finance, budget, zoning, or appropriation topics are discussed.
4. Use finance_committee only when the meeting itself is identified as a
   finance, budget, or appropriation committee meeting.
5. Use zoning_committee when the meeting itself is identified as zoning,
   planning, land use, or a related committee meeting.
6. Use city_council for regular, special, or explicitly identified city council
   meetings.
7. Use general when the formal meeting type cannot be determined.

Return only the classification value. Do not include commentary.
"""


def validate_profile_name(job_profile: str) -> str:
    """
    Validate a prompt/profile name before using it as a path component.

    Profiles are filesystem-backed, so names must be simple path components.
    This preserves profile flexibility without allowing path traversal or nested
    path injection through CLI arguments.

    :param job_profile: Requested profile name
    :return: Normalized profile name
    :raises ConfigError: If the profile name is empty or unsafe
    """
    normalized = job_profile.strip() if job_profile else ""
    if not _PROFILE_NAME_RE.fullmatch(normalized):
        raise ConfigError(
            "Invalid job profile. Use only letters, numbers, underscores, dashes, and dots."
        )
    return normalized


def _prompt_roots(entity_cfg: Dict[str, Any]) -> List[Path]:
    """
    Return prompt root directories in lookup precedence order.

    Entity-local prompt bundles override project prompt bundles. Both roots are
    optional so local installs can remain small while still supporting profile
    discovery when prompt files are present.

    :param entity_cfg: Merged entity configuration
    :return: Prompt root paths
    """
    roots: List[Path] = []
    data_path = str(entity_cfg.get("data_path", "")).strip()
    if data_path:
        roots.append(Path(data_path) / "prompts")

    roots.append(Path("prompts"))
    return roots


def _resolve_profile_prompt_file(
    entity_cfg: Dict[str, Any],
    job_profile: str,
    filename: str,
) -> str:
    """
    Resolve a profile prompt file using entity-local then project precedence.

    Prompt files are configuration artifacts. Resolution is centralized so
    summary and records workflows apply the same profile-name validation and
    entity override behavior without sharing their routing contracts.

    :param entity_cfg: Merged entity configuration
    :param job_profile: Prompt/profile name
    :param filename: Prompt filename within the profile directory
    :return: Resolved prompt path, or an empty string when not found
    """
    validated_profile = validate_profile_name(job_profile)

    for root in _prompt_roots(entity_cfg):
        prompt_path = root / validated_profile / filename
        if prompt_path.is_file():
            return str(prompt_path)

    return ""


def _load_prompt_config(prompt_path: str) -> Dict[str, Any]:
    """
    Parse one TOML prompt configuration file.

    :param prompt_path: Prompt TOML path
    :return: Parsed TOML mapping
    :raises ConfigError: If the file cannot be parsed or has an invalid root
    """
    try:
        raw_config = toml.load(prompt_path)
    except Exception as exc:
        raise ConfigError(
            f"Failed to parse prompt file '{prompt_path}': {exc}"
        ) from exc

    if not isinstance(raw_config, dict):
        raise ConfigError(
            f"Prompt file '{prompt_path}' must contain a TOML table structure"
        )

    return raw_config


def _load_prompt_texts(
    raw_config: Dict[str, Any],
    prompt_path: str,
) -> Dict[str, str]:
    """
    Load and validate the shared ``[prompts]`` table.

    Summary and records use different routing contracts, but both reference the
    same prompt table shape. Keeping this validation in one place prevents the
    two loaders from drifting on prompt-text requirements.

    :param raw_config: Parsed prompt configuration
    :param prompt_path: Prompt file path used in validation errors
    :return: Prompt identifier to text mapping
    :raises ConfigError: If the prompt table is invalid
    """
    raw_prompt_map = raw_config.get("prompts", {})
    if not isinstance(raw_prompt_map, dict):
        raise ConfigError(
            f"Prompt file '{prompt_path}' has invalid [prompts] structure"
        )

    prompt_texts: Dict[str, str] = {}
    for prompt_id, prompt_entry in raw_prompt_map.items():
        if not isinstance(prompt_entry, dict):
            raise ConfigError(
                f"Prompt '{prompt_id}' in '{prompt_path}' must be a table/object"
            )

        prompt_text = prompt_entry.get("text")
        if not isinstance(prompt_text, str) or not prompt_text.strip():
            raise ConfigError(
                f"Prompt '{prompt_id}' in '{prompt_path}' must define a non-empty text value"
            )

        prompt_texts[prompt_id] = prompt_text

    return prompt_texts



def _validate_classifier_bundle(bundle: Dict[str, Any]) -> None:
    """
    Validate classifier prompt text and allowed meeting labels.

    :param bundle: Loaded classifier bundle
    :raises ConfigError: If classifier configuration is incomplete or invalid
    """
    classifier_prompt = bundle.get("classifier_prompt")
    allowed_meeting_types = bundle.get("allowed_meeting_types")

    if not isinstance(classifier_prompt, str) or not classifier_prompt.strip():
        raise ConfigError("Classifier bundle must define non-empty prompt text")

    for required_placeholder in ("{context}", "{text}"):
        if required_placeholder not in classifier_prompt:
            raise ConfigError(
                "Classifier prompt must contain both {context} and {text} placeholders"
            )

    if not isinstance(allowed_meeting_types, list) or not allowed_meeting_types:
        raise ConfigError("Classifier bundle must define at least one allowed type")

    if any(
        not isinstance(label, str) or not label.strip()
        for label in allowed_meeting_types
    ):
        raise ConfigError("Classifier allowed types must be non-empty strings")

    if len(set(allowed_meeting_types)) != len(allowed_meeting_types):
        raise ConfigError("Classifier allowed types must not contain duplicates")

    if "general" not in allowed_meeting_types:
        raise ConfigError("Classifier allowed types must include 'general'")


def load_classifier_bundle(
    entity_cfg: Dict[str, Any],
    job_profile: str,
    *,
    require_prompt_file: bool = False,
) -> Dict[str, Any]:
    """
    Load shared meeting-classifier configuration for an intelligence profile.

    Resolution checks the entity-local profile first and then the project
    profile. A built-in artifact-neutral classifier remains available for
    profiles that have not yet added a sidecar.

    :param entity_cfg: Merged entity configuration
    :param job_profile: Prompt/profile name
    :param require_prompt_file: Whether ``classifier.toml`` must exist
    :return: Classifier prompt, allowed labels, path, and fallback metadata
    :raises ConfigError: If classifier configuration is missing or invalid
    """
    classifier_path = _resolve_profile_prompt_file(
        entity_cfg,
        job_profile,
        CLASSIFIER_PROMPT_FILENAME,
    )

    if not classifier_path and require_prompt_file:
        raise ConfigError(
            f"No classifier bundle found for profile '{job_profile}'. "
            f"Expected {CLASSIFIER_PROMPT_FILENAME} under a profile directory."
        )

    if not classifier_path:
        bundle = {
            "classifier_prompt": DEFAULT_CLASSIFIER_PROMPT,
            "allowed_meeting_types": list(DEFAULT_CLASSIFIER_TYPES["allowed"]),
            "path": "",
            "fallback": True,
        }
        _validate_classifier_bundle(bundle)
        return bundle

    raw_config = _load_prompt_config(classifier_path)

    classifier_prompt_entry = raw_config.get("classifier_prompt", {})
    if not isinstance(classifier_prompt_entry, dict):
        raise ConfigError(
            f"Classifier file '{classifier_path}' has invalid [classifier_prompt] structure"
        )

    classifier_prompt = classifier_prompt_entry.get("text")

    classifier_types = raw_config.get("classifier_types", {})
    if not isinstance(classifier_types, dict):
        raise ConfigError(
            f"Classifier file '{classifier_path}' has invalid [classifier_types] structure"
        )

    allowed_meeting_types = classifier_types.get("allowed")
    bundle = {
        "classifier_prompt": classifier_prompt,
        "allowed_meeting_types": allowed_meeting_types,
        "path": classifier_path,
        "fallback": False,
    }
    _validate_classifier_bundle(bundle)
    return bundle

def discover_summary_prompt_profiles(entity_cfg: Dict[str, Any] | None = None) -> List[str]:
    """
    Discover filesystem-backed prompt profiles.

    Discovery is intentionally mechanical: any direct child directory containing
    a summary ``prompts.toml`` file is considered a profile candidate. Validation still
    happens separately when each bundle is loaded.

    :param entity_cfg: Optional merged entity configuration
    :return: Sorted profile names
    """
    profile_names = set()
    for root in _prompt_roots(entity_cfg or {}):
        if not root.is_dir():
            continue

        for child in root.iterdir():
            if child.is_dir() and (child / SUMMARY_PROMPT_FILENAME).is_file():
                profile_names.add(child.name)

    return sorted(profile_names)


def validate_prompt(
    prompt_name: str | None,
    label: str,
    available_prompts: Dict[str, str],
) -> None:
    """
    Validate that a referenced prompt identifier exists in the loaded prompt set.

    This helper is used for CLI overrides and other direct prompt references so
    invalid identifiers fail with a clear message before generation begins.

    :param prompt_name: Requested prompt name
    :param label: Human-readable label for errors
    :param available_prompts: Prompt dictionary
    :raises ValueError: If the prompt name is invalid
    """
    if not prompt_name:
        return

    if prompt_name not in available_prompts:
        valid_keys = ", ".join(sorted(available_prompts.keys()))
        raise ValueError(
            f"Invalid {label} '{prompt_name}'. Available prompts: {valid_keys}"
        )


def _validate_summary_prompt_bundle(bundle: Dict[str, Any]) -> None:
    """
    Validate the structural requirements of a summary prompt bundle.

    A valid summary bundle must contain at least one prompt, a ``general``
    route, and routing entries that reference prompt identifiers that actually exist in the
    prompt map.

    :param bundle: Loaded prompt bundle dictionary
    :raises ConfigError: If the bundle structure is incomplete or invalid
    """
    prompts = bundle.get("prompts", {})
    prompt_router = bundle.get("prompt_router", {})

    if not prompts:
        raise ConfigError("Prompt bundle does not define any prompts")

    general_route = prompt_router.get("general")
    if not general_route:
        raise ConfigError("Prompt bundle must define a 'general' prompt route")

    for route_name, route in prompt_router.items():
        for required_key in ("first", "multi", "final"):
            if required_key not in route:
                raise ConfigError(
                    f"Prompt route '{route_name}' is missing required key '{required_key}'"
                )

            prompt_id = route[required_key]
            if prompt_id not in prompts:
                raise ConfigError(
                    f"Prompt route '{route_name}.{required_key}' references unknown prompt '{prompt_id}'"
                )


def load_summary_prompt_bundle(
    entity_cfg: Dict[str, Any],
    job_profile: str,
    *,
    require_prompt_file: bool = False,
) -> Dict[str, Any]:
    """
    Load summary prompts and routing metadata for a job profile.

    Resolution checks for an entity-local prompt file first, then the project
    default summary prompt file, and finally falls back to the built-in summary bundle.
    The resulting structure is validated before being returned.

    :param entity_cfg: Merged entity configuration
    :param job_profile: Prompt/profile name
    :param require_prompt_file: Whether a filesystem-backed profile file is required
    :return: Prompt bundle dictionary
    :raises ConfigError: If prompt loading or validation fails
    """
    prompt_types_path = _resolve_profile_prompt_file(
        entity_cfg,
        job_profile,
        SUMMARY_PROMPT_FILENAME,
    )

    if not prompt_types_path and require_prompt_file:
        raise ConfigError(
            f"No summary prompt bundle found for profile '{job_profile}'. "
            f"Expected {SUMMARY_PROMPT_FILENAME} under a profile directory."
        )

    if not prompt_types_path:
        bundle = {
            "prompts": DEFAULT_PROMPTS,
            "prompt_router": DEFAULT_PROMPT_ROUTER,
            "density_keywords": {},
            "path": "",
            "fallback": True,
        }
        _validate_summary_prompt_bundle(bundle)
        return bundle

    raw_prompts = _load_prompt_config(prompt_types_path)
    prompt_texts = _load_prompt_texts(raw_prompts, prompt_types_path)

    prompt_router = raw_prompts.get("prompt_router", {})
    if not isinstance(prompt_router, dict):
        raise ConfigError(
            f"Prompt file '{prompt_types_path}' has invalid [prompt_router] structure"
        )

    density_keywords = raw_prompts.get("density_keywords", {})
    if density_keywords and not isinstance(density_keywords, dict):
        raise ConfigError(
            f"Prompt file '{prompt_types_path}' has invalid [density_keywords] structure"
        )

    bundle = {
        "prompts": prompt_texts,
        "prompt_router": prompt_router,
        "density_keywords": density_keywords,
        "path": prompt_types_path,
        "fallback": False,
    }

    _validate_summary_prompt_bundle(bundle)
    return bundle


def _validate_records_prompt_bundle(bundle: Dict[str, Any]) -> None:
    """
    Validate a single-pass records extraction prompt bundle.

    Records routing maps each meeting type directly to one extraction prompt.
    It intentionally has no summary-style first, multi, or final stages. A
    ``general`` route is required as the deterministic fallback for unknown or
    unsupported meeting classifications.

    :param bundle: Loaded records prompt bundle
    :raises ConfigError: If the records routing contract is invalid
    """
    prompts = bundle.get("prompts", {})
    records_router = bundle.get("records_router", {})

    if not prompts:
        raise ConfigError("Records prompt bundle does not define any prompts")

    if not records_router:
        raise ConfigError("Records prompt bundle does not define [records_router]")

    general_prompt = records_router.get("general")
    if not isinstance(general_prompt, str) or not general_prompt.strip():
        raise ConfigError(
            "Records prompt bundle must define a non-empty 'general' route"
        )

    for meeting_type, prompt_id in records_router.items():
        if not isinstance(prompt_id, str) or not prompt_id.strip():
            raise ConfigError(
                f"Records route '{meeting_type}' must reference one prompt identifier"
            )

        if prompt_id not in prompts:
            raise ConfigError(
                f"Records route '{meeting_type}' references unknown prompt '{prompt_id}'"
            )


def load_records_prompt_bundle(
    entity_cfg: Dict[str, Any],
    job_profile: str,
) -> Dict[str, Any]:
    """
    Load meeting-type records extraction prompts from a profile sidecar.

    Resolution checks ``records_prompt.toml`` in the entity-local profile first
    and then the project profile. Records has no built-in fallback because a
    missing sidecar should fail visibly rather than silently use summary prompts.

    :param entity_cfg: Merged entity configuration
    :param job_profile: Prompt/profile name
    :return: Records prompt bundle
    :raises ConfigError: If the sidecar is missing or invalid
    """
    prompt_path = _resolve_profile_prompt_file(
        entity_cfg,
        job_profile,
        RECORDS_PROMPT_FILENAME,
    )
    if not prompt_path:
        raise ConfigError(
            f"No records prompt bundle found for profile '{job_profile}'. "
            f"Expected {RECORDS_PROMPT_FILENAME} under a profile directory."
        )

    raw_prompts = _load_prompt_config(prompt_path)
    prompt_texts = _load_prompt_texts(raw_prompts, prompt_path)

    records_router = raw_prompts.get("records_router", {})
    if not isinstance(records_router, dict):
        raise ConfigError(
            f"Prompt file '{prompt_path}' has invalid [records_router] structure"
        )

    bundle = {
        "prompts": prompt_texts,
        "records_router": records_router,
        "path": prompt_path,
    }
    _validate_records_prompt_bundle(bundle)
    return bundle


def resolve_records_prompt_id(
    meeting_type: str,
    records_router: Dict[str, str],
) -> str:
    """
    Resolve one records extraction prompt for a classified meeting type.

    :param meeting_type: Detected meeting type
    :param records_router: Meeting-type to prompt-ID mapping
    :return: Selected records extraction prompt identifier
    :raises ConfigError: If the required general fallback is missing
    """
    fallback_prompt = records_router.get("general")
    if not fallback_prompt:
        raise ConfigError(
            "Records prompt routing is missing required 'general' entry"
        )

    return records_router.get(meeting_type, fallback_prompt)


def resolve_summary_prompt_ids(
    meeting_type: str,
    prompt_lookup: Dict[str, Dict[str, str]],
    first_override: str | None,
    multi_override: str | None,
    final_override: str | None,
) -> Dict[str, str]:
    """
    Resolve the prompt identifiers for the current summarization run.

    Routing first attempts to use the detected meeting type. If no specific
    route exists, the ``general`` route is used. Individual pass prompts may be
    overridden by runtime arguments.

    :param meeting_type: Detected meeting type
    :param prompt_lookup: Prompt router mapping
    :param first_override: Override for the first pass prompt
    :param multi_override: Override for the intermediate pass prompt
    :param final_override: Override for the final synthesis prompt
    :return: Prompt ID mapping
    :raises ConfigError: If prompt routing is incomplete
    """
    fallback_route = prompt_lookup.get("general")
    if not fallback_route:
        raise ConfigError("Prompt routing is missing required 'general' entry")

    route = prompt_lookup.get(meeting_type, fallback_route)

    for required_key in ("first", "multi", "final"):
        if required_key not in route:
            raise ConfigError(
                f"Prompt route for '{meeting_type}' is missing required key '{required_key}'"
            )

    return {
        "first": first_override or route["first"],
        "multi": multi_override or route["multi"],
        "final": final_override or route["final"],
    }