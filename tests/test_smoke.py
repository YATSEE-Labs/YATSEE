"""
Smoke tests for security-sensitive YATSEE wiring.

These tests intentionally avoid heavy optional dependencies so they can run in a
minimal development environment with only the base package installed.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yatsee.cli.main import build_parser
from yatsee.core.errors import ValidationError
from yatsee.core.config import redact_sensitive_config
from yatsee.config_tools.validate import validate_entity_config, validate_global_config
from yatsee.core.paths import get_entity_dir, resolve_contained_path
from yatsee.providers.base import ProviderConfigError
from yatsee.providers.security import validate_provider_target


class CliSmokeTests(unittest.TestCase):
    """
    Validate root CLI parser construction without optional stage dependencies.
    """

    def test_root_parser_builds_without_optional_stage_imports(self) -> None:
        """
        Build the root parser and confirm important command aliases exist.
        """
        parser = build_parser()
        help_text = parser.format_help()

        self.assertIn("config", help_text)
        self.assertIn("audio", help_text)
        self.assertIn("audit", help_text)
        self.assertIn("transcript", help_text)
        self.assertIn("intel", help_text)

        args = parser.parse_args(["intel", "run", "--job-profile", "civic"])
        self.assertEqual(args.intel_command, "run")
        self.assertTrue(callable(args.handler))

        args = parser.parse_args(["intel", "summarize", "--job-profile", "civic"])
        self.assertEqual(args.intel_command, "summarize")
        self.assertTrue(callable(args.handler))

        args = parser.parse_args(["intel", "records"])
        self.assertEqual(args.chunk_style, "density")
        self.assertFalse(args.align_timestamps)
        self.assertFalse(args.timestamp_only)
        self.assertTrue(callable(args.handler))

        args = parser.parse_args(["intel", "records", "--align-timestamps"])
        self.assertTrue(args.align_timestamps)
        self.assertFalse(args.timestamp_only)
        self.assertTrue(callable(args.handler))

        args = parser.parse_args(["intel", "records", "--timestamp-only"])
        self.assertFalse(args.align_timestamps)
        self.assertTrue(args.timestamp_only)
        self.assertTrue(callable(args.handler))

        args = parser.parse_args(
            [
                "audit",
                "records",
                "-e",
                "test_entity",
                "--model",
                "gemma4:e4b",
                "--format",
                "json",
            ]
        )
        self.assertEqual(args.entity, "test_entity")
        self.assertEqual(args.output_format, "json")
        self.assertIsNone(args.rebuild_mode)
        self.assertTrue(callable(args.handler))

        args = parser.parse_args(
            ["audit", "records", "-e", "test_entity", "--rebuild"]
        )
        self.assertEqual(args.rebuild_mode, "plan")

        args = parser.parse_args(
            ["audit", "records", "-e", "test_entity", "--rebuild-auto"]
        )
        self.assertEqual(args.rebuild_mode, "auto")

        with self.assertRaises(SystemExit):
            parser.parse_args(["intel", "signals"])


class ProviderSecurityTests(unittest.TestCase):
    """
    Validate provider transport boundary policy.
    """

    def test_loopback_http_is_allowed_with_narrow_loopback_opt_in(self) -> None:
        """
        Local Ollama-style HTTP targets should not require broad insecure HTTP opt-in.
        """
        validate_provider_target(provider_name="ollama", target="http://localhost:11434")
        validate_provider_target(provider_name="ollama", target="http://127.0.0.1:11434")

    def test_loopback_http_can_be_disabled(self) -> None:
        """
        Loopback HTTP should be controlled by its own narrow setting.
        """
        with self.assertRaises(ProviderConfigError):
            validate_provider_target(
                provider_name="ollama",
                target="http://localhost:11434",
                allow_loopback_http=False,
            )

    def test_non_loopback_http_requires_remote_and_insecure_opt_in(self) -> None:
        """
        Off-box HTTP requires both remote and insecure HTTP opt-ins.
        """
        with self.assertRaises(ProviderConfigError):
            validate_provider_target(
                provider_name="ollama",
                target="http://192.168.1.20:11434",
                allow_remote=True,
                allow_insecure_http=False,
            )

        validate_provider_target(
            provider_name="ollama",
            target="http://192.168.1.20:11434",
            allow_remote=True,
            allow_insecure_http=True,
        )


class ConfigSafetyTests(unittest.TestCase):
    """
    Validate redaction and path-containment helpers.
    """

    def test_redact_config_masks_secrets_but_keeps_env_var_names(self) -> None:
        """
        Secret values should not appear in resolved config output by default.
        """
        redacted = redact_sensitive_config(
            {
                "system": {
                    "llm_api_key": "sk-test",
                    "llm_api_key_env": "OPENAI_API_KEY",
                    "llm_provider": "openai",
                }
            }
        )

        self.assertEqual(redacted["system"]["llm_api_key"], "***REDACTED***")
        self.assertEqual(redacted["system"]["llm_api_key_env"], "OPENAI_API_KEY")
        self.assertEqual(redacted["system"]["llm_provider"], "openai")

    def test_entity_path_rejects_invalid_handle(self) -> None:
        """
        Entity handles should not be able to escape root_data_dir.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = {"system": {"root_data_dir": tmp_dir}}
            with self.assertRaises(ValidationError):
                get_entity_dir(cfg, "../outside")

    def test_resolve_contained_path_rejects_traversal(self) -> None:
        """
        Generic contained path resolution should reject traversal.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(ValidationError):
                resolve_contained_path(tmp_dir, "..", "outside")

    def test_global_config_validation_checks_entity_handles(self) -> None:
        """
        Global config validation should reject unsafe entity registry keys.
        """
        with self.assertRaises(ValidationError):
            validate_global_config({"system": {}, "entities": {"bad-handle": {}}})

        messages = validate_global_config({"system": {}, "entities": {"good_handle_1": {}}})
        self.assertIn("Entity handles use safe naming.", messages)

    def test_entity_config_validation_finds_local_config(self) -> None:
        """
        Entity config validation should resolve the local config path safely.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            entity = "good_handle"
            entity_dir = Path(tmp_dir) / entity
            entity_dir.mkdir()
            (entity_dir / "config.toml").write_text("[settings]\n", encoding="utf-8")

            cfg = {
                "system": {"root_data_dir": tmp_dir},
                "entities": {entity: {"display_name": "Good Handle", "entity": entity}},
            }

            messages = validate_entity_config(cfg, entity)
            self.assertIn("Entity registry contains required keys.", messages)


if __name__ == "__main__":
    unittest.main()
