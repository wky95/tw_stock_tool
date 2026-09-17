"""Phase 0 operational commands."""

from __future__ import annotations

import argparse
import json
import sys
from importlib.metadata import version
from pathlib import Path

import yaml
from pydantic import ValidationError

from island_quant.config import load_settings
from island_quant.logging import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="island-quant")
    parser.add_argument("--config", type=Path, default=Path("config/default.yaml"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-config", help="validate configuration and safety invariants")
    subparsers.add_parser("show-config", help="print effective non-secret configuration")
    subparsers.add_parser("doctor", help="check the Phase 0 installation")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config)
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(settings.logging)
    if args.command == "validate-config":
        print(f"valid: {args.config}")
    elif args.command == "show-config":
        print(json.dumps(settings.safe_dump(), ensure_ascii=False, indent=2))
    elif args.command == "doctor":
        result = {"package": "island-quant", "version": version("island-quant"), "status": "ok"}
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

