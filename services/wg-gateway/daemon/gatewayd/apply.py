"""Command line entry point for the gateway daemon."""

from __future__ import annotations

import os

import uvicorn

DEFAULT_PORT = 80


def main() -> None:
	"""Serve the API on the configured bind address."""
	uvicorn.run(
		"gatewayd.main:app",
		host=os.environ.get("WG_GATEWAY_BIND", "127.0.0.1"),
		port=int(os.environ.get("WG_GATEWAY_PORT", DEFAULT_PORT)),
	)


if __name__ == "__main__":
	main()
