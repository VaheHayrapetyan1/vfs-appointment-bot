import logging
import re
import time
from typing import Dict, List

from playwright.sync_api import Page

from vfs_appointment_bot.utils.config_reader import get_config_value
from vfs_appointment_bot.utils.date_utils import extract_date_from_string
from vfs_appointment_bot.vfs_bot.vfs_bot import VfsBot


class VfsBotLt(VfsBot):
    """Armenia → Lithuania implementation."""

    def __init__(self, source_country_code: str):
        super().__init__()
        self.source_country_code = source_country_code
        self.destination_country_code = "LT"
        self.appointment_param_keys = [
            "visa_center",
            "visa_category",
            "visa_sub_category",
        ]

    def _log_turnstile_diagnostics(self, page: Page, stage: str) -> None:
        """
        Log Cloudflare Turnstile / captcha hints so flaky runs can be compared in app.log.

        Does not interact with the widget; Cloudflare renders most UI inside cross-origin iframes,
        so we report main-DOM cues and iframe URLs Playwright exposes.
        """
        logging.info("[captcha diagnostics] --- stage=%s ---", stage)
        logging.info("[captcha diagnostics] page.url=%s", page.url)

        loc = page.locator("iframe[src*='turnstile'], iframe[src*='challenges.cloudflare.com']")
        try:
            ic = loc.count()
            logging.info("[captcha diagnostics] main DOM: CF/Turnstile-like iframe count=%s", ic)
            for i in range(min(ic, 6)):
                try:
                    fe = loc.nth(i)
                    src = (fe.get_attribute("src") or "")[:140]
                    vis = fe.is_visible()
                    logging.info(
                        "[captcha diagnostics] main DOM iframe[%s] visible=%s src_prefix=%s",
                        i,
                        vis,
                        src,
                    )
                except Exception as ex:
                    logging.info("[captcha diagnostics] main DOM iframe[%s] read failed: %s", i, ex)
        except Exception as e:
            logging.info("[captcha diagnostics] iframe list failed: %s", e)

        cf_frames: List[str] = []
        for fr in page.frames:
            fu = (fr.url or "").strip()
            if "turnstile" in fu.lower() or "challenges.cloudflare.com" in fu.lower():
                cf_frames.append(fu[:180])
        logging.info("[captcha diagnostics] playwright frame URLs matching CF/Turnstile: %s", len(cf_frames))
        for i, fu in enumerate(cf_frames[:10]):
            logging.info("[captcha diagnostics] cf_frame[%s]=%s", i, fu)

        for field in ("cf-turnstile-response", "g-recaptcha-response"):
            try:
                fld = page.locator(
                    f"textarea[name='{field}'], input[name='{field}'][type='hidden']"
                )
                if fld.count() == 0:
                    fld = page.locator(f"[name='{field}']")
                fc = fld.count()
                if fc == 0:
                    logging.info(
                        "[captcha diagnostics] hidden token field '%s' not found in main DOM",
                        field,
                    )
                    continue
                try:
                    val = fld.first.input_value(timeout=1500)
                    logging.info(
                        "[captcha diagnostics] field '%s' present; value length=%s (0 means not solved yet)",
                        field,
                        len(val or ""),
                    )
                except Exception:
                    logging.info(
                        "[captcha diagnostics] field '%s' present (count=%s) but value not readable",
                        field,
                        fc,
                    )
            except Exception as e:
                logging.info("[captcha diagnostics] token field '%s' check failed: %s", field, e)

        try:
            tip = page.get_by_text(re.compile(r"verify you are human", re.I))
            if tip.count() > 0:
                try:
                    vis = tip.first.is_visible()
                except Exception:
                    vis = "unknown"
                logging.info(
                    "[captcha diagnostics] main DOM 'Verify you are human' occurrences=%s first_visible=%s",
                    tip.count(),
                    vis,
                )
            else:
                logging.info(
                    "[captcha diagnostics] main DOM: no 'Verify you are human' text (often lives inside iframe only)"
                )
        except Exception as e:
            logging.info("[captcha diagnostics] human-check copy probe skipped: %s", e)

    # ---------- Pre-login ----------
    def pre_login_steps(self, page: Page) -> None:
        # Let initial HTML & network settle
        page.wait_for_load_state("domcontentloaded")
        try:
            page.wait_for_selector("#loader", state="hidden", timeout=60000)
        except Exception:
            pass

        # Cookie banner: accept/reject if shown (ignore if not)
        for sel in (
            "button#onetrust-accept-btn-handler",
            "button[aria-label='Accept all']",
            "button:has-text('Accept all')",
            "button:has-text('Reject All')",
        ):
            try:
                page.locator(sel).first.click(timeout=1200)
                break
            except Exception:
                pass

        # Cloudflare Turnstile: diagnostics + short wait when widget iframe is present.
        try:
            ifr_loc = page.locator(
                "iframe[src*='turnstile'], iframe[src*='challenges.cloudflare.com']"
            )
            n_ts = ifr_loc.count()
            logging.info(
                "[captcha diagnostics] pre_login Turnstile/CF iframe count on main DOM: %s", n_ts
            )
            self._log_turnstile_diagnostics(page, "pre_login_before_wait")

            first_visible = False
            if n_ts > 0:
                try:
                    first_visible = ifr_loc.first.is_visible(timeout=2000)
                except Exception:
                    first_visible = False
                logging.info(
                    "[captcha diagnostics] pre_login first such iframe visible=%s; waiting 3s for widget…",
                    first_visible,
                )
                page.wait_for_timeout(3000)

            self._log_turnstile_diagnostics(page, "pre_login_after_turnstile_wait_block")

        except Exception as e:
            logging.info("[captcha diagnostics] Turnstile wait section error (non-fatal): %s", e)

        # Poll up to 60s for login inputs (main doc or any child frame)
        deadline = time.time() + 60
        selector_email = "input#email, input[type='email'], input[formcontrolname='username']"
        selector_pass = "input#password[type='password'], input[formcontrolname='password'][type='password']"

        while time.time() < deadline:
            login_ctx = None

            if page.locator(selector_email).count() and page.locator(selector_pass).count():
                login_ctx = page
            else:
                for f in page.frames:
                    try:
                        if f.locator(selector_email).count() and f.locator(selector_pass).count():
                            login_ctx = f
                            break
                    except Exception:
                        pass

            if login_ctx:
                # Ensure both are visible before we move on
                login_ctx.locator(selector_email + ":visible").first.wait_for(timeout=15000)
                login_ctx.locator(selector_pass + ":visible").first.wait_for(timeout=15000)
                self._login_ctx = login_ctx
                self._log_turnstile_diagnostics(page, "pre_login_ready_email_password_visible")
                return

            page.wait_for_timeout(1000)

        raise TimeoutError("Login inputs did not appear within 60s")

    # ---------- Login ----------
    def login(self, page: Page, email_id: str, password: str) -> None:
        ctx = getattr(self, "_login_ctx", page)

        email_box = ctx.locator(
            "input#email:visible, input[type='email']:visible, input[formcontrolname='username']:visible"
        ).first
        pass_box = ctx.locator(
            "input#password[type='password']:visible, input[formcontrolname='password'][type='password']:visible"
        ).first

        try:
            email_box.fill("")  # clear possible autofill
        except Exception:
            pass
        email_box.fill(email_id, timeout=10000)

        try:
            pass_box.fill("")
        except Exception:
            pass
        pass_box.fill(password, timeout=10000)

        self._log_turnstile_diagnostics(page, "before_sign_in_click")

        # Click the Sign In button in that same context
        btn = ctx.get_by_role("button", name=re.compile(r"^\s*Sign\s*In\s*$", re.I))
        (btn if btn.count() else ctx.locator("button[type='submit']")).first.click()

        # Dashboard guard
        page.get_by_role("button", name=re.compile("Start New Booking", re.I)).wait_for(timeout=30000)

    def _raise_if_vfs_site_error(self, page: Page) -> None:
        """
        VFS sometimes returns 502 and redirects to page-not-found. That is not 'no slots';
        raise so run() ends and main() does a full retry after the configured interval.
        """
        url = (page.url or "").lower()
        if "page-not-found" in url:
            raise RuntimeError(
                "VFS error: URL is page-not-found (often 502 / connectivity). Full retry."
            )

        try:
            heading = page.get_by_role(
                "heading", name=re.compile(r"temporary connectivity|connectivity issue|\(502\)", re.I)
            )
            if heading.count() > 0 and heading.first.is_visible():
                raise RuntimeError(
                    "VFS error: connectivity / 502 page shown. Full retry."
                )
        except RuntimeError:
            raise
        except Exception:
            pass

        try:
            snippet = page.get_by_text(re.compile(r"temporary connectivity issue", re.I))
            if snippet.count() > 0 and snippet.first.is_visible():
                raise RuntimeError(
                    "VFS error: connectivity message on page. Full retry."
                )
        except RuntimeError:
            raise
        except Exception:
            pass

    def _click_mat_option(self, page: Page, option_text: str) -> None:
        """
        Pick an Angular Material listbox option. CDK overlay backdrops often intercept
        normal clicks; use forced click and JS dispatch as fallbacks.
        """
        opt = page.locator("mat-option").filter(has_text=option_text).first
        opt.wait_for(state="visible", timeout=60_000)
        try:
            opt.scroll_into_view_if_needed()
        except Exception:
            pass
        page.wait_for_timeout(300)
        try:
            opt.click(timeout=10_000)
        except Exception:
            try:
                opt.click(force=True, timeout=15_000)
            except Exception:
                handle = opt.element_handle()
                if handle is None:
                    raise
                handle.evaluate("el => el.click()")

    def _select_visa_subcategory(self, page: Page, option_text: str) -> None:
        """Open the third mat-form-field (sub-category) and pick an mat-option by label."""
        self._raise_if_vfs_site_error(page)
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
        sub = page.locator("mat-form-field").nth(2)
        sub.wait_for(state="visible", timeout=30_000)
        sub.scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        sub.click()
        page.wait_for_timeout(450)
        self._click_mat_option(page, option_text)

    def _settle_after_subcategory_change(self, page: Page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        page.wait_for_timeout(800)

    def _read_tourism_appointment_dates(self, page: Page) -> List[str]:
        """Return any YYYY-MM-DD (etc.) parsed from div.alert; empty if none or no alerts."""
        self._raise_if_vfs_site_error(page)
        try:
            page.wait_for_selector("div.alert", timeout=15_000)
        except Exception:
            self._raise_if_vfs_site_error(page)
            return []

        dates: List[str] = []
        for el in page.query_selector_all("div.alert"):
            txt = (el.text_content() or "").strip()
            dt = extract_date_from_string(txt)
            if dt:
                dates.append(dt)
        return dates

    # ---------- Appointment check ----------
    def check_for_appointment(
        self, page: Page, appointment_params: Dict[str, str]
    ) -> List[str]:
        """
        Start New Booking, pick centre/category, then loop on sub-category:
        check Tourism for bookable dates; if none, wait 30s, select Family/Friends visit,
        select Tourism again (same browser session). Repeat until dates appear or an error
        is raised (outer run() closes the browser and main() applies the configured wait).
        """
        page.get_by_role("button", name="Start New Booking").click()

        self._raise_if_vfs_site_error(page)

        # Select Visa Centre
        centre = page.locator("mat-form-field").nth(0)
        centre.scroll_into_view_if_needed()
        centre.click()
        page.wait_for_timeout(400)
        self._click_mat_option(page, appointment_params["visa_center"])

        # Select Visa Category
        cat = page.locator("mat-form-field").nth(1)
        cat.scroll_into_view_if_needed()
        cat.click()
        page.wait_for_timeout(400)
        self._click_mat_option(page, appointment_params["visa_category"])

        tourism_label = (appointment_params.get("visa_sub_category") or "Tourism").strip()
        reset_label = "Family/Friends visit"

        while True:
            self._raise_if_vfs_site_error(page)
            self._select_visa_subcategory(page, tourism_label)
            self._settle_after_subcategory_change(page)

            dates = self._read_tourism_appointment_dates(page)
            if dates:
                return dates

            self._raise_if_vfs_site_error(page)

            _test_raw = get_config_value(
                "notification", "test_notify_when_empty", ""
            )
            if str(_test_raw or "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            ):
                logging.info(
                    "[TEST] test_notify_when_empty: stopping after first empty Tourism "
                    "check so run() can send test notifications (remove flag for normal polling)."
                )
                return []

            logging.info(
                "No Tourism slots with parsed dates; waiting 30s, selecting %r then Tourism again.",
                reset_label,
            )
            page.wait_for_timeout(30_000)

            self._select_visa_subcategory(page, reset_label)
            self._settle_after_subcategory_change(page)
            self._select_visa_subcategory(page, tourism_label)
            self._settle_after_subcategory_change(page)
