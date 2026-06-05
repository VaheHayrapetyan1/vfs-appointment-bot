"""Chrome CDP launch/quit and profile wipe after long VFS cooldown."""

from __future__ import annotations

import glob
import logging
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Tuple

from vfs_appointment_bot.utils.config_reader import get_config_value


def _truthy(raw: str | None, default: bool = True) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def cdp_port() -> int:
    try:
        return int(get_config_value("browser", "cdp_port", "9222") or "9222")
    except ValueError:
        return 9222


def cdp_user_data_dir() -> str:
    return (
        get_config_value("browser", "cdp_user_data_dir", "/tmp/vfs-debug-profile")
        or "/tmp/vfs-debug-profile"
    ).strip()


def is_cdp_port_open(port: int | None = None, host: str = "127.0.0.1") -> bool:
    port = port if port is not None else cdp_port()
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def ensure_cdp_chrome_ready() -> bool:
    """
    If ``[browser] auto_start_cdp`` is enabled and nothing listens on the CDP port,
    launch Google Chrome with the configured debug profile (macOS ``open -na``).
    """
    if not _truthy(get_config_value("browser", "auto_start_cdp", "true"), default=True):
        return is_cdp_port_open()

    port = cdp_port()
    if is_cdp_port_open(port):
        return True

    profile = cdp_user_data_dir()
    logging.info(
        "🚀 Starting Chrome for CDP (port %s, profile %s)…",
        port,
        profile,
    )

    if sys.platform == "darwin":
        subprocess.Popen(
            [
                "open",
                "-na",
                "Google Chrome",
                "--args",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        chrome_bins = (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        )
        launched = False
        for binary in chrome_bins:
            try:
                subprocess.Popen(
                    [
                        binary,
                        f"--remote-debugging-port={port}",
                        f"--user-data-dir={profile}",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                launched = True
                break
            except FileNotFoundError:
                continue
        if not launched:
            logging.warning(
                "Could not find Chrome/Chromium binary to auto-start CDP on this OS."
            )
            return False

    for _ in range(40):
        if is_cdp_port_open(port):
            logging.info("✅ Chrome CDP ready on port %s", port)
            return True
        time.sleep(0.5)

    logging.warning("Chrome CDP port %s not ready after ~20s", port)
    return False


def quit_cdp_chrome() -> None:
    """Quit Chrome using our CDP ``user-data-dir`` so the window does not stay open."""
    if not _truthy(
        get_config_value("browser", "close_cdp_chrome_on_run_end", "true"),
        default=True,
    ):
        return

    profile = cdp_user_data_dir()
    needle = f"user-data-dir={profile}"

    if sys.platform == "darwin":
        result = subprocess.run(
            ["pkill", "-f", needle],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logging.info("🧹 Quit CDP Chrome (profile %s).", profile)
            time.sleep(0.5)
            return
    else:
        result = subprocess.run(
            ["pkill", "-f", needle],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logging.info("🧹 Quit CDP Chrome (profile %s).", profile)
            time.sleep(0.5)
            return

    if is_cdp_port_open():
        logging.warning(
            "CDP Chrome may still be running on port %s (profile %s).",
            cdp_port(),
            profile,
        )
    else:
        logging.info("CDP Chrome already stopped (profile %s).", profile)


def _profile_dirs_to_clear() -> List[str]:
    """Collect configured Chrome user-data-dir paths and common Playwright variants."""
    seen: set[str] = set()
    ordered: List[str] = []

    def _add(path: str | None) -> None:
        if not path:
            return
        p = str(path).strip()
        if not p or p in seen:
            return
        seen.add(p)
        ordered.append(p)
        for match in glob.glob(f"{p}*"):
            if match not in seen:
                seen.add(match)
                ordered.append(match)

    _add(get_config_value("browser", "user_data_dir", "/tmp/vfs-profile") or "/tmp/vfs-profile")
    _add(get_config_value("browser", "cdp_user_data_dir", "/tmp/vfs-debug-profile"))

    return ordered


def clear_vfs_browser_fingerprints() -> Tuple[List[str], List[Tuple[str, str]]]:
    """
    Delete Chrome profile directories (cookies, cache, local storage on disk).

    Call only when no Chrome is using those dirs (``run()`` should have closed Playwright Chrome).
    If CDP Chrome is still open, deletion may fail — log a warning and continue.

    Returns:
        (cleared_paths, failed_paths_with_error_message)
    """
    if not _truthy(
        get_config_value("browser", "clear_profile_after_long_cooldown", "true"),
        default=True,
    ):
        logging.info(
            "Skipping browser profile wipe ([browser] clear_profile_after_long_cooldown=false)."
        )
        return [], []

    cleared: List[str] = []
    failed: List[Tuple[str, str]] = []

    for path in _profile_dirs_to_clear():
        p = Path(path)
        if not p.exists():
            continue
        if not p.is_dir():
            continue
        try:
            shutil.rmtree(p)
            cleared.append(path)
        except OSError as exc:
            failed.append((path, str(exc)))

    if cleared:
        logging.info(
            "🧹 Cleared browser profile fingerprint data: %s",
            ", ".join(cleared),
        )
    if failed:
        logging.warning(
            "Could not clear browser profile(s) — quit Chrome using those dirs and retry: %s",
            "; ".join(f"{p} ({err})" for p, err in failed),
        )

    return cleared, failed
