"""
Shared LLM execution helpers for YATSEE intelligence workflows.

This module centralizes provider runtime resolution, provider execution, token
accounting, and the reusable process for generating one model response per text
chunk. Workflow-specific behavior such as summary reduction, records validation,
and artifact layout remains in the owning workflow module.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Set

import requests

from yatsee.core.errors import ConfigError
from yatsee.intel.chunking import chunk_text
from yatsee.intel.speakers import (
    generate_known_speakers_context,
    scan_transcript_for_names,
)
from yatsee.providers import get_provider
from yatsee.providers.security import validate_model_name, validate_provider_target
from yatsee.providers.tokenization import estimate_token_count

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMRuntime:
    """
    Hold resolved provider settings shared by LLM-backed intelligence workflows.

    Workflow-specific decisions such as prompt routing, source chunk size, output
    format, and reduction behavior remain owned by the calling stage.

    :param provider_name: Configured provider identifier
    :param provider_target: Provider URL or executable target
    :param api_key: Optional provider API key
    :param model: Provider model name
    :param num_ctx: Requested model context window
    :param max_output_tokens: Optional default output token cap
    :param temperature: Provider sampling temperature
    :param timeout: Provider request timeout in seconds
    :param allow_remote: Whether remote non-local targets are allowed
    :param allow_insecure_http: Whether non-loopback plain HTTP targets are allowed
    :param allow_loopback_http: Whether loopback plain HTTP targets are allowed
    :param allow_custom_executable: Whether custom CLI executable targets are allowed
    """

    provider_name: str
    provider_target: str
    api_key: str | None
    model: str
    num_ctx: int
    max_output_tokens: int | None = None
    temperature: float = 0.2
    timeout: int = 300
    allow_remote: bool = False
    allow_insecure_http: bool = False
    allow_loopback_http: bool = True
    allow_custom_executable: bool = False


@dataclass
class TokenUsage:
    """
    Track estimated input and output tokens for one intelligence artifact run.

    :param input_tokens: Estimated prompt/input tokens
    :param output_tokens: Estimated generated/output tokens
    """

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """
        Return combined input and output token usage.

        :return: Total estimated tokens
        """
        return self.input_tokens + self.output_tokens

    def add(self, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        """
        Add token counts to the current totals.

        :param input_tokens: Input tokens to add
        :param output_tokens: Output tokens to add
        :return: None
        """
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens


@dataclass(frozen=True)
class GeneratedChunk:
    """
    Hold one source chunk, rendered prompt, and raw model response.

    The source text is retained because records and other workflows may need to
    validate generated evidence against the exact chunk supplied to the model.

    :param chunk_id: One-based chunk number
    :param chunk_count: Total number of chunks in the generation call
    :param source_text: Source text supplied for this chunk
    :param prompt: Fully rendered provider prompt
    :param output: Raw generated response
    :param input_tokens: Estimated prompt tokens
    :param output_tokens: Estimated response tokens
    """

    chunk_id: int
    chunk_count: int
    source_text: str
    prompt: str
    output: str
    input_tokens: int
    output_tokens: int


def resolve_llm_api_key(
    *,
    provider_name: str,
    cli_api_key: str | None,
    system_cfg: Dict[str, Any],
) -> str | None:
    """
    Resolve an LLM API key without requiring secrets on the command line.

    :param provider_name: Configured provider identifier
    :param cli_api_key: API key supplied directly on the CLI
    :param system_cfg: Global system configuration mapping
    :return: Resolved API key, if any
    """
    if cli_api_key is not None:
        return cli_api_key

    configured_env_name = str(system_cfg.get("llm_api_key_env", "")).strip()
    if configured_env_name:
        return os.environ.get(configured_env_name)

    configured_key = system_cfg.get("llm_api_key")
    if configured_key:
        return str(configured_key)

    provider_env_names = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
    }

    for env_name in (provider_env_names.get(provider_name), "YATSEE_LLM_API_KEY"):
        if env_name and os.environ.get(env_name):
            return os.environ[env_name]

    return None


def resolve_llm_runtime(
    *,
    system_cfg: Dict[str, Any],
    model: str,
    num_ctx: int,
    provider_name: str | None = None,
    provider_target: str | None = None,
    api_key: str | None = None,
) -> LLMRuntime:
    """
    Resolve provider settings shared by LLM-backed intelligence stages.

    CLI and other callers pass explicit overrides so this provider-neutral layer
    does not depend on argparse or another presentation interface. Target and
    model validation remains in the generation helper so every request crosses
    the same trust-boundary checks.

    :param system_cfg: Global system configuration mapping
    :param model: Resolved model name
    :param num_ctx: Resolved context window
    :param provider_name: Optional provider override
    :param provider_target: Optional provider URL or executable target override
    :param api_key: Optional API key override
    :return: Resolved LLM runtime
    :raises ConfigError: If required provider configuration is missing
    """
    resolved_provider_name = str(
        provider_name or system_cfg.get("llm_provider", "ollama")
    ).strip().lower()
    resolved_provider_target = (
        provider_target or system_cfg.get("llm_provider_url")
    )

    if not resolved_provider_name:
        raise ConfigError("No llm_provider configured in [system]")

    if not resolved_provider_target:
        raise ConfigError("No llm_provider_url configured in [system]")

    resolved_api_key = resolve_llm_api_key(
        provider_name=resolved_provider_name,
        cli_api_key=api_key,
        system_cfg=system_cfg,
    )

    return LLMRuntime(
        provider_name=resolved_provider_name,
        provider_target=str(resolved_provider_target),
        api_key=resolved_api_key,
        model=model,
        num_ctx=num_ctx,
        allow_remote=bool(system_cfg.get("llm_allow_remote", False)),
        allow_insecure_http=bool(system_cfg.get("llm_allow_insecure_http", False)),
        allow_loopback_http=bool(system_cfg.get("llm_allow_loopback_http", True)),
        allow_custom_executable=bool(
            system_cfg.get("llm_allow_custom_executable", False)
        ),
    )


def create_llm_session() -> requests.Session:
    """
    Create a provider session with deterministic proxy and content settings.

    Environment proxy variables are disabled because silently redirecting local
    or LAN model traffic through an ambient proxy would change the configured
    provider trust boundary.

    :return: Configured requests session
    """
    session = requests.Session()
    session.trust_env = False
    session.headers.update({"Content-Type": "application/json"})
    return session


def llm_generate_text(
    *,
    session: requests.Session,
    provider_name: str,
    llm_provider_url: str,
    api_key: str | None,
    model: str,
    prompt: str,
    num_ctx: int = 8192,
    max_output_tokens: int | None = None,
    allow_remote: bool = False,
    allow_insecure_http: bool = False,
    allow_loopback_http: bool = True,
    allow_custom_executable: bool = False,
) -> str:
    """
    Generate text through the configured LLM provider.

    This compatibility entry point converts explicit provider arguments into the
    shared runtime object. New intelligence workflows should resolve the runtime
    once and call ``llm_generate_with_runtime`` directly.

    :param session: Shared requests session
    :param provider_name: Configured provider identifier
    :param llm_provider_url: Provider base URL or CLI executable target
    :param api_key: Optional provider API key
    :param model: Provider model name
    :param prompt: Prompt text
    :param num_ctx: Requested context window
    :param max_output_tokens: Optional explicit output token cap
    :param allow_remote: Whether remote non-local targets are allowed
    :param allow_insecure_http: Whether non-loopback plain HTTP targets are allowed
    :param allow_loopback_http: Whether loopback plain HTTP targets are allowed
    :param allow_custom_executable: Whether custom CLI executable targets are allowed
    :return: Generated text
    :raises ValueError: If the prompt is empty
    :raises ProviderConfigError: If the model or provider target is invalid
    :raises ProviderError: If provider resolution or execution fails
    """
    runtime = LLMRuntime(
        provider_name=provider_name,
        provider_target=llm_provider_url,
        api_key=api_key,
        model=model,
        num_ctx=num_ctx,
        max_output_tokens=max_output_tokens,
        allow_remote=allow_remote,
        allow_insecure_http=allow_insecure_http,
        allow_loopback_http=allow_loopback_http,
        allow_custom_executable=allow_custom_executable,
    )
    return llm_generate_with_runtime(
        session=session,
        runtime=runtime,
        prompt=prompt,
    )


def llm_generate_with_runtime(
    *,
    session: requests.Session,
    runtime: LLMRuntime,
    prompt: str,
    max_output_tokens: int | None = None,
) -> str:
    """
    Generate text using a resolved shared LLM runtime.

    The explicit output limit overrides the runtime default for narrow calls such
    as classification while preserving the same trust and execution settings.

    :param session: Shared requests session
    :param runtime: Resolved provider runtime
    :param prompt: Prompt text
    :param max_output_tokens: Optional per-call output token cap
    :return: Generated text
    :raises ValueError: If the prompt is empty
    :raises ProviderConfigError: If the model or provider target is invalid
    :raises ProviderError: If provider resolution or execution fails
    """
    output_limit = (
        runtime.max_output_tokens
        if max_output_tokens is None
        else max_output_tokens
    )

    if not prompt or not prompt.strip():
        raise ValueError("Prompt must not be empty")

    validate_model_name(runtime.model)
    validate_provider_target(
        provider_name=runtime.provider_name,
        target=runtime.provider_target,
        allow_remote=runtime.allow_remote,
        allow_insecure_http=runtime.allow_insecure_http,
        allow_loopback_http=runtime.allow_loopback_http,
        allow_custom_executable=runtime.allow_custom_executable,
    )

    provider = get_provider(runtime.provider_name)
    return provider.generate_text(
        session=session,
        base_url=runtime.provider_target,
        model=runtime.model,
        prompt=prompt,
        api_key=runtime.api_key,
        num_ctx=runtime.num_ctx,
        max_output_tokens=output_limit,
        temperature=runtime.temperature,
        timeout=runtime.timeout,
    )


def generate_chunk_outputs(
    *,
    session: requests.Session,
    runtime: LLMRuntime,
    text: str,
    prompt_id: str,
    prompt_template: str,
    context: str,
    chunk_style: str,
    max_tokens: int,
    density_keywords: Set[str] | None = None,
    density_threshold: int = 25,
    known_name_permutations: Dict[str, List[str]] | None = None,
    stage_label: str = "generation",
    on_chunk: Callable[[GeneratedChunk], None] | None = None,
) -> tuple[List[GeneratedChunk], TokenUsage]:
    """
    Chunk text and generate one raw model response per chunk.

    This function shares chunk-level LLM execution without owning reduction,
    records validation, artifact layout, or workflow completion rules.

    :param session: Shared provider session
    :param runtime: Resolved provider runtime
    :param text: Source text to chunk and process
    :param prompt_id: Prompt identifier used for logging
    :param prompt_template: Prompt template containing context and text fields
    :param context: Human-readable meeting context
    :param chunk_style: Chunking strategy
    :param max_tokens: Approximate source token budget per chunk
    :param density_keywords: Optional density keywords
    :param density_threshold: Density threshold for density-aware chunking
    :param known_name_permutations: Optional configured-name variants
    :param stage_label: Human-readable stage label for logs
    :param on_chunk: Optional callback invoked after each generated chunk
    :return: Generated chunk results and estimated token usage
    """
    chunks = chunk_text(
        text=text,
        chunk_style=chunk_style,
        max_tokens=max_tokens,
        density_keywords=density_keywords,
        density_threshold=density_threshold,
    )

    generated_chunks: List[GeneratedChunk] = []
    usage = TokenUsage()
    name_permutations = known_name_permutations or {}

    for chunk_index, source_chunk in enumerate(chunks, start=1):
        logger.info(
            "%s processing chunk %d/%d with prompt [%s]",
            stage_label,
            chunk_index,
            len(chunks),
            prompt_id,
        )

        name_matches = scan_transcript_for_names(source_chunk, name_permutations)
        name_context = generate_known_speakers_context(name_matches)
        # Preserve the existing summary prompt shape exactly while moving the
        # reusable execution mechanics out of summary orchestration.
        prompt_text = f"{name_context}\n\n{source_chunk}"

        rendered_prompt = prompt_template.format(
            context=context or "No context provided.",
            text=prompt_text,
        )
        output = llm_generate_with_runtime(
            session=session,
            runtime=runtime,
            prompt=rendered_prompt,
        )

        input_tokens = estimate_token_count(rendered_prompt)
        output_tokens = estimate_token_count(output)
        usage.add(input_tokens=input_tokens, output_tokens=output_tokens)

        generated_chunk = GeneratedChunk(
            chunk_id=chunk_index,
            chunk_count=len(chunks),
            source_text=source_chunk,
            prompt=rendered_prompt,
            output=output,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        generated_chunks.append(generated_chunk)

        if on_chunk is not None:
            on_chunk(generated_chunk)

    return generated_chunks, usage
