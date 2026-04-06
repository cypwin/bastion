"""BASTION TUI Dashboard v2 — priority-ranked panels with switchable layouts."""
from __future__ import annotations

import argparse
import sys

import httpx


def _detect_admin_url(base_url: str) -> str:
    """Try to detect the admin API URL.

    First attempts ``/broker/health`` on the given *base_url*.  If that
    fails, falls back to the same host on port 9999.  Returns whichever
    URL responds successfully, or *base_url* as a last resort.
    """
    candidates = [base_url]
    # Build a port-9999 variant
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(base_url)
    alt = parsed._replace(netloc=f"{parsed.hostname}:9999")
    candidates.append(urlunparse(alt))

    for url in candidates:
        try:
            resp = httpx.get(f"{url.rstrip('/')}/broker/health", timeout=3.0)
            if resp.status_code < 500:
                return url
        except Exception:
            continue

    return base_url


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for the BASTION dashboard."""
    parser = argparse.ArgumentParser(
        prog="bastion-dashboard",
        description="BASTION TUI Dashboard — GPU/LLM broker monitor",
    )
    parser.add_argument(
        "--url",
        default="http://localhost:11434",
        help="Base URL for the BASTION proxy (default: http://localhost:11434)",
    )
    parser.add_argument(
        "--admin-url",
        default=None,
        help="Explicit admin API URL (auto-detected if omitted)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Refresh interval in seconds (default: 2.0)",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key for authenticated endpoints",
    )
    parser.add_argument(
        "--layout",
        choices=["compact", "standard", "full"],
        default="standard",
        help="Initial layout mode (default: standard)",
    )
    parser.add_argument(
        "--sparkline-width",
        type=int,
        default=None,
        help="Characters per sparkline (default: 20)",
    )
    parser.add_argument(
        "--history-len",
        type=int,
        default=None,
        help="Samples kept in history deques (default: 120)",
    )

    args = parser.parse_args(argv)

    # Apply sparkline/history overrides before importing app
    if args.sparkline_width is not None:
        import bastion.dashboard.helpers as _h
        _h.SPARKLINE_WIDTH = args.sparkline_width
    if args.history_len is not None:
        import bastion.dashboard.helpers as _h
        _h.HISTORY_LEN = args.history_len

    # Determine admin URL
    url = args.admin_url or _detect_admin_url(args.url)

    from bastion.dashboard.app import BastionDashboard

    app = BastionDashboard(
        url=url,
        interval=args.interval,
        api_key=args.api_key,
        layout_mode=args.layout,
    )
    app.run()


if __name__ == "__main__":
    main()
