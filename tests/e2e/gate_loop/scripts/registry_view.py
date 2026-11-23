#!/usr/bin/env python3
"""Read Gate's /v1/registry JSON on stdin: `count` prints the node count, `actions` the advertised actions."""

import json
import sys


def main(mode: str) -> None:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if mode == "count":
        print(len(data))
    else:
        print(
            json.dumps(
                {
                    k: sorted(v.get("supported_actions", []))
                    for k, v in data.items()
                    if isinstance(v, dict)
                }
            )
        )


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "actions")
