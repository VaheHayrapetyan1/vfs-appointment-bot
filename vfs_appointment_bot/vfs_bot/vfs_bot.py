import argparse
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple
from pathlib import Path

import playwright
from playwright.sync_api import sync_playwright

from vfs_appointment_bot.utils.appointment_availability import AppointmentScanResult
from vfs_appointment_bot.utils.browser_profile import (
    cdp_port,
    ensure_cdp_chrome_ready,
    quit_cdp_chrome,
)
from vfs_appointment_bot.utils.config_reader import get_config_value
from vfs_appointment_bot.notification.notification_client_factory import (
    get_notification_client,
)


class LoginError(Exception):
    """Exception raised when login fails."""


class VfsConnectivityError(RuntimeError):
    """Transient VFS outage (page-not-found, 502, etc.) — outer loop retries sooner."""


class VfsRateLimitError(RuntimeError):
    """VFS anti-automation / permission (e.g. 429201) — IP cooldown; retry after a long wait."""


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

    @staticmethod
    def _get_vfs_login_accounts() -> List[Tuple[str, str]]:
        """
        Ordered accounts: primary first, then optional fallbacks in section order.
        Primary = [vfs-account-1] if present, else [vfs-credential].
        Then [vfs-account-2] … [vfs-account-10] when set (skips duplicate emails).
        """
        pairs: List[Tuple[str, str]] = []

        def _add(email: Optional[str], pw: Optional[str]) -> None:
            if not email or not pw:
                return
            e, p = str(email).strip(), str(pw).strip()
            if not e or not p:
                return
            if any(existing[0].lower() == e.lower() for existing in pairs):
                return
            pairs.append((e, p))

        a1e = get_config_value("vfs-account-1", "email")
        a1p = get_config_value("vfs-account-1", "password")
        cre = get_config_value("vfs-credential", "email")
        crp = get_config_value("vfs-credential", "password")
        if a1e and a1p:
            _add(a1e, a1p)
        elif cre and crp:
            _add(cre, crp)

        for n in range(2, 11):
            section = f"vfs-account-{n}"
            _add(
                get_config_value(section, "email"),
                get_config_value(section, "password"),
            )

        if not pairs:
            raise RuntimeError(
                "No VFS credentials: set [vfs-account-1] or [vfs-credential], "
                "optional [vfs-account-2] … [vfs-account-10] for fallbacks after primary fails."
            )
        return pairs

    @staticmethod
    def _vfs_access_restricted(page) -> bool:
        """True when VFS shows access / user restriction (e.g. 429001) on the page."""
        try:
            snippet = page.get_by_text(
                re.compile(
                    r"access restricted|429001|unusual activity|user id\s*\(429",
                    re.I,
                )
            )
            if snippet.count() > 0:
                try:
                    if snippet.first.is_visible():
                        return True
                except Exception:
                    return True
        except Exception:
            pass
        try:
            html = (page.content() or "").lower()
            if "429001" in html and "restricted" in html:
                return True
            if "access restricted for user id" in html:
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _close_playwright_browser(
        connected_via_cdp: bool,
        browser,
        context,
    ) -> None:
        """
        Close Playwright resources when a run ends (success or failure).

        - CDP: disconnect Playwright, then quit the Chrome process for our debug profile.
        - Persistent context: ``context.close()`` shuts down Playwright-launched Chrome.
        """
        try:
            if connected_via_cdp and browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
                quit_cdp_chrome()
            elif context is not None:
                context.close()
                logging.info("🧹 Closed Playwright Chrome (persistent context).")
        except Exception as ex:
            logging.warning("Browser/context cleanup: %s", ex)

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

        try:
            vfs_accounts = self._get_vfs_login_accounts()
        except RuntimeError as e:
            logging.error("%s", e)
            return False

        appointment_params = self.get_appointment_params(args)

        # Launch / attach browser and perform actions
        with sync_playwright() as p:
            page = None
            context = None
            browser = None
            connected_via_cdp = False

            cdp_url = f"http://127.0.0.1:{cdp_port()}"
            if ensure_cdp_chrome_ready():
                try:
                    logging.info("🔌 Trying to attach to Chrome over CDP (%s)…", cdp_url)
                    browser = p.chromium.connect_over_cdp(cdp_url)
                    context = (
                        browser.contexts[0] if browser.contexts else browser.new_context()
                    )
                    page = (
                        context.pages[0]
                        if context.pages
                        else context.new_page()
                    )
                    connected_via_cdp = True
                    logging.info("✅ Attached via CDP.")
                except Exception as exc:
                    logging.info(
                        "ℹ️ CDP attach failed (%s) — falling back to persistent Chrome…",
                        exc,
                    )

            if not connected_via_cdp:
                logging.info("Launching persistent Chrome profile…")
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

            try:
                logging.info("➡ Navigating to login page…")
                page.goto(vfs_url, wait_until="domcontentloaded", timeout=90000)

                # Wait for the Angular app to finish loading after Cloudflare check
                try:
                    page.wait_for_selector("#loader", state="hidden", timeout=60000)
                except Exception:
                    pass

                self._dump_debug(page, "after_goto")

                appointment_found = False
                connectivity_error: Optional[VfsConnectivityError] = None

                for acc_index, (email_id, password) in enumerate(vfs_accounts):
                    if acc_index > 0:
                        logging.info(
                            "➡ Reloading login page for fallback account %s/%s…",
                            acc_index + 1,
                            len(vfs_accounts),
                        )
                        try:
                            context.clear_cookies()
                        except Exception:
                            pass
                        page.goto(vfs_url, wait_until="domcontentloaded", timeout=90000)
                        try:
                            page.wait_for_selector("#loader", state="hidden", timeout=60000)
                        except Exception:
                            pass
                        self._dump_debug(page, f"after_goto_account_{acc_index + 1}")

                    logging.info("➡ Running pre-login steps…")
                    try:
                        self.pre_login_steps(page)
                        logging.info("✅ pre_login_steps() completed.")
                    except VfsRateLimitError as e:
                        logging.warning(
                            "VFS rate limit during pre-login (%s/%s, %s): %s",
                            acc_index + 1,
                            len(vfs_accounts),
                            email_id,
                            e,
                        )
                        self._dump_debug(page, "prelogin_rate_limit")
                        if acc_index < len(vfs_accounts) - 1:
                            continue
                        raise
                    except Exception as e:
                        logging.info(f"Pre-login step error: {e}")
                        self._dump_debug(page, "prelogin_fail")
                        raise

                    logging.info(
                        "➡ Attempting VFS login (%s/%s) as %s…",
                        acc_index + 1,
                        len(vfs_accounts),
                        email_id,
                    )
                    try:
                        self.login(page, email_id, password)
                    except VfsRateLimitError as e:
                        logging.warning(
                            "VFS rate limit during login (%s/%s, %s): %s",
                            acc_index + 1,
                            len(vfs_accounts),
                            email_id,
                            e,
                        )
                        self._dump_debug(page, "login_rate_limit")
                        if acc_index < len(vfs_accounts) - 1:
                            continue
                        raise
                    except Exception as e:
                        logging.warning("VFS login failed for %s: %s", email_id, e)
                        self._dump_debug(page, "login_fail")
                        if acc_index < len(vfs_accounts) - 1:
                            continue
                        raise LoginError(
                            "\033[1;31mLogin failed for all configured accounts. "
                            "Verify passwords and VFS access, then try again.\033[0m"
                        )

                    if self._vfs_access_restricted(page):
                        logging.warning(
                            "VFS access restriction detected for %s; trying next account if any.",
                            email_id,
                        )
                        self._dump_debug(page, "access_restricted")
                        if acc_index < len(vfs_accounts) - 1:
                            continue
                        raise LoginError(
                            "\033[1;31mAccess restricted for all configured accounts "
                            "(e.g. 429001). Use Contact Us or wait, then retry.\033[0m"
                        )

                    logging.info("✅ Logged in successfully as %s", email_id)

                    logging.info(f"Checking appointments for {appointment_params}")
                    try:
                        scan = self.check_for_appointment(page, appointment_params)
                        if scan.has_dates:
                            logging.info(
                                "\033[1;32mFound appointments on: %s\033[0m",
                                ", ".join(scan.dates_iso),
                            )
                            self.notify_appointment(appointment_params, scan)
                            appointment_found = True
                        else:
                            logging.info(
                                "\033[1;33mNo appointments found for the specified criteria.\033[0m"
                            )
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
                    except VfsRateLimitError as e:
                        logging.warning(
                            "VFS rate limit during appointment check (%s/%s, %s): %s",
                            acc_index + 1,
                            len(vfs_accounts),
                            email_id,
                            e,
                        )
                        self._dump_debug(page, "appointment_rate_limit")
                        if acc_index < len(vfs_accounts) - 1:
                            continue
                        raise
                    except VfsConnectivityError as e:
                        logging.error("Appointment check failed: %s", e)
                        connectivity_error = e
                        break
                    except Exception as e:
                        logging.error(f"Appointment check failed: {e}")
                        appointment_found = False
                        break

                    break

                if connectivity_error:
                    raise connectivity_error

                return appointment_found
            finally:
                self._close_playwright_browser(connected_via_cdp, browser, context)


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

    def notify_appointment(
        self, appointment_params: Dict[str, str], scan: AppointmentScanResult
    ) -> None:
        """Send notifications using configured channels."""
        criteria = ", ".join(appointment_params.values())
        dates_line = ", ".join(scan.dates_iso)
        parts = [f"Found appointment(s) for {criteria} on {dates_line}"]
        if scan.alert_excerpts:
            bullets = "\n".join(f"• {ex}" for ex in scan.alert_excerpts)
            parts.append(f"VFS alert(s):\n{bullets}")
        self._notify_message("\n\n".join(parts))

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
    ) -> AppointmentScanResult:
        raise NotImplementedError