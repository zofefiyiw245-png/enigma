"""Console entry point: parse CLI args, build the Flask app, open the browser."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import webbrowser

from . import __version__
from .server import create_app


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="enigma",
        description=(
            "Spoof the GPS location of a USB-attached, non-jailbroken iPhone. "
            "Launches a local web UI; the browser opens automatically."
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Interface to bind to. Default: 127.0.0.1 (localhost only).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5037,
        help="Port to listen on. Default: 5037.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not auto-open the browser after the server starts.",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run with a mock device (no iPhone required). Useful for testing the UI.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable Flask debug mode (verbose logs, auto-reload).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"enigma {__version__}",
    )
    return parser


def _open_browser_later(url: str, delay: float = 0.8) -> None:
    def _open() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_open, daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    use_mock = args.mock or os.environ.get("ENIGMA_MOCK") == "1"
    app = create_app(use_mock=use_mock)

    url = f"http://{args.host}:{args.port}/"
    print(f"Enigma {__version__} ready at {url}", flush=True)
    if use_mock:
        print("  (mock mode — no iPhone required)", flush=True)

    if not args.no_browser and args.host in {"127.0.0.1", "localhost", "0.0.0.0"}:
        _open_browser_later(url.replace("0.0.0.0", "127.0.0.1"))

    try:
        # threaded=True so concurrent UI polls don't serialize behind blocking
        # device calls; use_reloader=False so child processes don't double-launch.
        app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        ext = app.extensions.get("enigma", {})
        device = ext.get("device")
        if device is not None:
            device.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
