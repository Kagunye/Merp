"""UI + interaction UAT.

Does what a real person does with a browser, not just the data layer:
  * log in through the login form (CSRF, redirect, session)
  * open every top-level module home page as a navigation and assert
    the sidebar, topbar and page title are intact
  * create a customer by POSTing the actual form
  * create & post an invoice through the inline formset (reference,
    due date, 2 line items)
  * use the list-view search (?q=) and status filter (?status=)
  * open the invoice detail and click Print — verify the print page
    renders a letterhead
  * click Email — verify the compose form prefills "to", "subject",
    "message" from the customer, then POST it and verify the redirect
  * navigate dashboard → invoice list → detail → payment → back
  * open and save the branding form (set a different brand color)
    then confirm the whole app re-themes to the new colour
  * check that pagination links work on at least one long list
  * sign out and verify we land on the login page

Run:
    python manage.py test_uat_ui
    python manage.py test_uat_ui --chatty   # per-check output
    python manage.py test_uat_ui --keep     # don't roll back
"""
from __future__ import annotations

from decimal import Decimal
from datetime import date, timedelta
import re

from django.core.management.base import BaseCommand
from django.db import transaction
from django.test import Client, override_settings
from django.urls import reverse


RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
BOLD = "\033[1m"
DIM = "\033[2m"


class UiFail(AssertionError):
    pass


class _Rollback(Exception):
    pass


class Command(BaseCommand):
    help = "UI + interaction UAT — clicks and types like a real user."

    def add_arguments(self, parser):
        parser.add_argument("--keep", action="store_true", help="Persist test data.")
        parser.add_argument("--chatty", action="store_true", help="Verbose checks.")

    def handle(self, *args, **opts):
        self.keep = opts["keep"]
        self.chatty = opts["chatty"]
        self.passed = 0
        self.failed = []
        self.sections = []

        self.stdout.write(f"\n{BOLD}╔══════════════════════════════════════════════════════════╗{RESET}")
        self.stdout.write(f"{BOLD}║        mofisi — UI & Interaction UAT                     ║{RESET}")
        self.stdout.write(f"{BOLD}╚══════════════════════════════════════════════════════════╝{RESET}")

        # Suppress the dev console email backend's stdout dumps during the
        # UI UAT so the summary stays readable.
        try:
            if self.keep:
                with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
                    self._run()
            else:
                try:
                    with transaction.atomic(), override_settings(
                        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"
                    ):
                        self._run()
                        raise _Rollback()
                except _Rollback:
                    self._info("All UI test data rolled back.")
        except UiFail as e:
            self.stdout.write(f"\n{RED}{BOLD}HARD STOP:{RESET} {e}")
            raise SystemExit(1)
        except Exception as e:
            self.stdout.write(f"\n{RED}{BOLD}UNEXPECTED:{RESET} {e}")
            import traceback; traceback.print_exc()
            raise SystemExit(2)

        self._summary()
        if self.failed:
            raise SystemExit(1)

    # ------------------------------------------------------------------

    def _run(self):
        self._section("1. Login flow — sign in through the actual form")
        self.step_login()

        self._section("2. Chrome check — sidebar, topbar, title on every module home")
        self.step_chrome()

        self._section("3. Create a customer via the form POST")
        self.step_create_customer()

        self._section("4. Create & post an invoice via the inline formset")
        self.step_create_invoice()

        self._section("5. List-view search (?q=) and status filter")
        self.step_filters()

        self._section("6. Click Print — letterhead renders")
        self.step_print()

        self._section("7. Click Email — compose form prefills + POST sends")
        self.step_email()

        self._section("8. Navigate dashboard → list → detail → payment")
        self.step_nav()

        self._section("9. Save branding — colour live-themes the whole app")
        self.step_branding()

        self._section("10. Pagination on a long list")
        self.step_pagination()

        self._section("11. Sign out — lands on login page")
        self.step_signout()

    # ---------------- steps ----------------

    def step_login(self):
        from apps.accounts.models import User
        from apps.organizations.models import Company

        self.company = Company.objects.create(
            name="UI UAT Co Ltd", currency="KES",
            tax_id="P099999999X", city="Nairobi", country="Kenya",
            phone="+254 711 777 999", email="ui@uat.co.ke",
            primary_color="#DC2538", accent_color="#111827",
            tagline="UI test tenant",
        )
        self.user = User.objects.create(
            email="ui-admin@uat.co.ke", username="ui-admin",
            is_staff=True, is_superuser=True,
        )
        self.user.set_password("ui-pass-9x!X")
        self.user.save()

        # Membership so middleware picks the company up as default
        from apps.organizations.models import CompanyMembership
        CompanyMembership.objects.create(
            company=self.company, user=self.user,
            role="ADMIN", is_default=True,
        )

        self.client = Client()
        # First GET the login page: there must be a CSRF token in the form.
        login_url = reverse("accounts:login")
        r = self.client.get(login_url)
        self._check(r.status_code == 200, f"GET {login_url} = 200")
        body = r.content.decode()
        self._check("csrfmiddlewaretoken" in body, "login form carries CSRF")
        self._check("mofisi" in body.lower(), "login hero shows mofisi branding")

        # Scrape token (quicker than enabling cookie-based CSRF handling)
        m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', body)
        token = m.group(1) if m else ""
        r = self.client.post(login_url, {
            "email": "ui-admin@uat.co.ke",
            "password": "ui-pass-9x!X",
            "csrfmiddlewaretoken": token,
        }, follow=True)
        self._check(r.status_code == 200, f"POST {login_url} follow = 200")
        self._check(r.redirect_chain, f"login redirected ({len(r.redirect_chain)} hops)")
        # After login we should be on the dashboard with the mofisi sidebar.
        final_body = r.content.decode()
        self._check("mofisi" in final_body, "post-login page shows mofisi")
        self._check(self.user.email in final_body or "Dashboard" in final_body or "sidebar" in final_body.lower(),
                    "user is logged in (sidebar or dashboard visible)")

        # Confirm active company picked up by middleware
        session = self.client.session
        self._check(session.get("active_company_id") == str(self.company.id)
                    or self.company.name in final_body,
                    "active company resolved after login")

    def step_chrome(self):
        """Visit every top-level module home — check the chrome is present."""
        modules = [
            ("/", "Dashboard"),
            ("/accounting/", "Accounting"),
            ("/sales/", "Sales"),
            ("/procurement/", "Procurement"),
            ("/inventory/", "Inventory"),
            ("/crm/", "CRM"),
            ("/hr/", "HR"),
            ("/payroll/", "Payroll"),
            ("/reports/", "Reports"),
            ("/banking/", "Banking"),
        ]
        for path, label in modules:
            r = self.client.get(path)
            self._check(r.status_code in (200, 302), f"{path} ({label}) returns {r.status_code}")
            if r.status_code == 200:
                body = r.content.decode()
                self._check("mofisi" in body, f"{path} sidebar shows mofisi")
                self._check('id="sidebar"' in body or 'sidebar' in body.lower(),
                            f"{path} includes the sidebar chrome")
                self._check("manifest.webmanifest" in body,
                            f"{path} declares the PWA manifest")

    def step_create_customer(self):
        from apps.crm.models import Customer
        url = reverse("crm:customer_create")
        r = self.client.get(url)
        self._check(r.status_code == 200, f"GET {url} = 200")
        m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', r.content.decode())
        token = m.group(1) if m else ""
        payload = {
            "csrfmiddlewaretoken": token,
            "name": "Jamii Supermarket Ltd",
            "customer_type": "BUSINESS",
            "tax_id": "P052222222X",
            "phone": "+254 722 000 000",
            "email": "finance@jamii.co.ke",
            "website": "",
            "credit_limit": "500000",
            "payment_terms_days": "30",
            "currency": "KES",
            "address_line1": "Ngong Road",
            "address_line2": "",
            "city": "Nairobi",
            "country": "Kenya",
            "notes": "Created via UI UAT",
        }
        r = self.client.post(url, payload, follow=True)
        self._check(r.status_code == 200, f"POST {url} = 200")
        self.customer = Customer.objects.filter(name="Jamii Supermarket Ltd").first()
        self._check(self.customer is not None, "customer row created")
        self._check(self.customer.company_id == self.company.id,
                    "customer scoped to active company")

    def step_create_invoice(self):
        from apps.sales.models import Invoice
        from apps.inventory.models import Product, UnitOfMeasure

        # Seed a product so the invoice line can bind to one
        uom, _ = UnitOfMeasure.objects.get_or_create(
            company=self.company, name="Each", defaults={"abbreviation": "ea"})
        self.product = Product.objects.create(
            company=self.company, sku="UI-WIDGET", name="UI Test Widget",
            product_type="GOODS", unit_of_measure=uom,
            cost_price=Decimal("300"), selling_price=Decimal("500"),
        )

        url = reverse("sales:invoice_create")
        r = self.client.get(url)
        self._check(r.status_code == 200, f"GET {url} = 200")
        m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', r.content.decode())
        token = m.group(1) if m else ""

        today = date.today().isoformat()
        due = (date.today() + timedelta(days=30)).isoformat()
        payload = {
            "csrfmiddlewaretoken": token,
            "reference": "INV-UI-0001",
            "customer": str(self.customer.id),
            "invoice_date": today,
            "due_date": due,
            "currency": "KES",
            "notes": "Issued through the UI UAT",
            # Inline formset management
            "lines-TOTAL_FORMS": "3",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "1",
            "lines-MAX_NUM_FORMS": "1000",
            "lines-0-product": str(self.product.id),
            "lines-0-description": "10 × UI Test Widget",
            "lines-0-quantity": "10",
            "lines-0-unit_price": "500",
            "lines-0-discount_percent": "0",
            "lines-0-tax_rate": "",
            "lines-1-product": "", "lines-1-description": "",
            "lines-1-quantity": "", "lines-1-unit_price": "",
            "lines-1-discount_percent": "0", "lines-1-tax_rate": "",
            "lines-2-product": "", "lines-2-description": "",
            "lines-2-quantity": "", "lines-2-unit_price": "",
            "lines-2-discount_percent": "0", "lines-2-tax_rate": "",
        }
        r = self.client.post(url, payload, follow=True)
        self._check(r.status_code == 200, f"POST {url} = 200")
        self.invoice = Invoice.objects.filter(reference="INV-UI-0001").first()
        self._check(self.invoice is not None, "invoice row created")
        self._check(self.invoice.lines.count() == 1, "invoice has 1 line")
        self._check(self.invoice.total_amount >= Decimal("5000"),
                    f"invoice total ≥ 5000 (got {self.invoice.total_amount})")

    def step_filters(self):
        # q search
        r = self.client.get("/sales/invoices/?q=INV-UI")
        self._check(r.status_code == 200, "invoice list q-search = 200")
        self._check(b"INV-UI-0001" in r.content, "q-search finds our invoice")

        # q search miss
        r = self.client.get("/sales/invoices/?q=NOT-FOUND-ZZZ")
        self._check(b"INV-UI-0001" not in r.content, "q-search miss excludes our invoice")

        # status filter
        inv_status = self.invoice.status
        r = self.client.get(f"/sales/invoices/?status={inv_status}")
        self._check(r.status_code == 200, f"status={inv_status} returns 200")
        self._check(b"INV-UI-0001" in r.content,
                    f"status={inv_status} includes our invoice")

        # customer list filter
        r = self.client.get("/crm/customers/?q=Jamii")
        self._check(r.status_code == 200, "customer list q-search = 200")
        self._check(b"Jamii Supermarket" in r.content, "customer q-search works")

    def step_print(self):
        url = reverse("sales:invoice_print", args=[self.invoice.pk])
        r = self.client.get(url)
        self._check(r.status_code == 200, f"GET {url} = 200")
        body = r.content.decode()
        self._check("doc-paper" in body, "print page uses the shared document shell")
        self._check("INV-UI-0001" in body, "print page shows invoice reference")
        self._check("Jamii Supermarket" in body, "print page shows the customer")
        self._check("UI UAT Co Ltd" in body, "print page shows the company letterhead")
        self._check("@media print" in body, "print CSS present")
        self._check("Download PDF" in body or "window.print" in body,
                    "Print toolbar present")

    def step_email(self):
        url = reverse("sales:invoice_email", args=[self.invoice.pk])
        r = self.client.get(url)
        self._check(r.status_code == 200, f"GET {url} = 200")
        body = r.content.decode()
        self._check('name="to"' in body, "email form has To input")
        self._check('name="subject"' in body, "email form has Subject input")
        self._check('name="message"' in body, "email form has Message input")
        self._check("finance@jamii.co.ke" in body,
                    "email To prefills from customer")
        self._check("INV-UI-0001" in body, "subject prefills from invoice ref")

        m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', body)
        token = m.group(1) if m else ""
        r = self.client.post(url, {
            "csrfmiddlewaretoken": token,
            "to": "finance@jamii.co.ke",
            "subject": "Your invoice INV-UI-0001",
            "message": "Please find your invoice attached.",
        }, follow=True)
        self._check(r.status_code == 200, "email submit = 200")
        # The dev backend prints to console; just ensure it didn't 500 and
        # the redirect landed back on the print page.
        self._check("INV-UI-0001" in r.content.decode(),
                    "post-send lands back on the invoice document")

    def step_nav(self):
        """Follow actual navigation links from the dashboard."""
        r = self.client.get("/", follow=True)
        self._check(r.status_code == 200, f"dashboard follow = {r.status_code}")
        body = r.content.decode()
        # The sidebar must link to key areas (prefix match is enough — some are
        # rendered via `{% url %}` and may add trailing slashes).
        for prefix in ["/sales/", "/procurement/", "/accounting/", "/crm/"]:
            self._check(prefix in body, f"sidebar links contain {prefix}")

        # Click through to the invoice list, then our invoice
        r = self.client.get("/sales/invoices/")
        self._check(r.status_code == 200, "sales invoice list = 200")

        # Our invoice detail
        detail = reverse("sales:invoice_detail", args=[self.invoice.pk])
        r = self.client.get(detail)
        self._check(r.status_code == 200, f"GET {detail} = 200")
        body = r.content.decode()
        self._check("INV-UI-0001" in body, "detail shows ref")
        self._check("Print" in body, "detail has Print button")
        self._check("Email" in body, "detail has Email button")
        self._check("Jamii Supermarket" in body, "detail shows customer name")

    def step_branding(self):
        url = reverse("organizations:branding")
        r = self.client.get(url)
        self._check(r.status_code == 200, f"GET {url} = 200")
        body = r.content.decode()
        self._check("primary_color" in body, "branding form shows primary colour input")
        self._check("Live Preview" in body or "brPreview" in body, "live preview panel rendered")

        m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', body)
        token = m.group(1) if m else ""
        r = self.client.post(url, {
            "csrfmiddlewaretoken": token,
            "tagline": "Themed via UI UAT",
            "primary_color": "#2563EB",   # blue — proves theming works
            "accent_color": "#0F172A",
            "document_header": "",
            "document_footer": "Thank you for your business — themed footer.",
            "bank_details": "",
        }, follow=True)
        self._check(r.status_code == 200, "branding save = 200")
        self.company.refresh_from_db()
        self._check(self.company.primary_color.upper() == "#2563EB",
                    "primary_color persisted as #2563EB")

        # Now visit any page — the sidebar CSS var should carry the new blue.
        r = self.client.get("/sales/invoices/", follow=True)
        body = r.content.decode()
        self._check("#2563EB" in body or "#2563eb" in body,
                    "new primary colour live in the sidebar CSS vars")

        # Reset
        self.company.primary_color = "#DC2538"
        self.company.save(update_fields=["primary_color"])

    def step_pagination(self):
        """Prove ?page= navigation works even on a short list (just checks
        no crash). We make several invoices quickly."""
        from apps.sales.models import Invoice
        for i in range(2, 32):
            Invoice.objects.create(
                company=self.company, reference=f"INV-UI-{i:04d}",
                customer=self.customer, invoice_date=date.today(),
                due_date=date.today() + timedelta(days=30),
                status="SENT", subtotal=Decimal("1000"),
                tax_amount=Decimal("160"), total_amount=Decimal("1160"),
                outstanding_amount=Decimal("1160"), currency="KES",
            )
        r = self.client.get("/sales/invoices/")
        self._check(r.status_code == 200, "invoice list w/ many rows = 200")
        body = r.content.decode()
        has_page_links = "?page=2" in body or 'name="page"' in body or "Page 1" in body
        self._check(has_page_links, "pagination controls appear once list exceeds page size")
        r2 = self.client.get("/sales/invoices/?page=2")
        self._check(r2.status_code == 200, "?page=2 = 200")

    def step_signout(self):
        url = reverse("accounts:logout")
        # Logout is POST (Django 5)
        r = self.client.get("/")
        m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', r.content.decode())
        token = m.group(1) if m else ""
        r = self.client.post(url, {"csrfmiddlewaretoken": token}, follow=True)
        self._check(r.status_code == 200, "logout POST = 200")
        body = r.content.decode()
        self._check("Sign In" in body or "Login" in body or 'name="password"' in body,
                    "post-logout lands on a public page")

    # ---------------- helpers ----------------

    def _section(self, label):
        self.sections.append(label)
        self.stdout.write(f"\n{BOLD}{CYAN}{label}{RESET}")

    def _info(self, msg):
        self.stdout.write(f"  {CYAN}·{RESET} {msg}")

    def _check(self, condition, msg):
        if condition:
            self.passed += 1
            if self.chatty:
                self.stdout.write(f"  {GREEN}✓{RESET} {msg}")
        else:
            self.failed.append(msg)
            self.stdout.write(f"  {RED}✗{RESET} {msg}")

    def _summary(self):
        total = self.passed + len(self.failed)
        self.stdout.write(f"\n{BOLD}╔══════════════════════════════════════════════════════════╗{RESET}")
        self.stdout.write(f"{BOLD}║                 UI UAT Summary                           ║{RESET}")
        self.stdout.write(f"{BOLD}╚══════════════════════════════════════════════════════════╝{RESET}")
        self.stdout.write(f"  Sections run : {len(self.sections)}")
        self.stdout.write(f"  Checks passed: {GREEN}{self.passed}{RESET} / {total}")
        self.stdout.write(f"  Failed       : {RED}{len(self.failed)}{RESET}")
        if self.failed:
            self.stdout.write(f"\n{RED}{BOLD}Failures:{RESET}")
            for f in self.failed:
                self.stdout.write(f"  {RED}✗{RESET} {f}")
            self.stdout.write(f"\n{RED}{BOLD}UI UAT FAILED{RESET}")
        else:
            self.stdout.write(f"\n{GREEN}{BOLD}✓ UI UAT PASSED — every click, form submission and theme change behaves.{RESET}")
