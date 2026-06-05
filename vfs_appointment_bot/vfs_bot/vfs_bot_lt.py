import logging
import re
import time
from typing import Dict, List

from playwright.sync_api import Frame, Locator, Page

from vfs_appointment_bot.utils.config_reader import get_config_value
from vfs_appointment_bot.utils.appointment_availability import (
    AppointmentScanResult,
    truncate_excerpt,
)
from vfs_appointment_bot.utils.date_utils import extract_all_dates_normalized
from vfs_appointment_bot.vfs_bot.vfs_bot import VfsBot, VfsConnectivityError, VfsRateLimitError


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

        self._raise_if_vfs_site_error(page)

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
            self._raise_if_vfs_site_error(page)

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

    def _wait_turnstile_token_best_effort(self, page: Page) -> None:
        """
        Poll for cf-turnstile-response (non-empty). Uses ``[login] turnstile_wait_seconds``
        from config (default 90). Continues anyway if still empty (some runs use silent token).
        """
        try:
            max_s = int(get_config_value("login", "turnstile_wait_seconds", "90") or "90")
        except ValueError:
            max_s = 90
        max_s = max(5, min(max_s, 180))
        deadline = time.time() + max_s
        while time.time() < deadline:
            try:
                fld = page.locator(
                    "textarea[name='cf-turnstile-response'], "
                    "input[name='cf-turnstile-response'][type='hidden']"
                )
                if fld.count() > 0:
                    val = fld.first.input_value(timeout=800)
                    if val and len(val.strip()) > 10:
                        logging.info("Turnstile token ready; proceeding to Sign In.")
                        return
            except Exception:
                pass
            page.wait_for_timeout(400)
        logging.info(
            "Turnstile token still empty after %ss — attempting Sign In anyway.",
            max_s,
        )

    def _click_sign_in(self, ctx: Page | Frame, page: Page, pass_box: Locator) -> None:
        """
        VFS + Cloudflare often leave Sign In barely in view or not "actionable".
        Scroll, wheel-nudge, normal/force/JS click, then Enter on the password field.
        """
        sign_in = (
            ctx.get_by_role("button", name=re.compile(r"sign\s*in", re.I))
            .or_(ctx.locator("button[type='submit']"))
            .or_(
                ctx.locator(
                    "button.mat-flat-button, button.mat-raised-button, "
                    "button.mat-mdc-raised-button, button.mdc-button"
                ).filter(has_text=re.compile(r"sign\s*in", re.I))
            )
        )
        sign_in.first.wait_for(state="attached", timeout=60_000)

        try:
            sign_in.first.scroll_into_view_if_needed(timeout=15_000)
        except Exception:
            pass
        try:
            page.mouse.wheel(0, 420)
        except Exception:
            pass
        page.wait_for_timeout(350)

        last_err: Exception | None = None
        for force, label in ((False, "normal"), (True, "force")):
            try:
                sign_in.first.click(timeout=25_000, force=force)
                logging.info("Sign In clicked (%s).", label)
                return
            except Exception as e:
                last_err = e
                logging.warning("Sign In click failed (%s): %s", label, e)

        try:
            handle = sign_in.first.element_handle()
            if handle is not None:
                handle.evaluate("el => el.click()")
                logging.info("Sign In clicked (JS dispatch).")
                return
        except Exception as e:
            last_err = e
            logging.warning("Sign In JS click failed: %s", e)

        try:
            pass_box.press("Enter")
            logging.info("Sign In: submitted via Enter on password field.")
            return
        except Exception as e:
            last_err = e

        if last_err:
            raise last_err
        raise RuntimeError("Could not activate Sign In")

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
        try:
            pass_box.scroll_into_view_if_needed(timeout=10_000)
        except Exception:
            pass

        self._log_turnstile_diagnostics(page, "before_sign_in_click")
        self._wait_turnstile_token_best_effort(page)
        self._click_sign_in(ctx, page, pass_box)

        # Dashboard guard
        page.get_by_role("button", name=re.compile("Start New Booking", re.I)).wait_for(timeout=30000)

    def _raise_if_vfs_site_error(self, page: Page) -> None:
        """
        Transient 502 / connectivity vs permission / rate wall (429201).

        ``page-not-found`` URL is used both for real 502s and for permission cooldown pages —
        inspect body text so 429201 triggers a *long* outer wait, not a 15s connectivity retry.
        """
        url = (page.url or "").lower()
        try:
            html_l = (page.content() or "").lower()
        except Exception:
            html_l = ""

        if "429201" in html_l or re.search(
            r"permission\s+issue", html_l, re.I
        ):
            raise VfsRateLimitError(
                "VFS Permission / rate block (e.g. 429201). Often tied to request volume / IP; "
                "outer loop may try other accounts before a long cooldown."
            )

        if "page-not-found" in url:
            if (
                "cooldown" in html_l
                or "temporary pause" in html_l
                or "multiple requests" in html_l
                or "defined thresholds" in html_l
            ):
                raise VfsRateLimitError(
                    "VFS access pause / rate limit (page-not-found with cooldown message). "
                    "Same class as 429201; outer loop may try other accounts before a long cooldown."
                )
            raise VfsConnectivityError(
                "VFS error: URL is page-not-found (often 502 / connectivity). Full retry."
            )

        try:
            heading = page.get_by_role(
                "heading", name=re.compile(r"temporary connectivity|connectivity issue|\(502\)", re.I)
            )
            if heading.count() > 0 and heading.first.is_visible():
                raise VfsConnectivityError(
                    "VFS error: connectivity / 502 page shown. Full retry."
                )
        except VfsConnectivityError:
            raise
        except Exception:
            pass

        try:
            snippet = page.get_by_text(re.compile(r"temporary connectivity issue", re.I))
            if snippet.count() > 0 and snippet.first.is_visible():
                raise VfsConnectivityError(
                    "VFS error: connectivity message on page. Full retry."
                )
        except VfsConnectivityError:
            raise
        except Exception:
            pass

    def _open_mat_form_field_select(self, page: Page, field_index: int) -> None:
        """
        Open the mat-select inside ``mat-form-field[n]``. Retries when Angular re-renders
        or CDK overlays detach the field mid-click (common during LT sub-category refresh).
        """
        last_err: Exception | None = None
        for attempt in range(1, 4):
            self._raise_if_vfs_site_error(page)
            page.keyboard.press("Escape")
            page.wait_for_timeout(350)

            field = page.locator("mat-form-field").nth(field_index)
            field.wait_for(state="attached", timeout=30_000)

            trigger = field.locator(
                ".mat-mdc-select-trigger, .mat-select-trigger, mat-select"
            )
            if trigger.count() > 0:
                target = trigger.first
                try:
                    target.wait_for(state="visible", timeout=15_000)
                except Exception:
                    target = field
            else:
                target = field

            try:
                target.scroll_into_view_if_needed(timeout=10_000)
            except Exception:
                pass
            page.wait_for_timeout(300)

            for force, label in ((False, "normal"), (True, "force")):
                try:
                    target.click(timeout=12_000, force=force)
                    page.wait_for_timeout(450)
                    return
                except Exception as e:
                    last_err = e
                    logging.warning(
                        "mat-form-field[%s] open click failed (%s, attempt %s/3): %s",
                        field_index,
                        label,
                        attempt,
                        e,
                    )

            try:
                handle = target.element_handle()
                if handle is not None:
                    handle.evaluate("el => el.click()")
                    page.wait_for_timeout(450)
                    return
            except Exception as e:
                last_err = e

            page.wait_for_timeout(600)

        if last_err:
            raise last_err
        raise RuntimeError(
            f"Could not open mat-form-field[{field_index}] select after 3 attempts"
        )

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
        self._open_mat_form_field_select(page, 2)
        self._click_mat_option(page, option_text)

    def _settle_after_subcategory_change(self, page: Page) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        page.wait_for_timeout(800)

    def _scan_tourism_appointment_alerts(self, page: Page) -> AppointmentScanResult:
        """Parse all ``div.alert`` blocks: ISO dates + verbatim excerpts for notifications."""
        self._raise_if_vfs_site_error(page)
        try:
            page.wait_for_selector("div.alert", timeout=15_000)
        except Exception:
            self._raise_if_vfs_site_error(page)
            return AppointmentScanResult.empty()

        ordered_iso: List[str] = []
        seen_iso: set[str] = set()
        excerpts: List[str] = []

        for el in page.query_selector_all("div.alert"):
            raw = (el.text_content() or "").strip()
            if not raw:
                continue
            found = extract_all_dates_normalized(raw)
            if not found:
                continue
            for dt in found:
                if dt not in seen_iso:
                    seen_iso.add(dt)
                    ordered_iso.append(dt)
            excerpt = truncate_excerpt(raw)
            if excerpt and excerpt not in excerpts:
                excerpts.append(excerpt)

        return AppointmentScanResult(tuple(ordered_iso), tuple(excerpts))

    # ---------- Appointment check ----------
    def check_for_appointment(
        self, page: Page, appointment_params: Dict[str, str]
    ) -> AppointmentScanResult:
        """
        Start New Booking, pick centre/category, then loop on sub-category:
        check Tourism for bookable dates; if none, wait ``[lt_refresh] interval_seconds``, select Family/Friends visit,
        select Tourism again (same browser session). Repeat until dates appear or an error
        is raised (outer run() closes the browser and main() applies the configured wait).
        """
        page.get_by_role("button", name="Start New Booking").click()

        self._raise_if_vfs_site_error(page)

        # Select Visa Centre
        self._open_mat_form_field_select(page, 0)
        self._click_mat_option(page, appointment_params["visa_center"])

        # Select Visa Category
        self._open_mat_form_field_select(page, 1)
        self._click_mat_option(page, appointment_params["visa_category"])

        tourism_label = (appointment_params.get("visa_sub_category") or "Tourism").strip()
        reset_label = "Family/Friends visit"

        while True:
            self._raise_if_vfs_site_error(page)
            self._select_visa_subcategory(page, tourism_label)
            self._settle_after_subcategory_change(page)

            scan = self._scan_tourism_appointment_alerts(page)
            if scan.has_dates:
                return scan

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
                return AppointmentScanResult.empty()

            try:
                wait_sec = int(
                    get_config_value("lt_refresh", "interval_seconds", "10") or "10"
                )
            except ValueError:
                wait_sec = 10
            wait_sec = max(3, min(wait_sec, 300))
            logging.info(
                "No Tourism slots with parsed dates; waiting %ss, selecting %r then Tourism again.",
                wait_sec,
                reset_label,
            )
            page.wait_for_timeout(wait_sec * 1000)

            self._select_visa_subcategory(page, reset_label)
            self._settle_after_subcategory_change(page)
            self._select_visa_subcategory(page, tourism_label)
            self._settle_after_subcategory_change(page)
