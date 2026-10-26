"""Regression checks for the sales and receivables fixes.

Usage:
    python manage.py test_fix_sales          # rolls the whole run back
    python manage.py test_fix_sales --keep   # leave the test company behind

Each section replays a tester's reproduction through the real screens, the way
a bookkeeper would, and checks the ledger says what the paper says:

    1. a posted invoice cannot be rewritten — only a draft can
    2. a draft switched to another currency (or date) books that day's rate
    3. VAT, revenue and AR post exactly the rounded figures on the invoice,
       in the invoice's money and in the book's, and so does a credit note
    4. a unit can only be credited once, however many notes are raised
    5. NaN, infinity and nonsense in the number boxes give a message, not a 500
    6. negative and absurd figures on invoice and order forms are refused
    7. a draft raised from an order keeps the order's billed quantities honest
    8. converting an order bills exactly the order's total
    9. the same receipt or invoice number cannot be used twice
"""
import datetime
from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand
from django.db import transaction
from django.test import Client
from django.test.utils import setup_test_environment, teardown_test_environment
from django.urls import reverse

D = Decimal
Z = D("0")
USD_RATE = D("131.25")

BLANK_LINE = {
    "product": "", "description": "", "quantity": "", "unit_price": "",
    "discount_percent": "0", "tax_rate": "",
}


def q2(value):
    return D(str(value)).quantize(D("0.01"), rounding=ROUND_HALF_UP)


def formset(prefix, items, blank=BLANK_LINE, blanks=1):
    """Management form + row data for an inline formset of new rows."""
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(items) + blanks),
        f"{prefix}-INITIAL_FORMS": "0",
        f"{prefix}-MIN_NUM_FORMS": "1",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    for i, row in enumerate(items):
        for key, value in row.items():
            data[f"{prefix}-{i}-{key}"] = "" if value is None else str(value)
    for j in range(blanks):
        for key, value in (blank or {}).items():
            data[f"{prefix}-{len(items) + j}-{key}"] = "" if value is None else str(value)
    return data


def merge(*parts):
    out = {}
    for part in parts:
        out.update(part)
    return out


class Command(BaseCommand):
    help = "Replay the sales/AR bug reproductions through the forms and check the fixes."

    def add_arguments(self, parser):
        parser.add_argument("--keep", action="store_true",
                            help="Commit the test data instead of rolling it back.")

    def handle(self, *args, **opts):
        self.passed = 0
        self.failures = []
        self.today = datetime.date.today()
        setup_test_environment()
        try:
            with transaction.atomic():
                self.run()
                if not opts["keep"]:
                    transaction.set_rollback(True)
        finally:
            teardown_test_environment()

        self.stdout.write("")
        self.stdout.write(f"Checks passed: {self.passed}")
        if self.failures:
            self.stdout.write(self.style.ERROR(f"Failures: {len(self.failures)}"))
            for line in self.failures:
                self.stderr.write(self.style.ERROR(f"  FAIL {line}"))
        else:
            self.stdout.write(self.style.SUCCESS("Failures: 0"))
        if not opts["keep"]:
            self.stdout.write("Test data rolled back (use --keep to retain it).")
        if self.failures:
            raise SystemExit(1)

    # ────────────────────────────── reporting ────────────────────────────────

    def section(self, title):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"-- {title}"))

    def expect(self, label, condition, detail=""):
        if condition:
            self.passed += 1
            self.stdout.write(f"  ok   {label}" + (f"  ({detail})" if detail else ""))
            return True
        self.failures.append(f"{label} — {detail}" if detail else label)
        self.stdout.write(self.style.ERROR(f"  FAIL {label} {detail}"))
        return False

    def flashes(self, client, response):
        """Messages only appear once the redirect target renders them."""
        if response is None:
            return []
        location = response.get("Location") if response.status_code in (301, 302, 303) else None
        target = client.get(location) if location else response
        ctx = getattr(target, "context", None)
        if not ctx or "messages" not in ctx:
            return []
        return [str(m.message) for m in ctx["messages"]]

    def errors(self, response):
        ctx = getattr(response, "context", None) or {}
        out = []
        form = ctx.get("form")
        if form is not None and form.errors:
            out += [f"{k}: {' '.join(v)}" for k, v in form.errors.items()]
        lines = ctx.get("line_formset")
        if lines is not None:
            out += [str(e) for e in lines.non_form_errors()]
            out += [str(dict(e)) for e in lines.errors if e]
        return out

    def send(self, client, label, url, data):
        """POST, turning a server error into a failed check instead of a crash."""
        try:
            response = client.post(url, data)
        except Exception as exc:  # a 500 in the view
            self.expect(f"{label} answers without a server error", False,
                        f"{type(exc).__name__}: {exc}")
            return None
        if response.status_code >= 500:
            self.expect(f"{label} answers without a server error", False,
                        f"HTTP {response.status_code}")
            return None
        return response

    def accepted(self, client, label, url, data):
        response = self.send(client, label, url, data)
        ok = response is not None and response.status_code in (301, 302, 303)
        self.expect(f"{label} is accepted", ok,
                    "" if ok or response is None else
                    f"HTTP {response.status_code} {self.errors(response)}")
        return response

    def refused(self, client, label, url, data, needle=None):
        """A form that must come back with its errors (HTTP 200), nothing saved."""
        response = self.send(client, label, url, data)
        if response is None:
            return None
        errs = self.errors(response)
        ok = response.status_code == 200 and bool(errs)
        if ok and needle:
            ok = any(needle.lower() in e.lower() for e in errs)
        self.expect(f"{label} is refused by the form", ok,
                    f"HTTP {response.status_code} {errs}")
        return response

    # ─────────────────────────────── fixtures ────────────────────────────────

    def run(self):
        from apps.accounts.models import User

        stamp = datetime.datetime.now().strftime("%H%M%S%f")
        user = User.objects.create(username=f"salesfix.{stamp}",
                                   email=f"salesfix.{stamp}@smoke.local",
                                   first_name="Sam", last_name="Sales")
        user.set_password("smoke-test-pw")
        user.save()
        self.user = user
        client = Client()
        client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        self.client_ = client

        self.section("0. A company to sell from")
        self.setup_company(client, stamp)

        self.section("1. Posted invoices are not edited")
        self.check_posted_invoice_locked(client)
        self.section("2. A draft switched to dollars books the dollar rate")
        self.check_draft_currency_restamped(client)
        self.section("3. The ledger posts the invoice's own rounded figures")
        self.check_rounding(client)
        self.section("4. A unit is credited once")
        self.check_credit_quantities(client)
        self.section("5. Nonsense in a number box is a message, not a 500")
        self.check_junk_numbers(client)
        self.section("6. Negative and absurd line figures are refused")
        self.check_line_figures(client)
        self.section("7. A draft raised from an order keeps the order honest")
        self.check_order_sync(client)
        self.section("8. Converting an order bills the order's total")
        self.check_order_total(client)
        self.section("9. One number, one receipt; one number, one invoice")
        self.check_duplicate_references(client)

    def setup_company(self, client, stamp):
        from apps.accounting.models import Account, Currency, ExchangeRate, TaxRate
        from apps.crm.models import Customer
        from apps.inventory.models import Product, UnitOfMeasure
        from apps.organizations.models import Company, CompanyMembership, Warehouse

        name = f"Salesfix Traders {stamp}"
        client.post(reverse("organizations:company_create"), {
            "name": name, "legal_name": f"{name} Limited",
            "registration_number": f"PVT-{stamp}", "tax_id": f"A{stamp}Z",
            "vat_number": "VAT-001", "currency": "KES", "timezone": "Africa/Nairobi",
            "fiscal_year_start_month": "1", "industry": "Trading",
            "phone": "+254 700 000 000", "email": f"info@{stamp}.smoke.local",
            "website": "", "address_line1": "1 Commerce Street", "address_line2": "",
            "city": "Nairobi", "state_province": "Nairobi", "postal_code": "00100",
            "country": "Kenya",
        })
        company = Company.objects.filter(name=name).first()
        if not self.expect("company created through the form", company is not None):
            raise SystemExit(1)
        self.company = company
        membership = CompanyMembership.objects.filter(company=company, user=self.user).first()
        if membership:
            # These checks are about the figures, not who may type them: an
            # administrator may do every step, so no role wall gets in the way.
            membership.role = "ADMIN"
            membership.save(update_fields=["role"])
        # Land on a page so the active company is resolved for this session.
        client.get(reverse("sales:index"))

        self.account = lambda code: Account.objects.get(company=company, code=code)
        self.warehouse = Warehouse.objects.filter(company=company, is_default=True).first()
        self.vat = TaxRate.objects.filter(company=company, code="VAT16").first()
        usd = Currency.objects.filter(code="USD").first()
        self.expect("warehouse, VAT and USD are available",
                    bool(self.warehouse and self.vat and usd))
        ExchangeRate.objects.create(company=company, currency=usd,
                                    effective_date=self.today - datetime.timedelta(days=30),
                                    rate=USD_RATE, source="MANUAL")
        uom, _ = UnitOfMeasure.objects.get_or_create(
            company=company, abbreviation="PCS", defaults={"name": "Pieces", "is_base": True})

        def product(sku, name, price):
            return Product.objects.create(
                company=company, sku=sku, name=name, product_type="SERVICE",
                unit_of_measure=uom, selling_price=D(price), track_inventory=False)

        self.cement = product("CEM-50", "Cement 50kg", "850")
        self.iron = product("IRN-12", "Iron bar 12mm", "1100")
        self.service = product("SVC-INS", "Installation", "2500")
        self.customer = Customer.objects.create(company=company, name="Umoja Hardware")
        self.other_customer = Customer.objects.create(company=company, name="Zuri Builders")

    # ─────────────────────────────── helpers ─────────────────────────────────

    def line(self, product=None, qty="1", price="100", disc="0", tax=True, desc=None):
        return {"product": product.pk if product else "",
                "description": desc or (product.name if product else "Custom item"),
                "quantity": qty, "unit_price": price, "discount_percent": disc,
                "tax_rate": self.vat.pk if tax else ""}

    def make_invoice(self, client, reference, lines, currency="KES", customer=None):
        from apps.sales.models import Invoice
        header = {"reference": reference, "customer": (customer or self.customer).pk,
                  "invoice_date": str(self.today),
                  "due_date": str(self.today + datetime.timedelta(days=30)),
                  "currency": currency, "notes": ""}
        self.accepted(client, f"invoice {reference}", reverse("sales:invoice_create"),
                      merge(header, formset("lines", lines)))
        return Invoice.objects.filter(company=self.company, reference=reference).first()

    def make_order(self, client, reference, lines, discount="0", currency="KES"):
        from apps.sales.models import SalesOrder
        header = {"reference": reference, "customer": self.customer.pk,
                  "order_date": str(self.today), "delivery_date": "",
                  "warehouse": self.warehouse.pk, "sales_rep": "", "currency": currency,
                  "discount_percent": discount, "branch": "", "notes": ""}
        self.accepted(client, f"order {reference}", reverse("sales:order_create"),
                      merge(header, formset("lines", lines)))
        order = SalesOrder.objects.filter(company=self.company, reference=reference).first()
        if order:
            client.post(reverse("sales:order_confirm", args=[order.pk]), {})
            order.refresh_from_db()
        return order

    def convert(self, client, order):
        before = set(order.invoices.values_list("pk", flat=True))
        client.post(reverse("sales:order_invoice", args=[order.pk]), {})
        fresh = order.invoices.exclude(pk__in=before).first()
        order.refresh_from_db()
        return fresh

    def edit_data(self, invoice, line_rows, **header):
        """The edit form as a browser sends it, with each saved line's id."""
        data = {"reference": invoice.reference, "customer": invoice.customer_id,
                "invoice_date": str(invoice.invoice_date), "due_date": str(invoice.due_date),
                "currency": invoice.currency, "notes": invoice.notes or ""}
        data.update(header)
        data.update({"lines-TOTAL_FORMS": str(len(line_rows)),
                     "lines-INITIAL_FORMS": str(len(line_rows)),
                     "lines-MIN_NUM_FORMS": "1", "lines-MAX_NUM_FORMS": "1000"})
        for i, (saved, row) in enumerate(line_rows):
            values = {"id": saved.pk, "invoice": invoice.pk,
                      "product": saved.product_id or "", "description": saved.description,
                      "quantity": saved.quantity, "unit_price": saved.unit_price,
                      "discount_percent": saved.discount_percent,
                      "tax_rate": saved.tax_rate_id or ""}
            values.update(row)
            for key, value in values.items():
                data[f"lines-{i}-{key}"] = "" if value is None else str(value)
        return data

    @staticmethod
    def entry_total(entry, code, side):
        if entry is None:
            return Z
        field = "debit_amount" if side == "debit" else "credit_amount"
        return sum((getattr(l, field) for l in entry.lines.filter(account__code=code)), Z)

    def pay(self, client, reference, amount, currency="KES", method="BANK_TRANSFER",
            code="", allocations=None, customer=None):
        data = {"reference": reference, "customer": (customer or self.customer).pk,
                "payment_date": str(self.today), "amount": amount, "currency": currency,
                "payment_method": method, "bank_account": "",
                "transaction_reference": code, "notes": ""}
        data.update(allocations or {})
        return self.send(client, f"receipt {reference}", reverse("sales:payment_create"), data)

    # ──────────────────────────────── checks ─────────────────────────────────

    def check_posted_invoice_locked(self, client):
        inv = self.make_invoice(client, "FIX-1-SENT", [self.line(self.service, "2", "2500")])
        if not self.expect("a posted invoice to try editing", inv and inv.journal_entry_id):
            return
        saved = inv.lines.first()
        url = reverse("sales:invoice_edit", args=[inv.pk])
        response = client.get(url)
        self.expect("the edit page of a posted invoice opens read-only",
                    response.status_code == 200 and response.context.get("locked")
                    and all(f.disabled for f in response.context["form"].fields.values()),
                    f"HTTP {response.status_code}")

        for label, header, row in (
                ("lines cut to 1 x 1000", {}, {"quantity": "1", "unit_price": "1000"}),
                ("customer and currency changed", {"customer": self.other_customer.pk,
                                                   "currency": "USD"}, {})):
            response = self.send(client, f"edit a SENT invoice: {label}", url,
                                 self.edit_data(inv, [(saved, row)], **header))
            inv.refresh_from_db()
            saved.refresh_from_db()
            self.expect(f"editing a SENT invoice is refused ({label})",
                        response is not None and response.status_code == 302
                        and inv.total_amount == D("5800.00") and saved.quantity == D("2")
                        and inv.customer_id == self.customer.pk and inv.currency == "KES",
                        f"{inv.total_amount} {inv.customer_id == self.customer.pk} {inv.currency}")
        self.expect("the refusal says how a posted invoice is corrected",
                    any("credit note" in m for m in self.flashes(client, response)))

        self.pay(client, "FIX-1-PAY", "3000", allocations={f"allocate_{inv.pk}": "3000"})
        inv.refresh_from_db()
        response = self.send(client, "edit a PARTIAL invoice", url,
                             self.edit_data(inv, [(saved, {"quantity": "1"})]))
        inv.refresh_from_db()
        self.expect("editing a part-paid invoice is refused",
                    inv.status == "PARTIAL" and inv.total_amount == D("5800.00")
                    and inv.outstanding_amount == D("2800.00"),
                    f"{inv.status} {inv.total_amount} {inv.outstanding_amount}")

    def check_draft_currency_restamped(self, client):
        order = self.make_order(client, "FIX-2-SO", [self.line(self.service, "1", "100", tax=False)])
        draft = self.convert(client, order) if order else None
        if not self.expect("a KES draft raised from an order", draft and draft.status == "DRAFT"):
            return
        self.expect("the KES draft carries rate 1", draft.rate == D("1"), str(draft.exchange_rate))
        url = reverse("sales:invoice_edit", args=[draft.pk])
        self.accepted(client, "switch the draft to USD", url,
                      self.edit_data(draft, [(draft.lines.first(), {})], currency="USD"))
        draft.refresh_from_db()
        self.expect("the USD draft books the USD rate, not 1",
                    draft.currency == "USD" and draft.exchange_rate == USD_RATE
                    and draft.total_amount_base == D("13125.00"),
                    f"{draft.currency} @ {draft.exchange_rate} base {draft.total_amount_base}")
        client.post(reverse("sales:invoice_post", args=[draft.pk]), {})
        draft.refresh_from_db()
        self.expect("posted at the USD rate: Dr AR 13,125.00",
                    self.entry_total(draft.journal_entry, "1300", "debit") == D("13125.00"),
                    str(self.entry_total(draft.journal_entry, "1300", "debit")))

        # And back again: a USD draft switched to the book's money is worth 1.
        order = self.make_order(client, "FIX-2-SO-B", [self.line(self.service, "1", "100", tax=False)],
                                currency="USD")
        draft = self.convert(client, order) if order else None
        if draft:
            self.accepted(client, "switch a USD draft to KES",
                          reverse("sales:invoice_edit", args=[draft.pk]),
                          self.edit_data(draft, [(draft.lines.first(), {})], currency="KES"))
            draft.refresh_from_db()
            self.expect("the KES draft is worth 1 of itself",
                        draft.rate == D("1") and draft.total_amount_base == D("100.00"),
                        f"{draft.exchange_rate} base {draft.total_amount_base}")

    def check_rounding(self, client):
        from apps.sales.models import Invoice
        inv = self.make_invoice(client, "FIX-3-USD", [self.line(self.service, "3", "10.01")],
                                currency="USD")
        if not self.expect("a posted USD invoice", inv and inv.journal_entry_id):
            return
        je = inv.journal_entry
        self.expect("header: tax 4.80, total 34.83, base 4,571.44",
                    inv.tax_amount == D("4.80") and inv.total_amount == D("34.83")
                    and inv.total_amount_base == D("4571.44"),
                    f"{inv.tax_amount} / {inv.total_amount} / {inv.total_amount_base}")
        self.expect("VAT posted is the invoice's 4.80 at 131.25 = 630.00",
                    self.entry_total(je, "2200", "credit") == D("630.00"),
                    str(self.entry_total(je, "2200", "credit")))
        self.expect("AR posted is the invoice's base, 4,571.44",
                    self.entry_total(je, "1300", "debit") == D("4571.44"),
                    str(self.entry_total(je, "1300", "debit")))
        revenue = sum((l.credit_amount for l in je.lines.filter(account__account_type="REVENUE")), Z)
        self.expect("revenue takes the rest: 3,941.44", revenue == D("3941.44"), str(revenue))
        base_before = inv.total_amount_base
        Invoice.objects.get(pk=inv.pk).save()
        inv.refresh_from_db()
        self.expect("the stored base does not move on the next save",
                    inv.total_amount_base == base_before, f"{base_before} -> {inv.total_amount_base}")

        response = self.pay(client, "FIX-3-PAY", "34.83", currency="USD",
                            allocations={f"allocate_{inv.pk}": "34.83"})
        inv.refresh_from_db()
        payment = inv.allocations.first().payment if inv.allocations.exists() else None
        left = (self.entry_total(je, "1300", "debit")
                - self.entry_total(payment and payment.journal_entry, "1300", "credit"))
        self.expect("paid in full at the same rate, nothing is left on AR",
                    inv.status == "PAID" and left == Z, f"{inv.status}, AR left {left}")

        kes = self.make_invoice(client, "FIX-3-KES", [
            self.line(None, "1", "1.03", desc="Sample A"), self.line(None, "1", "1.03", desc="Sample B"),
            self.line(None, "1", "1.03", desc="Sample C")])
        if kes and kes.journal_entry_id:
            self.expect("three lines of 1.03 +16%: the invoice says 0.49 of VAT and so does the ledger",
                        kes.tax_amount == D("0.49")
                        and self.entry_total(kes.journal_entry, "2200", "credit") == D("0.49")
                        and self.entry_total(kes.journal_entry, "1300", "debit") == D("3.58"),
                        f"{kes.tax_amount} vs {self.entry_total(kes.journal_entry, '2200', 'credit')}")

        # A credit note posts its own rounded VAT the same way.
        usd = self.make_invoice(client, "FIX-3-USD-CN", [self.line(self.service, "5", "10.01")],
                                currency="USD")
        if not usd:
            return
        saved = usd.lines.first()
        client.post(reverse("sales:invoice_credit_note", args=[usd.pk]),
                    {f"credit_qty_{saved.pk}": "3", "reason": "returned"})
        note = Invoice.objects.filter(original_invoice=usd, status="CREDIT_NOTE").first()
        if self.expect("a USD credit note was raised", note is not None):
            self.expect("the note's VAT is its 4.80 at 131.25 = 630.00",
                        note.tax_amount == D("4.80")
                        and self.entry_total(note.journal_entry, "2200", "debit") == q2(note.tax_amount * USD_RATE)
                        and self.entry_total(note.journal_entry, "1300", "credit") == note.total_amount_base,
                        f"{note.tax_amount} -> {self.entry_total(note.journal_entry, '2200', 'debit')}")

    def check_credit_quantities(self, client):
        from apps.sales.models import Invoice
        inv = self.make_invoice(client, "INV-UMO-2", [self.line(self.cement, "2", "850"),
                                                      self.line(self.service, "4", "2500")])
        if not self.expect("INV-UMO-2 posted", inv and inv.journal_entry_id):
            return
        cement = inv.lines.get(product=self.cement)
        url = reverse("sales:invoice_credit_note", args=[inv.pk])
        notes = lambda: Invoice.objects.filter(original_invoice=inv, status="CREDIT_NOTE").count()
        client.post(url, {f"credit_qty_{cement.pk}": "2", "reason": "cn 1"})
        self.expect("the first note for both bags goes through", notes() == 1, str(notes()))
        for k in (2, 3):
            response = client.post(url, {f"credit_qty_{cement.pk}": "2", "reason": f"cn {k}"})
            self.expect(f"note {k} for the same two bags is refused", notes() == 1, str(notes()))
        self.expect("the refusal says what was already credited",
                    any("already gave back 2" in m for m in self.flashes(client, response)))

        five = self.make_invoice(client, "FIX-4-FIVE", [self.line(self.cement, "5", "850")])
        if not five:
            return
        saved = five.lines.first()
        url = reverse("sales:invoice_credit_note", args=[five.pk])
        for qty in ("2", "2"):
            client.post(url, {f"credit_qty_{saved.pk}": qty})
        client.post(url, {f"credit_qty_{saved.pk}": "2"})
        credited = sum((l.quantity for n in Invoice.objects.filter(original_invoice=five)
                        for l in n.lines.all()), Z)
        self.expect("5 billed: 2 + 2 credited, a third 2 is refused", credited == D("4"), str(credited))
        client.post(url, {f"credit_qty_{saved.pk}": "1"})
        credited = sum((l.quantity for n in Invoice.objects.filter(original_invoice=five)
                        for l in n.lines.all()), Z)
        five.refresh_from_db()
        self.expect("the last bag can still be credited", credited == D("5")
                    and five.status == "CREDITED", f"{credited} {five.status}")

    def check_junk_numbers(self, client):
        from apps.sales.models import CustomerPayment, Invoice
        from apps.inventory.models import StockMovement
        junk = ["NaN", "nan", "sNaN", "Infinity", "-Infinity", "1e999999", "abc", "12,5"]

        inv = self.make_invoice(client, "FIX-5-INV", [self.line(self.service, "2", "2500")])
        if not inv:
            return
        saved = inv.lines.first()
        receipts = CustomerPayment.objects.filter(company=self.company).count()
        for text in junk:
            response = self.pay(client, f"FIX-5-PAY-{text}", "100",
                                allocations={f"allocate_{inv.pk}": text})
            self.expect(f"allocation '{text}' is refused with a message",
                        response is not None and response.status_code == 200
                        and any("not an amount" in e for e in self.errors(response)),
                        "" if response is None else str(self.errors(response)))
        response = self.pay(client, "FIX-5-PAY-bad-id", "100", allocations={"allocate_not-a-uuid": "50"})
        self.expect("an allocation against a malformed id is refused",
                    response is not None and response.status_code == 200)
        self.expect("no receipt was saved by the junk",
                    CustomerPayment.objects.filter(company=self.company).count() == receipts)

        notes = Invoice.objects.filter(original_invoice=inv).count()
        url = reverse("sales:invoice_credit_note", args=[inv.pk])
        for text in junk:
            response = self.send(client, f"credit quantity '{text}'", url,
                                 {f"credit_qty_{saved.pk}": text})
            self.expect(f"credit quantity '{text}' is refused with a message",
                        response is not None and response.status_code == 302
                        and any("not a quantity" in m for m in self.flashes(client, response)))
        self.expect("no credit note was raised by the junk",
                    Invoice.objects.filter(original_invoice=inv).count() == notes)

        order = self.make_order(client, "FIX-5-SO", [self.line(self.service, "3", "100")])
        if not order:
            return
        ol = order.lines.first()
        url = reverse("sales:order_deliver_record", args=[order.pk])
        for text in junk:
            response = self.send(client, f"delivery quantity '{text}'", url,
                                 {"warehouse": self.warehouse.pk, f"deliver_qty_{ol.pk}": text})
            self.expect(f"delivery quantity '{text}' is refused with a message",
                        response is not None and response.status_code == 302
                        and any("not a quantity" in m for m in self.flashes(client, response)))
        response = self.send(client, "delivery from warehouse 'abc'", url,
                             {"warehouse": "abc", f"deliver_qty_{ol.pk}": "1"})
        self.expect("a malformed warehouse id is refused with a message",
                    response is not None and response.status_code == 302
                    and any("warehouse" in m for m in self.flashes(client, response)))
        ol.refresh_from_db()
        self.expect("nothing was delivered by the junk",
                    ol.delivered_quantity == Z
                    and not StockMovement.objects.filter(sales_order=order).exists(),
                    str(ol.delivered_quantity))

    def check_line_figures(self, client):
        from apps.sales.models import Invoice, SalesOrder
        header = {"reference": "", "customer": self.customer.pk,
                  "invoice_date": str(self.today),
                  "due_date": str(self.today + datetime.timedelta(days=30)),
                  "currency": "KES", "notes": ""}
        url = reverse("sales:invoice_create")
        before = Invoice.objects.filter(company=self.company).count()
        for label, row in (("quantity -5", {"qty": "-5"}), ("quantity 0", {"qty": "0"}),
                           ("unit price -5", {"price": "-5"}),
                           ("discount -5%", {"disc": "-5"}), ("discount 150%", {"disc": "150"})):
            header["reference"] = f"FIX-6-{label}"
            self.refused(client, f"invoice line with {label}", url,
                         merge(header, formset("lines", [self.line(self.service, **row)])))
        self.expect("no invoice was saved from the bad lines",
                    Invoice.objects.filter(company=self.company).count() == before)

        order_header = {"reference": "FIX-6-SO", "customer": self.customer.pk,
                        "order_date": str(self.today), "delivery_date": "",
                        "warehouse": self.warehouse.pk, "sales_rep": "", "currency": "KES",
                        "discount_percent": "0", "branch": "", "notes": ""}
        url = reverse("sales:order_create")
        self.refused(client, "order header discount 150", url,
                     merge(order_header, {"discount_percent": "150"},
                           formset("lines", [self.line(self.service)])), needle="discount")
        for label, row in (("quantity -5", {"qty": "-5"}), ("unit price -5", {"price": "-5"}),
                           ("discount 150%", {"disc": "150"})):
            self.refused(client, f"order line with {label}", url,
                         merge(order_header, formset("lines", [self.line(self.service, **row)])))
        self.expect("no order was saved from the bad figures",
                    not SalesOrder.objects.filter(company=self.company, reference="FIX-6-SO").exists())

    def check_order_sync(self, client):
        order = self.make_order(client, "FIX-7-SO", [self.line(self.service, "20", "100")])
        first = self.convert(client, order) if order else None
        if not self.expect("SO for 20 converted to a draft", first is not None):
            return
        ol = order.lines.first()
        ol.refresh_from_db()
        self.expect("the order reads fully invoiced", order.status == "INVOICED"
                    and ol.invoiced_quantity == D("20"), f"{order.status} {ol.invoiced_quantity}")
        self.accepted(client, "cut the draft to 8", reverse("sales:invoice_edit", args=[first.pk]),
                      self.edit_data(first, [(first.lines.first(), {"quantity": "8"})]))
        order.refresh_from_db()
        ol.refresh_from_db()
        self.expect("the order now has 12 left to bill and is no longer INVOICED",
                    ol.invoiced_quantity == D("8") and order.status == "CONFIRMED",
                    f"{order.status} {ol.invoiced_quantity}")
        second = self.convert(client, order)
        self.expect("the remaining 12 can be billed",
                    second is not None and second.lines.first().quantity == D("12")
                    and order.status == "INVOICED",
                    f"{order.status} {second and second.lines.first().quantity}")
        if second:
            client.post(reverse("sales:invoice_cancel", args=[second.pk]), {})
            order.refresh_from_db()
            ol.refresh_from_db()
            self.expect("cancelling the second draft hands its 12 back",
                        ol.invoiced_quantity == D("8") and order.status == "CONFIRMED",
                        f"{order.status} {ol.invoiced_quantity}")
        first.delete()
        order.refresh_from_db()
        ol.refresh_from_db()
        self.expect("deleting the first draft hands its 8 back",
                    ol.invoiced_quantity == Z and order.status == "CONFIRMED",
                    f"{order.status} {ol.invoiced_quantity}")

    def check_order_total(self, client):
        order = self.make_order(client, "SO-UMOJA-1", [
            self.line(self.cement, "100", "850", "5"), self.line(self.iron, "50", "1100"),
            self.line(self.service, "1", "2500")], discount="2.5")
        if not order:
            return
        self.expect("the order totals 156,360.75", order.total_amount == D("156360.75"),
                    str(order.total_amount))
        invoice = self.convert(client, order)
        if not self.expect("the order converted", invoice is not None):
            return
        folded = invoice.lines.get(product=self.cement).discount_percent
        self.expect("5% then 2.5% is carried as exactly 7.375%", folded == D("7.375"), str(folded))
        self.expect("the invoice bills exactly the order's total",
                    invoice.total_amount == order.total_amount
                    and invoice.tax_amount == order.tax_amount,
                    f"invoice {invoice.total_amount} / order {order.total_amount}")
        response = client.get(reverse("sales:invoice_edit", args=[invoice.pk]))
        rendered = response.content.decode()
        self.expect("the draft's edit form shows the folded discount as 7.375",
                    response.status_code == 200 and 'value="7.375"' in rendered)

    def check_duplicate_references(self, client):
        from apps.sales.models import CustomerPayment, Invoice
        self.make_invoice(client, "FIX-9-INV", [self.line(self.service, "1", "1000")])
        response = self.pay(client, "RCPT-UMO-9", "500", method="MPESA", code="SJK42XYZ")
        self.expect("the first receipt is recorded",
                    response is not None and response.status_code == 302)
        count = CustomerPayment.objects.filter(company=self.company).count()
        response = self.pay(client, "RCPT-UMO-9", "500", method="MPESA", code="SJK42XYZ")
        self.expect("the same receipt sent twice is refused",
                    response is not None and response.status_code == 200
                    and CustomerPayment.objects.filter(company=self.company).count() == count,
                    str(self.errors(response) if response else ""))
        response = self.pay(client, "rcpt-umo-9", "500")
        self.expect("the number is matched whatever its case",
                    response is not None and response.status_code == 200
                    and any("already recorded" in e for e in self.errors(response)))
        response = self.pay(client, "RCPT-UMO-10", "500", method="MPESA", code="sjk42xyz")
        self.expect("a second live receipt quoting the same M-Pesa code is refused",
                    response is not None and response.status_code == 200
                    and any("M-Pesa code" in e for e in self.errors(response)))

        header = {"reference": "fix-9-inv", "customer": self.customer.pk,
                  "invoice_date": str(self.today),
                  "due_date": str(self.today + datetime.timedelta(days=30)),
                  "currency": "KES", "notes": ""}
        self.refused(client, "a second invoice numbered FIX-9-INV", reverse("sales:invoice_create"),
                     merge(header, formset("lines", [self.line(self.service)])),
                     needle="already used")
        self.expect("only one invoice carries the number",
                    Invoice.objects.filter(company=self.company,
                                           reference__iexact="FIX-9-INV").count() == 1)
