"""Run the localhost gateway demo: ``python -m gateway [--port 8035]``.

Loopback only, by design: this demo has no TLS and a plaintext key store, so it
refuses to bind anything but 127.0.0.1/localhost/::1. The M2 deployment (TLS,
encrypted store) is the first version allowed to face a network.
"""
from __future__ import annotations

import argparse
import logging

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        prog="python -m gateway",
        description="Localhost demo of the hosted Moneybird MCP gateway (M1).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Loopback bind address only.")
    parser.add_argument("--port", type=int, default=8035)
    args = parser.parse_args(argv)

    if args.host not in LOOPBACK_HOSTS:
        raise SystemExit(
            f"Refusing to bind {args.host}: the demo gateway is loopback-only "
            "(no TLS, plaintext key store). See docs/hosted_gateway_design.md M2."
        )

    import uvicorn

    from .app import build_gateway_app

    logging.getLogger("gateway").info(
        "Gateway demo on http://%s:%s — open it in a browser to connect Moneybird.",
        args.host,
        args.port,
    )
    uvicorn.run(build_gateway_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
