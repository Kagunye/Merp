"""Checks for offline mode (apps/core/offline.py and the till's sync endpoint).

    python manage.py test_fix_offline      # rolls back

    1. the offline files      -> service worker (site-wide scope), manifest,
                                 offline page and ping need no sign-in
    2. a form saved offline   -> replayed with its key it is saved once; the same
                                 key again is answered, not saved twice; a form
                                 with errors comes back as "fix it", kept on the
                                 device; signed out means "sign in", not lost
    3. a till ticket offline  -> rung into the open till with its own token;
                                 sent twice, it is taken once; with no open till
                                 it waits; a product not sold here is refused
    4. roles still apply      -> a viewer replaying a form gets "not allowed"
"""
import datetime
import json
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.test import Client
from django.test.utils import setup_test_environment, teardown_test_environment
from django.urls import reverse
from django.utils import timezone

D = Decimal


class Command(BaseCommand):
    help = "Checks for offline mode and syncing."

    def handle(self, *args, **opts):
        self.passed, self.failures = 0, []
        setup_test_environment()
        try:
            with transaction.atomic():
                self.run()
                transaction.set_rollback(True)
        finally:
            teardown_test_environment()
        self.stdout.write("")
        self.stdout.write(f"Checks passed: {self.passed}")
        if self.failures:
            self.stdout.write(self.style.ERROR(f"Failures: {len(self.failures)}"))
            for f in self.failures:
                self.stderr.write(self.style.ERROR(f"  FAIL {f}"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("Failures: 0"))

    def section(self, t):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"-- {t}"))

    def expect(self, label, cond, detail=""):
        if cond:
            self.passed += 1
            self.stdout.write(f"  ok   {label}" + (f"  ({detail})" if detail else ""))
            return True
        self.failures.append(f"{label} — {detail}" if detail else label)
        self.stdout.write(self.style.ERROR(f"  FAIL {label} {detail}"))
        return False

    def account(self, code):
        from apps.accounting.models import Account
        return Account.objects.get(company=self.company, code=code)

    def user(self, name, role):
        from apps.accounts.models import User
        from apps.organizations.models import CompanyMembership
        u = User.objects.create(username=f"{name}.{self.stamp}", email=f"{name}.{self.stamp}@fixoff.local",
                                first_name=name.title(), last_name="Test")
        u.set_password("fix-off-pw")
        u.save()
        CompanyMembership.objects.create(company=self.company, user=u, role=role, branch=self.hq, is_default=True)
        return u

    def client_for(self, u):
        c = Client(raise_request_exception=False, enforce_csrf_checks=False)
        c.force_login(u, backend="django.contrib.auth.backends.ModelBackend")
        return c

    def sync(self, client, url, key, data=None, body=None):
        headers = {"HTTP_X_OFFLINE_SYNC": "1", "HTTP_X_OFFLINE_KEY": key}
        if body is not None:
            r = client.post(url, data=json.dumps(body), content_type="application/json", **headers)
        else:
            r = client.post(url, data or {}, **headers)
        try:
            return r.status_code, json.loads(r.content or b"{}")
        except ValueError:
            return r.status_code, {"raw": r.content[:120]}

    def run(self):
        from apps.accounts.models import User
        from apps.core.models import OfflineSubmission
        from apps.inventory.models import Product, StockMovement, UnitOfMeasure
        from apps.organizations.models import Company, Warehouse
        from apps.pos import services as pos
        from apps.timesheets.models import TimesheetEntry

        self.stamp = datetime.datetime.now().strftime("%H%M%S%f")
        self.section("1. the offline files")
        anon = Client()
        r = anon.get("/sw.js")
        self.expect("the service worker is served from the site root, scope /",
                    r.status_code == 200 and r.get("Service-Worker-Allowed") == "/"
                    and "javascript" in r.get("Content-Type", ""), f"{r.status_code}")
        r = anon.get("/manifest.webmanifest")
        self.expect("the app manifest is served (install as an app)",
                    r.status_code == 200 and json.loads(r.content).get("start_url") == "/dashboard/")
        self.expect("the offline page needs no sign-in", anon.get("/offline/").status_code == 200)
        self.expect("the ping answers", anon.get("/ping/").status_code == 200)

        admin = User.objects.create(username=f"off.admin.{self.stamp}", email=f"off.admin.{self.stamp}@fixoff.local")
        admin.set_password("fix-off-pw")
        admin.save()
        name = f"Fix Offline {self.stamp}"
        self.client_for(admin).post(reverse("organizations:company_create"), {
            "name": name, "legal_name": name, "registration_number": f"PVT-{self.stamp}", "tax_id": "",
            "vat_number": "", "currency": "KES", "timezone": "Africa/Nairobi", "fiscal_year_start_month": "1",
            "industry": "Trading", "phone": "+254 700 000 004", "email": f"i@{self.stamp}.fixoff.local",
            "website": "", "address_line1": "4 Moi Avenue", "address_line2": "", "city": "Nairobi",
            "state_province": "Nairobi", "postal_code": "00100", "country": "Kenya"})
        self.company = Company.objects.get(name=name)
        self.hq = self.company.branches.get(is_headquarters=True)
        staff = self.user("off.staff", "EMPLOYEE")
        viewer = self.user("off.viewer", "VIEWER")
        cashier = self.user("off.cashier", "POS_CASHIER")

        self.section("2. a form saved offline")
        c = self.client_for(staff)
        url = reverse("timesheets:entry_create")
        day = str(timezone.localdate())
        form = {"entry_date": day, "hours": "3", "description": "Offline work", "project": "", "task": ""}
        code, res = self.sync(c, url, "offlinekey000001", form)
        self.expect("replayed with its key, the entry is saved", res.get("ok") and code == 200, f"{code} {res}")
        code, res = self.sync(c, url, "offlinekey000001", form)
        self.expect("the same key again is answered, not saved twice",
                    res.get("ok") and res.get("replayed")
                    and TimesheetEntry.objects.filter(user=staff, entry_date=day).count() == 1,
                    f"{TimesheetEntry.objects.filter(user=staff, entry_date=day).count()} entries")
        code, res = self.sync(c, url, "offlinekey000002", {**form, "hours": "30"})
        self.expect("a form with errors comes back as 'fix it' and is kept on the device",
                    not res.get("ok") and res.get("permanent") and res.get("error"), res.get("error"))
        code, res = self.sync(Client(), url, "offlinekey000003", form)
        self.expect("signed out: asked to sign in, not lost", not res.get("ok") and not res.get("permanent"),
                    f"{code} {res}")
        code, res = self.sync(c, url, "bad key!", form)
        self.expect("an item with no proper key is refused (it could not be de-duplicated)",
                    code == 400 and res.get("permanent"))

        self.section("3. a till ticket offline")
        uom = (UnitOfMeasure.objects.filter(company=self.company).first()
               or UnitOfMeasure.objects.create(company=self.company, name="Pieces", abbreviation="PCS", is_base=True))
        wh = Warehouse.objects.get(company=self.company, is_default=True)
        soda = Product.objects.create(company=self.company, sku="SODA", name="Soda", unit_of_measure=uom,
                                      cost_price=D("40"), selling_price=D("80"),
                                      income_account=self.account("4200"), cogs_account=self.account("5000"),
                                      inventory_account=self.account("1400"))
        StockMovement.objects.create(product=soda, warehouse=wh, movement_type="RECEIPT", quantity=D("20"),
                                     unit_cost=D("40"), movement_date=timezone.localdate(), reference="GRN")
        cc = self.client_for(cashier)
        turl = reverse("pos:terminal_sync")
        ticket = {"ticket_token": "devicetoken0001", "lines": [{"id": str(soda.pk), "qty": "2"}],
                  "payment_method": "CASH", "amount_tendered": "200", "warehouse": "",
                  "rung_at": timezone.now().isoformat()}
        code, res = self.sync(cc, turl, "tillkey0000000001", body=ticket)
        self.expect("with no open till the ticket waits (not refused)",
                    not res.get("ok") and not res.get("permanent"), res.get("error"))
        pos.open_session(self.company, cashier, self.hq, D("0"))
        code, res = self.sync(cc, turl, "tillkey0000000001", body=ticket)
        self.expect("with the till open it is rung", res.get("ok") and res.get("reference", "").startswith("POS-"),
                    f"{code} {res}")
        from apps.pos.models import POSSale
        sale = POSSale.objects.filter(submission_token="devicetoken0001").first()
        self.expect("…for 160.00 with 40.00 change, marked as rung offline",
                    sale and sale.total_amount == D("160.00") and sale.change_given == D("40.00")
                    and "rung offline" in sale.notes, sale and f"{sale.total_amount} {sale.notes}")
        code, res = self.sync(cc, turl, "tillkey0000000002", body=ticket)
        self.expect("sent again under a new key, the token still rings it once",
                    res.get("ok") and POSSale.objects.filter(submission_token="devicetoken0001").count() == 1,
                    str(res))
        bad = {**ticket, "ticket_token": "devicetoken0002", "lines": [{"id": "00000000-0000-0000-0000-000000000000", "qty": "1"}]}
        code, res = self.sync(cc, turl, "tillkey0000000003", body=bad)
        self.expect("a product not on this company's list is refused for fixing", not res.get("ok") and res.get("permanent"),
                    res.get("error"))

        self.section("4. roles still apply")
        code, res = self.sync(self.client_for(viewer), reverse("crm:customer_create"), "viewerkey0000001",
                              {"name": "Offline customer"})
        self.expect("a viewer's replay is refused as not allowed", not res.get("ok") and res.get("permanent"),
                    f"{code} {res.get('error')}")
        self.expect("every answered key is remembered once",
                    OfflineSubmission.objects.filter(key__in=["offlinekey000001", "tillkey0000000001"]).count() == 2)
