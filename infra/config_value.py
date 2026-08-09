"""Print one scalar from a YAML file for the shell deployment scripts."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from typing import Any

import yaml


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("key", help="Dot-delimited mapping key")
    args = parser.parse_args()
    value: Any = yaml.safe_load(args.path.read_text(encoding="utf-8"))
    for part in args.key.split("."):
        value = value[part]
    if isinstance(value, str):
        missing = [name for name in re.findall(r"\$\{([^}]+)\}", value) if name not in os.environ]
        if missing:
            raise ValueError(f"Unset environment variable: {missing[0]}")
        value = re.sub(r"\$\{([^}]+)\}", lambda match: os.environ[match.group(1)], value)
    if isinstance(value, bool):
        print(str(value).lower())
    elif isinstance(value, (str, int, float)):
        print(value)
    else:
        raise TypeError(f"Configuration value is not scalar: {args.key}")


if __name__ == "__main__":
    main()
