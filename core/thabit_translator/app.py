#!/usr/bin/env python3
"""Thabit Translator - entry point, used as a CLI or imported as a library.

CLI (no arguments opens the interactive menu); any of these work:

    thabit-translator <mode> [...]                 # pipx / pip installed
    python3 -m thabit_translator <mode> [...]      # from src/core
    python3 core/thabit_translator/__main__.py <mode> [...]   # from the repo

The script bootstraps its dependencies before anything else runs: in a source
checkout or under the Jellyfin plugin it creates/reuses the sibling venv
(`src/.venv_thabit` or `<plugin data>/.venv_thabit`) and re-executes itself
inside it; under pipx - already a venv - it repairs that venv in place instead
and never re-execs.

Library: import `core.*` / `providers.*` from inside that same venv - importing
    has no side effects beyond the environment variables set below, and there is
    no UI left: NiceGUI (`library/ui/`, the `-ui` flag) was removed, and the web
    app that followed it (FastAPI + Svelte) has since been deleted - this module
    is driven by the CLI or by a subprocess caller such as the Jellyfin plugin.
"""

import json
import os
import sys
import warnings
import shutil
import subprocess

os.environ["ARGOS_DEVICE_TYPE"] = "cpu"
os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["PYTORCH_NO_CUDA_MEMORY_CACHING"] = "1"
warnings.filterwarnings("ignore", message=".*CUDA.*", category=UserWarning)

# The sibling modules are imported flat (`from cli import ...`, `from core...`).
# Running this file as a script already puts its directory on sys.path; the pipx
# console-script entry (thabit_translator.app:main) does not, so add it once.
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
if _PACKAGE_DIR not in sys.path:
    sys.path.insert(0, _PACKAGE_DIR)


def bootstrap_environment(verbose=False):
    script_dir = _PACKAGE_DIR
    # Two levels up: src/ (checkout), <plugin data>/ (Jellyfin plugin), or the
    # pipx venv's site-packages root - only the first two own a venv.
    base_dir = os.path.realpath(os.path.join(script_dir, "..", ".."))
    venv_dir = os.path.join(base_dir, ".venv_thabit")
    venv_python = os.path.join(venv_dir, "bin", "python3")
    if not os.path.exists(venv_python):
        venv_python = os.path.join(venv_dir, "bin", "python")

    # pip package name -> import name
    REQUIRED_PACKAGES = {
        "argostranslate": "argostranslate",
        "srt": "srt",
        "imageio-ffmpeg": "imageio_ffmpeg",
        "requests": "requests",
        "chardet": "chardet",
        "vosk": "vosk",
        "guessit": "guessit",
    }
    IMPORT_NAMES = list(REQUIRED_PACKAGES.values()) + ["torch"]
    CHECK_CODE = (
        "import importlib, json\n"
        f"names = {IMPORT_NAMES!r}\n"
        "missing = []\n"
        "for name in names:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except Exception:\n"
        "        missing.append(name)\n"
        "print(json.dumps(missing))\n"
    )

    def running_in_venv():
        return os.path.realpath(sys.prefix) == os.path.realpath(venv_dir)

    def running_in_foreign_venv():
        # Inside some venv that is not the sibling this script manages (pipx,
        # a manually activated venv): sys.prefix differs from base_prefix.
        return (
            os.path.realpath(sys.prefix) != os.path.realpath(sys.base_prefix)
            and not running_in_venv()
        )

    def missing_packages(python):
        """Import names `python` cannot import, or None if that call fails."""
        try:
            res = subprocess.run(
                [python, "-c", CHECK_CODE],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if res.returncode != 0:
            return None
        try:
            return json.loads(res.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None

    def install(missing, python, dest):
        torch_failed = False
        if "torch" in missing:
            # argostranslate -> stanza -> torch, and the default PyPI torch wheel
            # drags in several GB of CUDA libraries. This tool is CPU-only.
            print("[INFO] Installing CPU-only torch", flush=True)
            try:
                res = subprocess.run(
                    [
                        python,
                        "-m",
                        "pip",
                        "install",
                        "--upgrade",
                        "torch",
                        "--index-url",
                        "https://download.pytorch.org/whl/cpu",
                    ]
                )
                torch_failed = res.returncode != 0
            except OSError:
                torch_failed = True
            if torch_failed:
                print("[WARN] CPU torch index unavailable, falling back to PyPI")
            missing = [name for name in missing if name != "torch"]

        pip_names = [
            pkg for pkg, imp in REQUIRED_PACKAGES.items() if imp in missing
        ]
        if torch_failed:
            pip_names.append("torch")
        if not pip_names:
            return
        print(
            f"[INFO] Installing into {dest}: " + ", ".join(pip_names),
            flush=True,
        )
        try:
            res = subprocess.run(
                [python, "-m", "pip", "install", "--upgrade"] + pip_names
            )
        except OSError as exc:
            print(f"[ERROR] pip could not run: {exc}")
            sys.exit(1)
        if res.returncode != 0:
            print("[ERROR] pip install failed (see output above).")
            sys.exit(1)

    def verify(python, dest):
        still_missing = missing_packages(python)
        if still_missing is None or still_missing:
            names = ", ".join(still_missing) if still_missing else "unknown problem"
            print(f"[ERROR] Virtual environment is unusable after install: {dest}")
            print(f"[ERROR] Still missing after install: {names}")
            print("Please delete that folder and restart the script.")
            sys.exit(1)

    # Case 1: pipx (or any foreign venv) - repair it in place, never re-exec:
    # the caller's interpreter keeps owning the process and its entry point.
    if running_in_foreign_venv():
        missing = missing_packages(sys.executable) or list(
            REQUIRED_PACKAGES.values()
        ) + ["torch"]
        if missing:
            install(missing, sys.executable, sys.prefix)
            verify(sys.executable, sys.prefix)
        return

    # Case 2: system python (checkout / plugin subprocess) - sibling venv.
    missing = missing_packages(venv_python)

    if missing is None:
        if running_in_venv():
            print(f"[ERROR] Virtual environment is broken: {venv_dir}")
            print("Please delete that folder and restart the script.")
            sys.exit(1)
        if os.path.isdir(venv_dir):
            print(f"[INFO] Rebuilding broken virtual environment: {venv_dir}")
            shutil.rmtree(venv_dir)
        print(f"[INFO] Creating virtual environment: {venv_dir}", flush=True)
        try:
            subprocess.run([sys.executable, "-m", "venv", venv_dir], check=True)
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"[ERROR] Could not create virtual environment: {exc}")
            sys.exit(1)
        venv_python = os.path.join(venv_dir, "bin", "python3")
        if not os.path.exists(venv_python):
            venv_python = os.path.join(venv_dir, "bin", "python")
        missing = list(REQUIRED_PACKAGES.values()) + ["torch"]

    if missing:
        install(missing, venv_python, venv_dir)
        verify(venv_python, venv_dir)

    if running_in_venv():
        return

    os.execv(venv_python, [venv_python, os.path.abspath(__file__)] + sys.argv[1:])


def ensure_ffmpeg_on_path():
    """Jellyfin's images keep ffmpeg/ffprobe only under /usr/lib/jellyfin-ffmpeg,
    which is not on PATH: prepend it (once, in this process and every child it
    spawns) whenever the tools are otherwise unreachable."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    jellyfin_ffmpeg = "/usr/lib/jellyfin-ffmpeg"
    if os.path.isdir(jellyfin_ffmpeg):
        os.environ["PATH"] = jellyfin_ffmpeg + os.pathsep + os.environ.get("PATH", "")


def check_system_requirements(verbose=True):
    try:
        with open("/etc/os-release", "r") as f:
            os_release = {}
            for line in f:
                if "=" in line:
                    key, value = line.strip().split("=", 1)
                    os_release[key] = value.strip('"')
    except FileNotFoundError:
        if verbose:
            print("[ERROR] Cannot determine Linux distribution.")
        return False

    dist_id = os_release.get("ID", "")
    dist_like = os_release.get("ID_LIKE", "")
    is_ubuntu_based = (
        dist_id in ("ubuntu", "debian")
        or "ubuntu" in dist_like
        or "debian" in dist_like
    )
    if not is_ubuntu_based:
        if verbose:
            print(f"[ERROR] Ubuntu/Debian required. Found: {dist_id}")
        return False

    missing_critical = []
    if not shutil.which("ffprobe") and not shutil.which("ffmpeg"):
        missing_critical.append("ffmpeg")

    if missing_critical:
        if verbose:
            print("[ERROR] Missing: " + ", ".join(missing_critical))
        return False

    if verbose:
        print(f"[INFO] Running on {dist_id}")
        try:
            res = subprocess.run(
                ["ffmpeg", "-version"], capture_output=True, text=True, timeout=5
            )
            if res.returncode == 0:
                print(f"[INFO] {res.stdout.splitlines()[0]}")
        except Exception:
            pass

    return True


def main():
    ensure_ffmpeg_on_path()
    bootstrap_environment()
    if not check_system_requirements(verbose=True):
        sys.exit(1)
    from cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
