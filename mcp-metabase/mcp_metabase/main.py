from __future__ import annotations

import argparse
import json
import logging

from mcp_metabase.mcp_server import get_runtime, mcp


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-metabase")
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify Metabase API-key identity, version, and capabilities",
    )
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    args = _parser().parse_args()
    if args.check:
        health = get_runtime().health()
        print(json.dumps(health, ensure_ascii=False, sort_keys=True))
        return
    try:
        health = get_runtime().health()
    except Exception:  # pragma: no cover - startup must leave the health tool reachable.
        logging.getLogger("mcp-metabase").warning(
            "Metabase MCP startup preflight is unavailable; starting fail-closed.",
            exc_info=True,
        )
    else:
        if not health["writes_ready"]:
            logging.getLogger("mcp-metabase").warning(
                "Metabase MCP is starting in read-only degraded mode: status=%s version=%s",
                health["status"],
                health["server_version"],
            )
    mcp.run()


if __name__ == "__main__":
    main()
