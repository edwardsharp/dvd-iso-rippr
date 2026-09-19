"""dependency-aware CLI; help and diagnostics work before Textual is installed."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import shutil
import sys
from pathlib import Path

from .ffmpeg import dependency_issues
from .scanner import find_isos
from .tool_setup import select_tools, setup_instructions

SETUP_HINT = (
    "from the checkout, run: python3 scripts/setup.py\n"
    "Windows: py -3 scripts/setup.py\n"
    "then use .venv/bin/python -m dvd_ripper (Windows: .venv\\Scripts\\python.exe).\n"
    "if already in a virtual environment, install with: python -m pip install -e .\n"
    "see README.md for setup; use --setup-ffmpeg for build instructions."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="interactively convert unencrypted dvd images to mp4."
    )
    parser.add_argument(
        "paths", nargs="*", type=Path, metavar="PATH", help="ISO/IMG files or directories"
    )
    parser.add_argument(
        "-o", "--output-dir", type=Path, help="output root (omit to choose/confirm in the UI)"
    )
    parser.add_argument("--recursive", action="store_true", help="include nested directories")
    parser.add_argument(
        "--no-mouse",
        action="store_true",
        help="leave mouse selection to the terminal; navigate the app with the keyboard",
    )
    parser.add_argument(
        "--check", action="store_true", help="check Python and FFmpeg dependencies, then exit"
    )
    parser.add_argument(
        "--setup-ffmpeg",
        action="store_true",
        help="show platform-specific build steps; change nothing",
    )
    parser.add_argument(
        "--ffmpeg", help="ffmpeg executable (default: project-local pair, then PATH)"
    )
    parser.add_argument(
        "--ffprobe", help="ffprobe executable (default: project-local pair, then PATH)"
    )
    args = parser.parse_args(argv)
    if args.setup_ffmpeg:
        print(setup_instructions())
        return 0
    if not args.check and not args.paths:
        parser.print_help()
        print("\nfirst time here?\n" + SETUP_HINT)
        return 2
    try:
        paths = find_isos(args.paths, recursive=args.recursive) if not args.check else []
        if not args.check and not paths:
            print(
                "no .iso or .img files found. use --recursive to include nested directories.",
                file=sys.stderr,
            )
            return 2
        ffmpeg, ffprobe = select_tools(args.ffmpeg, args.ffprobe)
        issues: list[str] = []
        if importlib.util.find_spec("textual") is None:
            issues.append("missing Python dependency: Textual.\n" + SETUP_HINT)
        issues += asyncio.run(dependency_issues(ffmpeg=ffmpeg, ffprobe=ffprobe))
        if args.check:
            print(f"Python {sys.version.split()[0]} ({sys.executable})")
            print(
                f"ffmpeg: {shutil.which(ffmpeg) or ffmpeg}\nffprobe: {shutil.which(ffprobe) or ffprobe}"
            )
        if issues:
            print("not ready to convert:", file=sys.stderr)
            for issue in issues:
                print(f"\n- {issue}", file=sys.stderr)
            print(
                "\nrun --setup-ffmpeg for build steps, or see docs/ffmpeg.md and README.md. "
                "use --ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe for another build.",
                file=sys.stderr,
            )
            return 1
        if args.check:
            print(
                "ready: Textual, ffmpeg/ffprobe dvdvideo + preindex, libx264, AAC, bwdif, and mp4 are available."
            )
            return 0
        try:
            from .app import DVDRipperApp
        except ImportError as exc:
            print(f"could not load the terminal UI: {exc}\n{SETUP_HINT}", file=sys.stderr)
            return 1
        DVDRipperApp(paths, output_dir=args.output_dir, ffmpeg=ffmpeg, ffprobe=ffprobe).run(
            mouse=not args.no_mouse
        )
        return 0
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
