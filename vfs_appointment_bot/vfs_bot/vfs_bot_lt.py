import logging
import re
import time
from typing import Dict, List, Optional

from playwright.sync_api import Page

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

        # If Turnstile is present, give it a moment
        try:
            if page.locator("iframe[src*='turnstile']").first.is_visible():
                page.wait_for_timeout(3000)
        except Exception:
            pass

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

        # Click the Sign In button in that same context
        btn = ctx.get_by_role("button", name=re.compile(r"^\s*Sign\s*In\s*$", re.I))
        (btn if btn.count() else ctx.locator("button[type='submit']")).first.click()

        # Dashboard guard
        page.get_by_role("button", name=re.compile("Start New Booking", re.I)).wait_for(timeout=30000)

    # ---------- Appointment check ----------
    def check_for_appointment(
        self, page: Page, appointment_params: Dict[str, str]
    ) -> Optional[List[str]]:
        """
        Click Start New Booking, select centre/category/subcategory and parse results.
        """
        page.get_by_role("button", name="Start New Booking").click()

        # Select Visa Centre
        visa_centre_dropdown = page.wait_for_selector("mat-form-field")
        visa_centre_dropdown.click()
        page.wait_for_selector(f'mat-option:has-text("{appointment_params.get("visa_center")}")')
        page.locator(f'mat-option:has-text("{appointment_params.get("visa_center")}")').click()

        # Select Visa Category
        visa_category_dropdown = page.query_selector_all("mat-form-field")[1]
        visa_category_dropdown.click()
        page.wait_for_selector(f'mat-option:has-text("{appointment_params.get("visa_category")}")')
        page.locator(f'mat-option:has-text("{appointment_params.get("visa_category")}")').click()

        # Select Subcategory
        visa_subcategory_dropdown = page.query_selector_all("mat-form-field")[2]
        visa_subcategory_dropdown.click()
        page.wait_for_selector(f'mat-option:has-text("{appointment_params.get("visa_sub_category")}")', timeout=60000)
        page.locator(f'mat-option:has-text("{appointment_params.get("visa_sub_category")}")').click()

        # Parse results
        try:
            page.wait_for_selector("div.alert", timeout=15000)
        except Exception:
            return None

        appointment_dates: List[str] = []
        for el in page.query_selector_all("div.alert"):
            txt = (el.text_content() or "").strip()
            dt = extract_date_from_string(txt)
            if dt:
                appointment_dates.append(dt)

        return appointment_dates or None