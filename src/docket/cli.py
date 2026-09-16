"""Command-line entry point: `python -m docket.cli path/to/document.pdf`."""
from __future__ import annotations

import json
import sys

from .pipeline import process


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m docket.cli <path-to-document>", file=sys.stderr)
        raise SystemExit(1)

    result = process(sys.argv[1])
    print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
    if not result.is_valid:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
