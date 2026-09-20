"""CLI for fetching the full text of a CENDOJ resolution.

Usage::

    python -m navaja.cli <document_url>
    navaja-doc <document_url>
"""

from __future__ import annotations

import argparse
import sys

from navaja.captcha import resolve_captcha_host
from navaja.cendoj import CendojClient
from navaja.documents import FullTextResult


def _preview(text: str, limit: int = 500) -> str:
    snippet = text[:limit]
    return snippet.replace("\n", " ")


def _report(result: FullTextResult, out_path: str | None) -> None:
    status = "success" if result.ok else "failure"
    print(f"Result: {status}")
    print(f"Attempts: {result.attempts}")
    print(f"Content-Type: {result.content_type}")
    print(f"Text preview: {_preview(result.text)}")
    if result.error:
        print(f"Error: {result.error}")

    if out_path is not None:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(result.text)
        print(f"Wrote {len(result.text)} characters to {out_path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch the full text of a CENDOJ resolution",
    )
    parser.add_argument("url", help="CENDOJ document URL")
    parser.add_argument(
        "--host",
        default=None,
        help=(
            "interface for the local captcha form "
            "(default: auto-detect: NAVAJA_CAPTCHA_HOST, then tailscale0 IPv4, then 127.0.0.1)"
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="port for the local captcha form (default: 8765)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="seconds to wait for a captcha answer (default: 300)",
    )
    parser.add_argument(
        "--out",
        help="write extracted text to this file",
    )
    args = parser.parse_args(argv)

    if args.host is None:
        host, host_reason = resolve_captcha_host()
        print(
            f"Captcha form will bind to {host} because {host_reason}",
            file=sys.stderr,
        )
    else:
        host = args.host

    try:
        with CendojClient() as client:
            result = client.fetch_full_text(
                args.url,
                host=host,
                port=args.port,
                timeout=args.timeout,
            )
    except Exception as exc:  # pragma: no cover - CLI error path
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    _report(result, args.out)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
