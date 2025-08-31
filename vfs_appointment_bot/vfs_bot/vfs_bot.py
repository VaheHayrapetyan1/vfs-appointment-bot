import argparse
import logging
from abc import ABC, abstractmethod
from typing import Dict, List
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
            connected_via_cdp = False

            # Try to attach to a real Chrome started with --remote-debugging-port=9222
            try:
                logging.info("🔌 Trying to attach to Chrome over CDP (localhost:9222)…")
                browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.new_page()
                connected_via_cdp = True
                logging.info("✅ Attached via CDP.")
            except Exception:
                logging.info("ℹ️ CDP attach failed — launching persistent Chrome profile instead…")
                # Fall back to launching user Chrome with a persistent profile
                context = p.chromium.launch_persistent_context(
                    user_data_dir="/tmp/vfs-profile",
                    channel="chrome",                 # use system Chrome
                    headless=False,                   # IMPORTANT for CF/Turnstile
                    args=[
                        "--start-maximized",
                        "--disable-blink-features=AutomationControlled",
                    ],
                    ignore_default_args=["--enable-automation"],
                )
                page = context.pages[0] if context.pages else context.new_page()
                logging.info("✅ Launched persistent Chrome context.")

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
        channels = get_config_value("notification", "channels")
        if not channels:
            logging.warning("No notification channels configured. Skipping notification.")
            return
        for channel in channels.split(","):
            client = get_notification_client(channel)
            try:
                client.send_notification(message)
            except Exception:
                logging.error(f"Failed to send {channel} notification")

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