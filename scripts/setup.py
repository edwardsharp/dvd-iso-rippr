"""bootstrap a local installation using only the standard library (python 3.8 syntax)."""

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

MIN_PYTHON = (3, 11)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = os.name == "nt"
CREATE_TIMEOUT = 120
INSTALL_TIMEOUT = 600
CHECK_TIMEOUT = 120

PYTHON_GUIDANCE = """use a full python 3.11+ installation; keep your existing python if it meets that requirement.
  macos: if needed, brew install python@3.13, then python3.13 scripts/setup.py
  debian/ubuntu: install python3, python3-venv and python3-pip using your package manager;
    check python3 --version (older distributions may need a newer python package).
  windows: install full python from python.org with pip and the launcher,
    then py -3.13 scripts/setup.py (or select another installed python >=3.11).
this script does not install or upgrade python or run a system package manager."""

VENV_GUIDANCE = """make sure this python has venv and ensurepip support and the project is writable.
on debian/ubuntu, install python3-venv (or the matching python3.X-venv package).
if a partial .venv was created, inspect it and move it aside before retrying.
no existing directory will be deleted automatically."""

FFMPEG_GUIDANCE = """next steps for ffmpeg:
  install ffmpeg and ffprobe, or point the cli to them with --ffmpeg PATH --ffprobe PATH.
  both need the dvdvideo demuxer: use ffmpeg 7+ built with --enable-libdvdnav
  and --enable-libdvdread; ffmpeg also needs libx264 and aac encoding.
  an ordinary ffmpeg package (including homebrew's) may lack dvdvideo.
  'Unknown format dvdvideo' means it is missing, even when the command exits 0.
  see README.md for feature checks. reinstalling python will not add dvd support.
  build help: python -m dvd_ripper --setup-ffmpeg
  local build: python scripts/build_ffmpeg.py"""


class SetupError(Exception):
    """an actionable bootstrap failure."""


def command_text(command):
    # these strings are for display only; subprocess always receives an argument list.
    if IS_WINDOWS:
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def venv_python(venv_dir):
    if IS_WINDOWS:
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def run_command(command, timeout, label, guidance="", capture=False, required=True):
    try:
        result = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            shell=False,
            timeout=timeout,
            check=False,
            capture_output=capture,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise SetupError(
            f"{label} timed out after {timeout} seconds.\n"
            f"command: {command_text(command)}\n{guidance}"
        ) from exc
    except OSError as exc:
        raise SetupError(
            f"could not run {label}: {exc}\ncommand: {command_text(command)}\n{guidance}"
        ) from exc
    if required and result.returncode != 0:
        details = ""
        if capture:
            details = "\n" + (result.stdout or "") + (result.stderr or "")
        raise SetupError(
            f"{label} failed (exit {result.returncode}).{details}\n"
            f"command: {command_text(command)}\n{guidance}"
        )
    return result


def validate_venv_layout(venv_dir):
    config = venv_dir / "pyvenv.cfg"
    if (
        venv_dir.is_symlink()
        or not venv_dir.is_dir()
        or config.is_symlink()
        or not config.is_file()
        or not venv_python(venv_dir).is_file()
    ):
        raise SetupError(
            f"refusing to modify {venv_dir}: it is not a usable local virtual environment "
            "(or is a symlink).\ninspect it and move it aside yourself if appropriate, "
            "then rerun setup. its contents have not been removed."
        )


def prepare_venv():
    venv_dir = PROJECT_ROOT / ".venv"
    if venv_dir.exists() or venv_dir.is_symlink():
        validate_venv_layout(venv_dir)
        print(f"reusing {venv_dir} (no python upgrade).", flush=True)
    else:
        print(f"creating {venv_dir} with {sys.executable}.", flush=True)
        run_command(
            [sys.executable, "-m", "venv", str(venv_dir)],
            CREATE_TIMEOUT,
            "virtual environment creation",
            VENV_GUIDANCE + "\n" + PYTHON_GUIDANCE,
        )
        validate_venv_layout(venv_dir)

    python = str(venv_python(venv_dir))
    result = run_command(
        [
            python,
            "-c",
            (
                "import json, sys; print(json.dumps({'version': list(sys.version_info[:3]), "
                "'prefix': sys.prefix, 'base_prefix': sys.base_prefix}))"
            ),
        ],
        CHECK_TIMEOUT,
        "virtual environment validation",
        "the existing .venv may be broken or moved. inspect and move it aside, then rerun setup.",
        capture=True,
    )
    try:
        info = json.loads(result.stdout)
        version = tuple(info["version"])
        prefix = Path(info["prefix"]).resolve()
        base_prefix = Path(info["base_prefix"]).resolve()
        if prefix != venv_dir.resolve() or prefix == base_prefix:
            raise ValueError("interpreter is not running inside the project's .venv")
        if version[:2] < MIN_PYTHON:
            version_text = ".".join(map(str, version))
            raise ValueError(f"the existing .venv uses python {version_text}")
    except (ValueError, KeyError, TypeError) as exc:
        raise SetupError(
            f"cannot use {venv_dir}: {exc}.\ninspect and move the existing .venv aside, then rerun "
            "setup with python >=3.11. no automatic replacement was attempted."
        ) from exc
    return python


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="create/reuse .venv, install dvd-ripper, and check runtime dependencies."
    )
    parser.add_argument(
        "--dev", action="store_true", help="also install pytest, pytest-asyncio and ruff"
    )
    args = parser.parse_args(argv)

    if sys.version_info[:2] < MIN_PYTHON:
        version_text = ".".join(map(str, sys.version_info[:3]))
        print(
            f"setup requires python >=3.11; you are running {version_text}.\n{PYTHON_GUIDANCE}",
            file=sys.stderr,
        )
        return 1
    if not (PROJECT_ROOT / "pyproject.toml").is_file():
        print(
            f"cannot find pyproject.toml in {PROJECT_ROOT}. "
            "run this script from a complete project checkout.",
            file=sys.stderr,
        )
        return 1

    try:
        python = prepare_venv()
        requirement = str(PROJECT_ROOT) + ("[dev]" if args.dev else "")
        extra = " and development tools" if args.dev else ""
        ensurepip_command = command_text([python, "-m", "ensurepip", "--upgrade"])
        print(
            f"installing the editable package{extra} using pip's configured package index.\n"
            "this may download python dependencies; it will not install python or ffmpeg.",
            flush=True,
        )
        run_command(
            [
                python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "-e",
                requirement,
            ],
            INSTALL_TIMEOUT,
            "package installation",
            "check the pip output above, network/proxy/index settings and available disk space.\n"
            f"if pip is missing, run {ensurepip_command} then rerun setup.\n"
            f"do not use sudo pip; dependencies belong in .venv.\n{VENV_GUIDANCE}",
        )
        run_command(
            [python, "-c", "import dvd_ripper.cli; import textual"],
            CHECK_TIMEOUT,
            "python package import check",
            "the package installed but could not be imported. review the traceback above; "
            "rerun setup with the same python to repair missing dependencies.",
        )
        print(
            "python environment and package installation succeeded. checking runtime capabilities...",
            flush=True,
        )
        check_command = [python, "-m", "dvd_ripper", "--check"]
        result = run_command(
            check_command,
            CHECK_TIMEOUT,
            "runtime capability check",
            "the python installation completed, but the runtime check could not finish.\n"
            f"rerun {command_text(check_command)} and review the output.\n{FFMPEG_GUIDANCE}",
            required=False,
        )
        if result.returncode:
            # --check uses a nonzero status for missing runtime capabilities. the
            # successful install is still useful for fixing/checking those tools.
            print(
                "\nwarning: python setup completed, but the runtime check did not pass "
                f"(exit {result.returncode}).\n"
                "review its output above; conversion is not ready until the check passes.\n"
                f"{FFMPEG_GUIDANCE}\nrerun: {command_text(check_command)}",
                flush=True,
            )
        else:
            print("runtime capability check passed.", flush=True)
        help_command = command_text([python, "-m", "dvd_ripper", "--help"])
        print(
            f"\nrun without activation:\n  {help_command}\n"
            "or activate .venv (see README.md) and run dvd-ripper.\n"
            "use --help for input and output options.",
            flush=True,
        )
        return 0
    except SetupError as exc:
        print(f"setup error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "setup interrupted. no environment was deleted. if .venv is incomplete, "
            "inspect and move it aside before retrying.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
