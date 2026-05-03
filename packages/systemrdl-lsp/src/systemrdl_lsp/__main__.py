"""Entry point: `systemrdl-lsp` (stdio LSP) and `python -m systemrdl_lsp`."""

from __future__ import annotations

import argparse
import logging
import sys

from systemrdl_lsp import __version__
from systemrdl_lsp.server import build_server


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="systemrdl-lsp",
        description="Language Server for SystemRDL 2.0 (stdio).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: WARNING). Logs go to stderr.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    # Swap pygls's sync stdout writer for our async one BEFORE start_io.
    # The sync writer blocks the asyncio loop on every flush — fine for
    # small responses, fatal for the multi-MB spine envelopes a 25k-reg
    # design produces (the loop stalls for seconds while vscode drains
    # the pipe, and every other in-flight LSP request waits behind it).
    from systemrdl_lsp._async_writer import install as _install_async_writer
    _install_async_writer()

    server = build_server()
    server.start_io()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
