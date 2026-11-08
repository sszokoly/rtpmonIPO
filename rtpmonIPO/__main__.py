"""Module entry point for the rtpmonIPO library."""

from __future__ import annotations

import argparse


def main() -> None:
    """Provide basic module invocation and version discovery."""
    parser = argparse.ArgumentParser(
        description="Asynchronous Avaya IP Office SysMonitor RTP summary library."
    )
    parser.add_argument("--version", action="version", version="rtpmonIPO 0.1.0")
    parser.parse_args()


if __name__ == "__main__":
    main()
