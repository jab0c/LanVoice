#!/usr/bin/env python3
"""One-command build for LAN Voice (platform-aware)."""
from __future__ import annotations

import argparse
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_ENTRY = ROOT / "lan_voice_lite.py"
DEFAULT_BUNDLE_ID = "com.lanvoice.app"
DEFAULT_MIC_DESC = "LAN Voice needs microphone access for ultra low-latency LAN voice chat."


def run(cmd: list[str]) -> None:
    print("+", " ".join(str(c) for c in cmd))
    subprocess.check_call(cmd)


def run_optional(cmd: list[str]) -> None:
    print("+", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=False)


def choose_mode(requested: str | None, sysname: str) -> str:
    if requested:
        return requested
    if sysname == "Windows":
        return "onefile"
    return "onedir"


def find_app(dist_dir: Path, preferred_name: str | None) -> Path:
    apps = list(dist_dir.rglob("*.app"))
    if not apps:
        raise FileNotFoundError(f"No .app found under {dist_dir}")
    if preferred_name:
        preferred = f"{preferred_name}.app"
        for app in apps:
            if app.name == preferred:
                return app
    return apps[0]


def patch_plist(app_path: Path, bundle_id: str | None, mic_desc: str | None) -> None:
    plist_path = app_path / "Contents" / "Info.plist"
    if not plist_path.exists():
        raise FileNotFoundError(f"Missing Info.plist at {plist_path}")
    with plist_path.open("rb") as f:
        plist = plistlib.load(f)

    changed = False
    if bundle_id and plist.get("CFBundleIdentifier") != bundle_id:
        plist["CFBundleIdentifier"] = bundle_id
        changed = True
    if mic_desc and plist.get("NSMicrophoneUsageDescription") != mic_desc:
        plist["NSMicrophoneUsageDescription"] = mic_desc
        changed = True
    # Make sure display name is set (cosmetic)
    if "CFBundleDisplayName" not in plist and "CFBundleName" in plist:
        plist["CFBundleDisplayName"] = plist["CFBundleName"]
        changed = True

    if changed:
        with plist_path.open("wb") as f:
            plistlib.dump(plist, f)


def build_windows(args: argparse.Namespace) -> None:
    entry = Path(args.entry).resolve()
    if not entry.exists():
        raise FileNotFoundError(f"Entry file not found: {entry}")

    name = args.name or "LANVoice"
    mode = choose_mode(args.mode, "Windows")
    icon = Path(args.icon) if args.icon else (ROOT / "lanvoice.ico")

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        name,
    ]
    cmd.append("--onefile" if mode == "onefile" else "--onedir")
    if icon.exists():
        cmd += ["--icon", str(icon)]
    else:
        print("! Windows icon not found; building without --icon")
    cmd.append(str(entry))
    run(cmd)


def build_macos(args: argparse.Namespace) -> None:
    entry = Path(args.entry).resolve()
    if not entry.exists():
        raise FileNotFoundError(f"Entry file not found: {entry}")

    name = args.name or "LAN Voice"
    mode = choose_mode(args.mode, "Darwin")
    icon = Path(args.icon) if args.icon else (ROOT / "lanvoice.icns")

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        name,
        "--osx-bundle-identifier",
        args.bundle_id,
    ]
    cmd.append("--onefile" if mode == "onefile" else "--onedir")
    if icon.exists():
        cmd += ["--icon", str(icon)]
    else:
        print("! macOS icon not found; building without --icon")
    cmd.append(str(entry))
    run(cmd)

    dist_dir = ROOT / "dist"
    app_path = find_app(dist_dir, name)
    patch_plist(app_path, args.bundle_id, args.mic_desc)

    run(["codesign", "--force", "--deep", "--sign", "-", str(app_path)])

    if args.install:
        install_dir = Path(args.install_dir)
        dest = install_dir / app_path.name
        try:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(app_path, dest)
        except PermissionError:
            print("! Permission denied installing to /Applications.")
            print("  Re-run with sudo, or pass --install-dir to a writable folder.")
            return

        run_optional(["tccutil", "reset", "Microphone", args.bundle_id])
        run_optional(["killall", "tccd"])
        run_optional(["open", str(dest)])


def build_other(args: argparse.Namespace) -> None:
    entry = Path(args.entry).resolve()
    if not entry.exists():
        raise FileNotFoundError(f"Entry file not found: {entry}")

    name = args.name or "lan-voice"
    mode = choose_mode(args.mode, platform.system())
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        name,
    ]
    cmd.append("--onefile" if mode == "onefile" else "--onedir")
    cmd.append(str(entry))
    run(cmd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build LAN Voice for the current OS.")
    parser.add_argument("--entry", default=str(DEFAULT_ENTRY), help="Entry-point script")
    parser.add_argument("--name", help="App name (overrides default for the OS)")
    parser.add_argument("--mode", choices=["onefile", "onedir"], help="PyInstaller mode")
    parser.add_argument("--icon", help="Path to icon file")

    parser.add_argument("--bundle-id", default=DEFAULT_BUNDLE_ID, help="macOS bundle identifier")
    parser.add_argument("--mic-desc", default=DEFAULT_MIC_DESC, help="macOS mic permission text")
    parser.add_argument("--install", action="store_true", help="macOS only: copy to /Applications, reset TCC, open")
    parser.add_argument("--install-dir", default="/Applications", help="macOS only: install destination")

    args = parser.parse_args()
    sysname = platform.system()

    if sysname == "Windows":
        build_windows(args)
    elif sysname == "Darwin":
        build_macos(args)
    else:
        build_other(args)

    print("Build complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
