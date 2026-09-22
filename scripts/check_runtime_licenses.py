"""Reject copyleft packages from a pip-licenses JSON report."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

FORBIDDEN = re.compile(
    r"(?:^|\W)(?:A?GPL|LGPL)(?:\W|$)|GNU\s+(?:Affero\s+|Lesser\s+)?General\s+Public\s+License",
    re.IGNORECASE,
)
TOOLS = {"pip", "pip-licenses", "prettytable", "setuptools", "wcwidth"}


def violations(packages: list[dict[str, str]]) -> list[str]:
    return [
        f"{item['Name']} {item['Version']}: {item.get('License', 'UNKNOWN')}"
        for item in packages
        if item["Name"].lower() not in TOOLS and FORBIDDEN.search(item.get("License", ""))
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    found = violations(json.loads(args.report.read_text()))
    if found:
        raise SystemExit("copyleft runtime dependencies found:\n  " + "\n  ".join(found))


if __name__ == "__main__":
    main()
