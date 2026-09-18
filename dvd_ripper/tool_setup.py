"""project-local tool selection and platform-specific setup instructions."""

from __future__ import annotations

import platform
import shlex
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def select_tools(ffmpeg: str | None = None, ffprobe: str | None = None) -> tuple[str, str]:
    # explicit overrides bypass local discovery; otherwise keep the pair together.
    if ffmpeg is not None or ffprobe is not None:
        return ffmpeg or "ffmpeg", ffprobe or "ffprobe"
    directory = PROJECT_ROOT / ".tools" / "ffmpeg" / "bin"
    pair = directory / "ffmpeg", directory / "ffprobe"
    if any(path.exists() or path.is_symlink() for path in pair):
        if not all(path.is_file() for path in pair):
            raise ValueError(
                f"incomplete local ffmpeg build: {directory}; both ffmpeg and ffprobe are needed. "
                "run --setup-ffmpeg for repair instructions, or specify both tool paths."
            )
        return str(pair[0]), str(pair[1])
    return "ffmpeg", "ffprobe"


def setup_instructions() -> str:
    system = platform.system()
    if system == "Darwin":
        prerequisites = (
            "macos prerequisites (run yourself):\n"
            "  xcode-select --install\n"
            "  brew install pkgconf nasm libdvdnav libdvdread x264"
        )
    elif system == "Linux":
        try:
            release = platform.freedesktop_os_release()
        except OSError:
            release = {}
        family = {release.get("ID", ""), *release.get("ID_LIKE", "").split()}
        if family & {"debian", "raspbian", "ubuntu"}:
            prerequisites = (
                "debian / raspberry pi os prerequisites (run yourself):\n"
                "  sudo apt update\n"
                "  sudo apt install build-essential pkg-config nasm libdvdnav-dev "
                "libdvdread-dev libx264-dev ca-certificates"
            )
        else:
            prerequisites = (
                "linux: use your package manager for a C compiler, make, pkg-config, "
                "dvdnav/dvdread/x264 development libraries, and ca certificates. "
                "x86 also needs nasm. see docs/ffmpeg.md for debian commands."
            )
    else:
        return (
            "the build helper supports debian / raspberry pi os and macos.\n"
            "on other systems, supply a suitable build using --ffmpeg and --ffprobe.\n"
            "both tools need dvdvideo with preindex; ffmpeg also needs libx264, aac, bwdif, "
            "and the mp4 muxer. see docs/ffmpeg.md."
        )
    script = PROJECT_ROOT / "scripts" / "build_ffmpeg.py"
    return (
        f"{prerequisites}\n\n"
        f"from the checkout ({PROJECT_ROOT}):\n"
        "  python3 scripts/build_ffmpeg.py --dry-run\n"
        "  python3 scripts/build_ffmpeg.py --jobs 1\n"
        "  .venv/bin/python -m dvd_ripper --check\n\n"
        f"from another directory: python3 {shlex.quote(str(script))}\n"
        "the app prefers .tools/ffmpeg/bin; system ffmpeg is unchanged.\n"
        "the build downloads pinned source, checks its sha256, and uses configure + make.\n"
        "no packages are installed automatically. a pi build can take hours; "
        "software x264 encoding can also be slow.\n"
        "see docs/ffmpeg.md for logs, rebuilds, and prerequisites."
    )
