import argparse
import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from pathlib import Path

import playwright
from playwright.sync_api import sync_playwright

from vfs_appointment_bot.utils.config_reader import get_config_value
from vfs_appointment_bot.notification.notification_client_factory import (
    get_notification_client,
)


class LoginError(Exception):
    """Exception raised when login fails."""


class VfsBot(ABC):
    """
    Abstract base class for VfsBot
    """

    def __init__(self):
        self.source_country_code = None
        self.destination_country_code = None
        self.appointment_param_keys: List[str] = []

    @staticmethod
    def _dump_debug(page, tag: str):
        """Save URL, screenshot, and HTML to ./debug/ for quick diagnostics."""
        Path("debug").mkdir(exist_ok=True)
        png = f"debug/{tag}.png"
        html = f"debug/{tag}.html"
        try:
            page.screenshot(path=png, full_page=True)
        except Exception:
            pass
        try:
            with open(html, "w", encoding="utf-8") as f:
                f.write(page.content())
        except Exception:
            pass
        logging.info(f"📸 Saved screenshot to {png}")
        logging.info(f"📄 Saved HTML to {html}")
        logging.info(f"🌐 Current URL: {page.url}")

    def run(self, args: argparse.Namespace = None) -> bool:
        """
        Starts the VFS bot for appointment checking and notification.
        """
        logging.info(
            f"Starting VFS Bot for {self.source_country_code.upper()}-{self.destination_country_code.upper()}"
        )

        # Configuration values
        try:
            url_key = self.source_country_code + "-" + self.destination_country_code
            vfs_url = get_config_value("vfs-url", url_key)
        except KeyError as e:
            logging.error(f"Missing configuration value: {e}")
            return False

        email_id = get_config_value("vfs-credential", "email")
        password = get_config_value("vfs-credential", "password")

        appointment_params = self.get_appointment_params(args)

        # Launch / attach browser and perform actions
        with sync_playwright() as p:
            page = None
            context = None
            browser = None
            connected_via_cdp = False

            # Try to attach to a real Chrome started with --remote-debugging-port=9222
            try:
                logging.info("🔌 Trying to attach to Chrome over CDP (localhost:9222)…")
                browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = (
                    context.pages[0]
                    if context.pages
                    else context.new_page()
                )
                connected_via_cdp = True
                logging.info("✅ Attached via CDP.")
            except Exception:
                logging.info("ℹ️ CDP attach failed — launching persistent Chrome profile instead…")
                base_profile = get_config_value(
                    "browser", "user_data_dir", "/tmp/vfs-profile"
                )
                profile_candidates = [
                    base_profile,
                    f"{base_profile}-{os.getpid()}",
                    f"{base_profile}-{os.getpid()}-{int(time.time())}",
                ]
                context = None
                last_launch_error: Optional[Exception] = None
                for user_data_dir in profile_candidates:
                    try:
                        logging.info("Trying Chrome user-data-dir: %s", user_data_dir)
                        # Large window; use no_viewport so layout matches the real window
                        # (fixed viewport + window-size often mis-centers pages on macOS/Retina).
                        _bw, _bh = 2560, 1440
                        context = p.chromium.launch_persistent_context(
                            user_data_dir=user_data_dir,
                            channel="chrome",
                            headless=False,
                            no_viewport=True,
                            args=[
                                f"--window-size={_bw},{_bh}",
                                "--window-position=0,0",
                                "--disable-blink-features=AutomationControlled",
                            ],
                            ignore_default_args=["--enable-automation"],
                        )
                        page = (
                            context.pages[0]
                            if context.pages
                            else context.new_page()
                        )
                        logging.info(
                            "✅ Launched persistent Chrome (profile %s). "
                            "Tip: close regular Chrome or use another dir if you see "
                            "\"Opening in existing browser session\".",
                            user_data_dir,
                        )
                        break
                    except Exception as e:
                        last_launch_error = e
                        logging.warning(
                            "Persistent launch failed for %s: %s", user_data_dir, e
                        )
                        continue

                if context is None:
                    logging.error(
                        "Chrome profile is probably locked by another running Chrome "
                        "(same user-data-dir). Close Chrome windows using that profile, "
                        "or start debugging Chrome on port 9222 so CDP attach works:\n"
                        '  open -na "Google Chrome" --args '
                        "--remote-debugging-port=9222 --user-data-dir=\"/tmp/vfs-debug-profile\""
                    )
                    raise last_launch_error

            logging.info("➡ Navigating to login page…")
            page.goto(vfs_url, wait_until="domcontentloaded", timeout=90000)

            # Wait for the Angular app to finish loading after Cloudflare check
            try:
                page.wait_for_selector("#loader", state="hidden", timeout=60000)
            except Exception:
                pass

            self._dump_debug(page, "after_goto")

            logging.info("➡ Running pre-login steps…")
            try:
                self.pre_login_steps(page)
                logging.info("✅ pre_login_steps() completed.")
            except Exception as e:
                logging.info(f"Pre-login step error: {e}")
                self._dump_debug(page, "prelogin_fail")
                # Close cleanly
                try:
                    if connected_via_cdp:
                        browser.close()
                    else:
                        context.close()
                except Exception:
                    pass
                raise

            try:
                self.login(page, email_id, password)
                logging.info("✅ Logged in successfully")
            except Exception:
                self._dump_debug(page, "login_fail")
                # Close cleanly
                try:
                    if connected_via_cdp:
                        browser.close()
                    else:
                        context.close()
                except Exception:
                    pass
                raise LoginError(
                    "\033[1;31mLogin failed. "
                    "Please verify your username and password by logging in to the browser and try again.\033[0m"
                )

            logging.info(f"Checking appointments for {appointment_params}")
            appointment_found = False
            try:
                dates = self.check_for_appointment(page, appointment_params)
                if dates:
                    logging.info(
                        f"\033[1;32mFound appointments on: {', '.join(dates)} \033[0m"
                    )
                    self.notify_appointment(appointment_params, dates)
                    appointment_found = True
                else:
                    logging.info(
                        "\033[1;33mNo appointments found for the specified criteria.\033[0m"
                    )
                    # TEMP: remove after verifying email + Telegram — set
                    # [notification] test_notify_when_empty = true in config.ini
                    _test_flag = get_config_value(
                        "notification", "test_notify_when_empty", ""
                    )
                    if str(_test_flag or "").strip().lower() in (
                        "1",
                        "true",
                        "yes",
                        "on",
                    ):
                        crit = ", ".join(appointment_params.values())
                        self._notify_message(
                            "[TEST] No slots this run — notification pipeline check only. "
                            f"Criteria: {crit}. "
                            "Turn off test_notify_when_empty in [notification] when done."
                        )
            except Exception as e:
                logging.error(f"Appointment check failed: {e}")

            # Close connection/context (don’t kill your real Chrome when attached)
            try:
                if connected_via_cdp:
                    browser.close()      # closes the CDP connection only
                else:
                    context.close()
            except Exception:
                pass

            return appointment_found

    def get_appointment_params(self, args: argparse.Namespace) -> Dict[str, str]:
        """Read appointment params from CLI or prompt once."""
        appointment_params = {}
        for key in self.appointment_param_keys:
            if (
                getattr(args, "appointment_params") is not None
                and args.appointment_params.get(key) is not None
            ):
                appointment_params[key] = args.appointment_params[key]
            else:
                key_name = key.replace("_", " ")
                appointment_params[key] = input(f"Enter the {key_name}: ")
        return appointment_params

    def notify_appointment(self, appointment_params: Dict[str, str], dates: List[str]):
        """Send notifications using configured channels."""
        message = f"Found appointment(s) for {', '.join(appointment_params.values())} on {', '.join(dates)}"
        self._notify_message(message)

    def _notify_message(self, message: str) -> None:
        """Deliver `message` to every channel in [notification] channels."""
        channels = get_config_value("notification", "channels")
        if not channels:
            logging.warning("No notification channels configured. Skipping notification.")
            return
        for raw in channels.split(","):
            channel = raw.strip().lower()
            if not channel:
                continue
            try:
                client = get_notification_client(channel)
                client.send_notification(message)
            except Exception:
                logging.exception("Failed to send %s notification", channel)

    # ----- Abstracts for country implementations -----

    @abstractmethod
    def login(
        self, page: playwright.sync_api.Page, email_id: str, password: str
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def pre_login_steps(self, page: playwright.sync_api.Page) -> None:
        raise NotImplementedError

    @abstractmethod
    def check_for_appointment(
        self, page: playwright.sync_api.Page, appointment_params: Dict[str, str]
    ) -> List[str]:
        raise NotImplementedError