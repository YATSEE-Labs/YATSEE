"""
CLI command registration and handlers for YATSEE configuration commands.

This module owns the `yatsee config ...` command group, including entity
registry operations, scaffold creation, validation, and config resolution.
"""

from __future__ import annotations

import argparse
import json

from yatsee.config_tools.entity import (
    add_entity,
    list_entities as list_registered_entities,
    purge_entity,
    remove_entity,
)
from yatsee.config_tools.resolve import resolve_config
from yatsee.config_tools.scaffold import build_entity_structure
from yatsee.config_tools.validate import validate_entity_config, validate_global_config
from yatsee.core.config import load_global_config


def register_config_commands(subparsers: argparse._SubParsersAction) -> None:
    """
    Register configuration-management CLI commands.

    :param subparsers: Root argparse subparser registry
    :return: None
    """
    # ----------------------------
    # config
    # ----------------------------
    config_parser = subparsers.add_parser("config", help="Configuration management commands")
    config_subparsers = config_parser.add_subparsers(dest="config_command")

    entity_parser = config_subparsers.add_parser("entity", help="Manage entity registry")
    entity_subparsers = entity_parser.add_subparsers(dest="entity_command")

    entity_list_parser = entity_subparsers.add_parser("list", help="List registered entities")
    entity_list_parser.set_defaults(handler=handle_config_entity_list)

    entity_add_parser = entity_subparsers.add_parser("add", help="Add an entity to the global registry")
    entity_add_parser.add_argument("--display-name", required=True, help="Human-friendly entity name")
    entity_add_parser.add_argument("--entity", required=True, help="Entity handle")
    entity_add_parser.add_argument("--base", default="", help="Optional namespace/base value")
    entity_add_parser.add_argument(
        "--no-create-dir",
        action="store_true",
        help="Do not create the top-level entity directory",
    )
    entity_add_parser.set_defaults(handler=handle_config_entity_add)

    entity_remove_parser = entity_subparsers.add_parser(
        "remove",
        help="Remove an entity from the global registry only",
    )
    entity_remove_parser.add_argument("-e", "--entity", required=True, help="Entity handle")
    entity_remove_parser.set_defaults(handler=handle_config_entity_remove)

    entity_purge_parser = entity_subparsers.add_parser(
        "purge",
        help="Purge an entity from both the registry and the local filesystem",
    )
    entity_purge_parser.add_argument("-e", "--entity", required=True, help="Entity handle")
    entity_purge_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be removed without making changes",
    )
    entity_purge_parser.set_defaults(handler=handle_config_entity_purge)

    init_parser = config_subparsers.add_parser("init", help="Create local entity config scaffolds")
    init_parser.add_argument("-e", "--entity", help="Initialize only one entity")
    init_parser.set_defaults(handler=handle_config_init)

    validate_parser = config_subparsers.add_parser("validate", help="Validate global and entity config")
    validate_parser.add_argument("-e", "--entity", help="Validate a specific entity as well")
    validate_parser.set_defaults(handler=handle_config_validate)

    resolve_parser = config_subparsers.add_parser("resolve", help="Print resolved runtime config")
    resolve_parser.add_argument("-e", "--entity", help="Resolve a specific entity")
    resolve_parser.add_argument(
        "--show-secrets",
        action="store_true",
        help="Print sensitive config values instead of redacting them",
    )
    resolve_parser.set_defaults(handler=handle_config_resolve)


def handle_config_entity_list(args: argparse.Namespace) -> int:
    """
    List registered entities from the global configuration.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    global_cfg = load_global_config(args.config)
    entities = list_registered_entities(global_cfg)

    if not entities:
        print("No entities defined.")
        return 0

    print("Registered entities:")
    for entity in entities:
        print(f"- {entity}")
    return 0


def handle_config_entity_add(args: argparse.Namespace) -> int:
    """
    Add an entity to the global registry.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    global_cfg = load_global_config(args.config)
    result = add_entity(
        global_cfg=global_cfg,
        config_path=args.config,
        display_name=args.display_name,
        entity=args.entity,
        base=args.base,
        create_dir=not args.no_create_dir,
    )
    print(result["message"])
    if result["entity_dir"]:
        print(f"Entity directory: {result['entity_dir']}")
    return 0


def handle_config_entity_remove(args: argparse.Namespace) -> int:
    """
    Remove an entity from the global registry.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    global_cfg = load_global_config(args.config)
    result = remove_entity(global_cfg=global_cfg, config_path=args.config, entity=args.entity)
    print(result["message"])
    return 0


def handle_config_entity_purge(args: argparse.Namespace) -> int:
    """
    Purge an entity from the registry and local filesystem.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    global_cfg = load_global_config(args.config)
    result = purge_entity(
        global_cfg=global_cfg,
        config_path=args.config,
        entity=args.entity,
        dry_run=args.dry_run,
    )

    print(result["message"])
    print(f"Entity: {result['entity']}")
    print(f"Registry entry exists: {result['registry_entry_exists']}")
    print(f"Entity directory: {result['entity_dir']}")
    print(f"Entity directory exists: {result['entity_dir_exists']}")
    print(f"Local config exists: {result['config_exists']}")
    print(f"Contained files: {result['file_count']}")
    print(f"Contained subdirectories: {result['dir_count']}")
    return 0


def handle_config_init(args: argparse.Namespace) -> int:
    """
    Create local entity config scaffolds.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    global_cfg = load_global_config(args.config)
    messages = build_entity_structure(global_cfg=global_cfg, entity=args.entity)
    for message in messages:
        print(message)
    return 0


def handle_config_validate(args: argparse.Namespace) -> int:
    """
    Validate global and optional entity configuration.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    global_cfg = load_global_config(args.config)

    for message in validate_global_config(global_cfg):
        print(message)

    if args.entity:
        for message in validate_entity_config(global_cfg, args.entity):
            print(message)

    print("Validation passed.")
    return 0


def handle_config_resolve(args: argparse.Namespace) -> int:
    """
    Print resolved runtime configuration.

    :param args: Parsed CLI arguments
    :return: Process exit code
    """
    resolved = resolve_config(
        global_config_path=args.config,
        entity=args.entity,
        redact=not args.show_secrets,
    )
    print(json.dumps(resolved, indent=2, sort_keys=True))
    return 0