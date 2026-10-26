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
   10. a direct invoice issues its stocked goods at average cost; a credit note
       for returned goods brings them back at the cost they left at
   11. money on account is shown in, and netted by, every receivables report
   12. money on account can be applied to an invoice later
   13. an invoice takes its own day's rate; a statement closes at what is owed
       however the rate moved; applying credit books the realised exchange
   14. the dashboard's sales chart is net revenue
   15. a credit note, receipt or invoice form sent twice books once
   16. a due date or a settlement before the invoice date is refused
   17. messages print money to the cent
   18. an order nothing has come of can be cancelled
   19. a sales rep's invoice waits as a draft for finance to post
"""
import datetime
from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum
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
        self.section("10. A direct invoice issues its stock; a return brings it back at cost")
        self.check_direct_invoice_stock(client)
        self.section("11. Money on account is in every receivables report")
        self.check_on_account_reports(client)
        self.section("12. Credit on account can be applied to an invoice")
        self.check_apply_credit(client)
        self.section("13. Exchange: invoice-day rates, statements and applied credit")
        self.check_fx_settlements(client)
        self.section("14. The dashboard charts net revenue")
        self.check_sales_chart(client)
        self.section("15. A form sent twice books one document")
        self.check_double_submits(client)
        self.section("16. Dates that cannot be")
        self.check_dates(client)
        self.section("17. Messages print money to the cent and name what they refuse")
        self.check_messages(client)
        self.section("18. An order nothing has come of can be cancelled")
        self.check_order_cancel(client)
        self.section("19. Who may post an invoice")
        self.check_separation_of_duties(client)

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

    # ─────────────────────── second round of fixes ───────────────────────────

    def ar_control(self):
        """AR 1300 as the ledger carries it, debit-normal."""
        from apps.accounting.models import JournalEntryLine
        agg = JournalEntryLine.objects.filter(
            journal_entry__company=self.company, journal_entry__status="POSTED",
            account__code="1300").aggregate(dr=Sum("debit_amount"), cr=Sum("credit_amount"))
        return (agg["dr"] or Z) - (agg["cr"] or Z)

    def account_moved(self, code, since_ids, side):
        """Debits or credits on an account from entries not in `since_ids`."""
        from apps.accounting.models import JournalEntryLine
        field = "debit_amount" if side == "debit" else "credit_amount"
        qs = JournalEntryLine.objects.filter(
            journal_entry__company=self.company, journal_entry__status="POSTED",
            account__code=code).exclude(journal_entry_id__in=since_ids)
        return qs.aggregate(t=Sum(field))["t"] or Z

    def entry_ids(self):
        from apps.accounting.models import JournalEntry
        return set(JournalEntry.objects.filter(company=self.company).values_list("pk", flat=True))

    def credit(self, client, invoice, quantities, returned=False, token=None, reason="cn"):
        data = {f"credit_qty_{line.pk}": qty for line, qty in quantities}
        data["reason"] = reason
        if returned:
            data["goods_returned"] = "1"
        if token:
            data["form_token"] = token
        return self.send(client, f"credit note on {invoice.reference}",
                         reverse("sales:invoice_credit_note", args=[invoice.pk]), data)

    def check_direct_invoice_stock(self, client):
        from apps.inventory.models import Product, StockLevel, StockMovement
        from apps.sales.models import Invoice
        self.drill = Product.objects.create(
            company=self.company, sku="DRL-01", name="Hammer drill", product_type="GOODS",
            unit_of_measure=self.cement.unit_of_measure, selling_price=D("6500"),
            track_inventory=True)
        StockMovement.objects.create(
            product=self.drill, warehouse=self.warehouse, movement_type="OPENING",
            quantity=D("10"), unit_cost=D("4000"), movement_date=self.today,
            reference="OPEN-DRL")
        shelf = lambda: StockLevel.objects.get(product=self.drill, warehouse=self.warehouse).quantity

        before = self.entry_ids()
        inv = self.make_invoice(client, "FIX-10-DIRECT", [self.line(self.drill, "3", "6500")])
        if not self.expect("a direct invoice for 3 drills posted", inv and inv.journal_entry_id):
            return
        issue = StockMovement.objects.filter(product=self.drill, movement_type="DELIVERY",
                                             reference=inv.reference)
        self.expect("posting the direct invoice issued the 3 drills",
                    issue.count() == 1 and issue.first().quantity == D("3") and shelf() == D("7"),
                    f"{issue.count()} issue(s), shelf {shelf()}")
        self.expect("at average cost: Dr COGS 12,000 / Cr Inventory 12,000",
                    self.account_moved("5000", before, "debit") == D("12000.00")
                    and self.account_moved("1400", before, "credit") == D("12000.00"),
                    f"COGS {self.account_moved('5000', before, 'debit')} "
                    f"inventory {self.account_moved('1400', before, 'credit')}")

        count = Invoice.objects.filter(company=self.company).count()
        header = {"reference": "FIX-10-SHORT", "customer": self.customer.pk,
                  "invoice_date": str(self.today),
                  "due_date": str(self.today + datetime.timedelta(days=30)),
                  "currency": "KES", "notes": ""}
        self.refused(client, "a direct invoice for 50 drills with 7 on the shelf",
                     reverse("sales:invoice_create"),
                     merge(header, formset("lines", [self.line(self.drill, "50", "6500")])),
                     needle="not enough stock")
        self.expect("nothing was saved or issued for the short invoice",
                    Invoice.objects.filter(company=self.company).count() == count
                    and shelf() == D("7"), f"shelf {shelf()}")

        # The shelf's average moves before the return: 10 more at 5,000.
        StockMovement.objects.create(
            product=self.drill, warehouse=self.warehouse, movement_type="OPENING",
            quantity=D("7"), unit_cost=D("5000"), movement_date=self.today,
            reference="OPEN-DRL-2")
        saved = inv.lines.first()
        before = self.entry_ids()
        self.credit(client, inv, [(saved, "1")], returned=True, reason="one drill came back")
        note = Invoice.objects.filter(original_invoice=inv, status="CREDIT_NOTE").first()
        back = StockMovement.objects.filter(product=self.drill, movement_type="RETURN_IN")
        self.expect("a 'goods returned' note puts the drill back on the shelf",
                    note is not None and note.goods_returned and back.count() == 1
                    and shelf() == D("15"), f"{back.count()} return(s), shelf {shelf()}")
        self.expect("at its original issue cost: Dr Inventory 4,000 / Cr COGS 4,000",
                    back.exists() and back.first().unit_cost == D("4000")
                    and self.account_moved("1400", before, "debit") == D("4000.00")
                    and self.account_moved("5000", before, "credit") == D("4000.00"),
                    f"{back.first().unit_cost if back.exists() else '-'} / "
                    f"{self.account_moved('1400', before, 'debit')}")

        before = self.entry_ids()
        self.credit(client, inv, [(saved, "1")], returned=False, reason="price agreed down")
        self.expect("a price-only credit moves no stock and no cost",
                    back.count() == 1 and shelf() == D("15")
                    and self.account_moved("5000", before, "credit") == Z
                    and self.account_moved("1400", before, "debit") == Z,
                    f"shelf {shelf()}")
        self.expect("the credit-note form offers 'goods returned' on a stocked invoice",
                    'name="goods_returned"' in client.get(
                        reverse("sales:invoice_detail", args=[inv.pk])).content.decode())

        # An order's invoice: the delivery issued the goods, the invoice must not.
        order = self.make_order(client, "FIX-10-SO", [self.line(self.drill, "2", "6500")])
        if not order:
            return
        ol = order.lines.first()
        self.send(client, "deliver 2 drills", reverse("sales:order_deliver_record", args=[order.pk]),
                  {"warehouse": self.warehouse.pk, f"deliver_qty_{ol.pk}": "2"})
        level = shelf()
        draft = self.convert(client, order)
        if not self.expect("the delivered order converted", draft is not None):
            return
        client.post(reverse("sales:invoice_post", args=[draft.pk]), {})
        draft.refresh_from_db()
        self.expect("posting an invoice for delivered goods issues nothing more",
                    draft.journal_entry_id and shelf() == level
                    and not StockMovement.objects.filter(reference=draft.reference).exists(),
                    f"shelf {level} -> {shelf()}")
        before = self.entry_ids()
        self.credit(client, draft, [(draft.lines.first(), "1")], returned=True)
        self.expect("a return against the order's invoice comes back at the delivery's cost",
                    shelf() == level + 1
                    and self.account_moved("5000", before, "credit")
                    == q2(StockMovement.objects.filter(sales_order=order).first().unit_cost),
                    f"shelf {shelf()} COGS back {self.account_moved('5000', before, 'credit')}")

    def check_on_account_reports(self, client):
        from apps.crm.models import Customer
        self.kilimo = Customer.objects.create(company=self.company, name="Kilimo Supplies")
        response = self.pay(client, "FIX-11-ON-ACCT", "5000", customer=self.kilimo)
        self.expect("a receipt from a customer with nothing owed is taken on account",
                    response is not None and response.status_code == 302)
        control = self.ar_control()
        debtors = client.get(reverse("reporting:aged_debtors")).context
        self.expect("Aged Debtors shows the 5,000 on account on its own line",
                    debtors["unapplied_total"] == D("5000.00"), str(debtors["unapplied_total"]))
        self.expect("Aged Debtors' net total equals AR 1300",
                    debtors["net_total"] == control,
                    f"net {debtors['net_total']} vs 1300 {control}")
        ageing = client.get(reverse("sales:ar_aging")).context
        self.expect("the sales AR ageing nets to AR 1300 as well",
                    ageing["net_total"] == control and ageing["unapplied_total"] == D("5000.00"),
                    f"net {ageing['net_total']} vs 1300 {control}")
        home = client.get(reverse("sales:index")).context
        self.expect("the receivables home shows the net receivable",
                    home["net_receivable"] == control and home["unapplied_credit"] == D("5000.00"),
                    f"{home['net_receivable']} vs {control}")
        board = client.get(reverse("dashboard:index")).context
        self.expect("the dashboard's receivables equal AR 1300",
                    board["accounts_receivable"] == control,
                    f"{board['accounts_receivable']} vs {control}")
        statement = client.get(reverse("sales:customer_statement", args=[self.kilimo.pk])).context
        self.expect("Kilimo's statement shows 5,000 in its favour",
                    statement["closing_balance"] == D("-5000.00")
                    and statement["on_account"] == D("5000.00"),
                    f"{statement['closing_balance']} / {statement['on_account']}")

    def check_apply_credit(self, client):
        from apps.sales.models import CustomerPayment, Invoice
        payment = CustomerPayment.objects.get(company=self.company, reference="FIX-11-ON-ACCT")
        inv = self.make_invoice(client, "FIX-12-INV", [self.line(self.service, "1", "2500")],
                                customer=self.kilimo)
        url = reverse("sales:payment_apply", args=[payment.pk])
        page = client.get(url)
        self.expect("the apply-credit screen lists Kilimo's open invoice",
                    page.status_code == 200
                    and any(r["invoice"].pk == inv.pk for r in page.context["rows"])
                    and page.context["unapplied"] == D("5000.00"))
        other = self.make_invoice(client, "FIX-12-OTHER", [self.line(self.service, "1", "100")])
        for label, data, needle in (
                ("more than the invoice owes", {f"allocate_{inv.pk}": "3000"}, "only owed"),
                ("another customer's invoice", {f"allocate_{other.pk}": "100"}, "not one of"),
                ("an application dated before the invoice",
                 {f"allocate_{inv.pk}": "100",
                  "allocation_date": str(self.today - datetime.timedelta(days=3))}, "cannot")):
            response = self.send(client, f"apply credit: {label}", url, data)
            self.expect(f"applying {label} is refused",
                        response is not None and response.status_code == 200
                        and needle in (response.context.get("error") or ""),
                        "" if response is None else str(response.context.get("error")))
        inv.refresh_from_db()
        self.expect("the refusals applied nothing", inv.paid_amount == Z, str(inv.paid_amount))
        before = self.entry_ids()
        response = self.send(client, "apply 2,900 of credit", url, {
            f"allocate_{inv.pk}": "2900", "form_token": page.context["form_token"]})
        inv.refresh_from_db()
        payment.refresh_from_db()
        from apps.sales.receivables import unapplied_amount
        self.expect("the credit settles the invoice and 2,100 stays on account",
                    inv.status == "PAID" and unapplied_amount(payment) == D("2100.00"),
                    f"{inv.status} unapplied {unapplied_amount(payment)}")
        self.expect("at one rate nothing more is booked", self.entry_ids() == before)
        response = self.send(client, "the same apply form sent again", url, {
            f"allocate_{inv.pk}": "1", "form_token": page.context["form_token"]})
        self.expect("a second post of the same apply form is turned away",
                    response is not None and response.status_code == 302
                    and unapplied_amount(payment) == D("2100.00"))
        self.expect("Aged Debtors still nets to AR 1300 after applying",
                    client.get(reverse("reporting:aged_debtors")).context["net_total"]
                    == self.ar_control())

    def check_fx_settlements(self, client):
        """A second rate from today: 135.00, against 131.25 up to yesterday."""
        from apps.accounting.models import Currency, ExchangeRate
        from apps.crm.models import Customer
        from apps.sales.models import CustomerPayment, Invoice
        usd = Currency.objects.get(code="USD")
        new_rate = D("135.00")
        ExchangeRate.objects.create(company=self.company, currency=usd,
                                    effective_date=self.today, rate=new_rate, source="MANUAL")
        earlier = self.today - datetime.timedelta(days=3)

        # 11. an order taken at the old rate is invoiced at the invoice day's.
        from apps.sales.models import SalesOrder
        header = {"reference": "FIX-13-USD-SO", "customer": self.customer.pk,
                  "order_date": str(earlier), "delivery_date": "",
                  "warehouse": self.warehouse.pk, "sales_rep": "", "currency": "USD",
                  "discount_percent": "0", "branch": "", "notes": ""}
        self.accepted(client, "USD order dated three days ago", reverse("sales:order_create"),
                      merge(header, formset("lines", [self.line(self.service, "1", "100", tax=False)])))
        order = SalesOrder.objects.get(company=self.company, reference="FIX-13-USD-SO")
        client.post(reverse("sales:order_confirm", args=[order.pk]), {})
        order.refresh_from_db()
        draft = self.convert(client, order)
        self.expect("the order carries its own day's rate",
                    order.exchange_rate == USD_RATE, str(order.exchange_rate))
        self.expect("the invoice raised today takes today's rate, not the order's",
                    draft is not None and draft.exchange_rate == new_rate
                    and draft.total_amount_base == D("13500.00"),
                    f"{draft and draft.exchange_rate} {draft and draft.total_amount_base}")

        # 4. paid at a different rate: the statement closes at nil.
        delta = Customer.objects.create(company=self.company, name="Delta Exporters")
        data = {"reference": "FIX-13-USD-INV", "customer": delta.pk,
                "invoice_date": str(earlier), "due_date": str(self.today),
                "currency": "USD", "notes": ""}
        self.accepted(client, "USD invoice at 131.25", reverse("sales:invoice_create"),
                      merge(data, formset("lines", [self.line(self.service, "1", "100", tax=False)])))
        inv = Invoice.objects.get(company=self.company, reference="FIX-13-USD-INV")
        self.pay(client, "FIX-13-USD-PAY", "100", currency="USD", customer=delta,
                 allocations={f"allocate_{inv.pk}": "100"})
        inv.refresh_from_db()
        statement = client.get(reverse("sales:customer_statement", args=[delta.pk])).context
        self.expect("USD 100 raised at 131.25 and paid at 135.00 is PAID",
                    inv.status == "PAID", inv.status)
        self.expect("and the statement closes at nil, the gain being the company's",
                    statement["closing_balance"] == Z
                    and statement["closing_balance"] == statement["opening_balance"]
                    + statement["total_debit"] - statement["total_credit"],
                    str(statement["closing_balance"]))

        # 3 with FX: money on account at 131.25 applied to an invoice at 135.00.
        echo = Customer.objects.create(company=self.company, name="Echo Imports")
        data = {"reference": "FIX-13-USD-DEP", "customer": echo.pk,
                "payment_date": str(earlier), "amount": "100", "currency": "USD",
                "payment_method": "BANK_TRANSFER", "bank_account": "",
                "transaction_reference": "", "notes": "deposit"}
        self.accepted(client, "USD 100 deposit at 131.25", reverse("sales:payment_create"), data)
        deposit = CustomerPayment.objects.get(company=self.company, reference="FIX-13-USD-DEP")
        later = self.make_invoice(client, "FIX-13-USD-LATER",
                                  [self.line(self.service, "1", "100", tax=False)],
                                  currency="USD", customer=echo)
        self.expect("the deposit did not settle an invoice raised after it",
                    later is not None and later.paid_amount == Z
                    and not deposit.allocations.exists())
        before = self.entry_ids()
        self.send(client, "apply the USD deposit", reverse("sales:payment_apply", args=[deposit.pk]),
                  {f"allocate_{later.pk}": "100", "allocation_date": str(self.today)})
        later.refresh_from_db()
        self.expect("the deposit settles the later invoice",
                    later.status == "PAID", later.status)
        self.expect("the rate gap is realised: Dr 7300 375.00",
                    self.account_moved("7300", before, "debit") == D("375.00")
                    and self.account_moved("1300", before, "credit")
                    - self.account_moved("1300", before, "debit") == D("375.00"),
                    f"7300 {self.account_moved('7300', before, 'debit')}")
        statement = client.get(reverse("sales:customer_statement", args=[echo.pk])).context
        self.expect("Echo's statement closes at nil", statement["closing_balance"] == Z,
                    str(statement["closing_balance"]))
        self.expect("Aged Debtors nets to AR 1300 after FX settlements",
                    client.get(reverse("reporting:aged_debtors")).context["net_total"]
                    == self.ar_control(),
                    f"{client.get(reverse('reporting:aged_debtors')).context['net_total']} "
                    f"vs {self.ar_control()}")

        # The deposit given back: the application's exchange entry goes too.
        client.post(reverse("sales:payment_reverse", args=[deposit.pk]), {"reason": "bounced"})
        later.refresh_from_db()
        self.expect("reversing the deposit reopens the invoice and undoes the exchange",
                    later.status == "SENT"
                    and self.account_moved("7300", before, "debit")
                    == self.account_moved("7300", before, "credit"),
                    f"{later.status}")

    def check_sales_chart(self, client):
        from apps.accounting.models import JournalEntryLine
        month = self.today.replace(day=1)
        lines = JournalEntryLine.objects.filter(
            journal_entry__company=self.company, journal_entry__status="POSTED",
            account__account_type="REVENUE", journal_entry__posting_date__gte=month,
            journal_entry__posting_date__lte=self.today).exclude(
            journal_entry__entry_type="CLOSING")
        agg = lines.aggregate(dr=Sum("debit_amount"), cr=Sum("credit_amount"))
        net = (agg["cr"] or Z) - (agg["dr"] or Z)
        data = client.get(reverse("dashboard:sales_chart")).json()["datasets"][0]["data"]
        self.expect("this month's chart bar is net revenue, credit notes taken off",
                    agg["dr"] and D(str(data[-1])) == net,
                    f"chart {data[-1]} vs net {net} (gross {agg['cr']})")

    def check_double_submits(self, client):
        from apps.sales.models import CustomerPayment, Invoice
        five = self.make_invoice(client, "FIX-14-FIVE", [self.line(self.cement, "5", "850")])
        saved = five.lines.first()
        token = client.get(reverse("sales:invoice_detail", args=[five.pk])).context["credit_token"]
        self.credit(client, five, [(saved, "2")], token=token)
        response = self.credit(client, five, [(saved, "2")], token=token)
        notes = Invoice.objects.filter(original_invoice=five).count()
        self.expect("2 of 5 sent twice with the same form makes one credit note",
                    notes == 1, f"{notes} notes")
        self.expect("the second copy is told it was already sent",
                    any("already sent" in m for m in self.flashes(client, response)))
        self.credit(client, five, [(saved, "2")], token="made-up")
        self.expect("an unknown ticket is refused too",
                    Invoice.objects.filter(original_invoice=five).count() == 1)

        form = client.get(reverse("sales:payment_create")).context
        self.expect("the receipt form carries a ticket",
                    bool(form.get("form_token")))
        receipts = CustomerPayment.objects.filter(company=self.company).count()
        data = {"reference": "", "customer": self.customer.pk,
                "payment_date": str(self.today), "amount": "100", "currency": "KES",
                "payment_method": "CASH", "bank_account": "", "transaction_reference": "",
                "notes": "", "form_token": form["form_token"]}
        self.accepted(client, "receipt with blank number", reverse("sales:payment_create"), data)
        self.send(client, "the same receipt again", reverse("sales:payment_create"), data)
        self.expect("a blank-numbered receipt sent twice books once",
                    CustomerPayment.objects.filter(company=self.company).count() == receipts + 1,
                    str(CustomerPayment.objects.filter(company=self.company).count() - receipts))

        form = client.get(reverse("sales:invoice_create")).context
        count = Invoice.objects.filter(company=self.company).count()
        header = {"reference": "", "customer": self.customer.pk,
                  "invoice_date": str(self.today),
                  "due_date": str(self.today + datetime.timedelta(days=30)),
                  "currency": "KES", "notes": "", "form_token": form["form_token"]}
        data = merge(header, formset("lines", [self.line(self.service, "1", "100")]))
        self.accepted(client, "invoice with blank number", reverse("sales:invoice_create"), data)
        self.send(client, "the same invoice again", reverse("sales:invoice_create"), data)
        self.expect("an invoice form sent twice makes one invoice",
                    Invoice.objects.filter(company=self.company).count() == count + 1)

    def check_dates(self, client):
        from apps.sales.models import Invoice
        header = {"reference": "FIX-15-DUE", "customer": self.customer.pk,
                  "invoice_date": str(self.today),
                  "due_date": str(self.today - datetime.timedelta(days=5)),
                  "currency": "KES", "notes": ""}
        self.refused(client, "a due date before the invoice date", reverse("sales:invoice_create"),
                     merge(header, formset("lines", [self.line(self.service)])), needle="due date")
        inv = self.make_invoice(client, "FIX-15-INV", [self.line(self.service, "1", "1000")])
        data = {"reference": "FIX-15-EARLY", "customer": self.customer.pk,
                "payment_date": str(self.today - datetime.timedelta(days=19)), "amount": "100",
                "currency": "KES", "payment_method": "CASH", "bank_account": "",
                "transaction_reference": "", "notes": "", f"allocate_{inv.pk}": "100"}
        response = self.send(client, "payment dated before its invoice",
                             reverse("sales:payment_create"), data)
        inv.refresh_from_db()
        self.expect("a payment dated before the invoice it settles is refused",
                    response is not None and response.status_code == 200
                    and any("raised after" in e for e in self.errors(response))
                    and inv.paid_amount == Z, str(self.errors(response) if response else ""))

    def check_messages(self, client):
        from apps.sales.models import SalesOrder
        header = {"reference": "FIX-16-SO", "customer": self.customer.pk,
                  "order_date": str(self.today), "delivery_date": "",
                  "warehouse": self.warehouse.pk, "sales_rep": "", "currency": "KES",
                  "discount_percent": "2.5", "branch": "", "notes": ""}
        response = self.send(client, "order with a discount", reverse("sales:order_create"),
                             merge(header, formset("lines", [self.line(self.cement, "100", "850", "5")])))
        shown = self.flashes(client, response)
        import re
        self.expect("the order's total is printed to the cent",
                    any("created" in m and re.search(r"total \d+\.\d{2} KES", m) for m in shown),
                    str(shown))
        inv = self.make_invoice(client, "FIX-16-CANCEL", [self.line(self.service, "1", "100")])
        client.post(reverse("sales:invoice_cancel", args=[inv.pk]), {})
        response = self.credit(client, inv, [(inv.lines.first(), "1")])
        shown = self.flashes(client, response)
        self.expect("crediting a cancelled invoice says what it is",
                    any("cancelled invoice" in m for m in shown), str(shown))

    def check_order_cancel(self, client):
        order = self.make_order(client, "FIX-17-SO", [self.line(self.service, "2", "100")])
        if not order:
            return
        self.send(client, "cancel the order", reverse("sales:order_cancel", args=[order.pk]), {})
        order.refresh_from_db()
        self.expect("an untouched order can be cancelled", order.status == "CANCELLED", order.status)
        before = order.invoices.count()
        response = client.post(reverse("sales:order_invoice", args=[order.pk]), {})
        self.expect("a cancelled order cannot be invoiced", order.invoices.count() == before)
        client.post(reverse("sales:order_confirm", args=[order.pk]), {})
        order.refresh_from_db()
        self.expect("nor confirmed again", order.status == "CANCELLED", order.status)

        billed = self.make_order(client, "FIX-17-SO-B", [self.line(self.service, "2", "100")])
        draft = self.convert(client, billed)
        response = self.send(client, "cancel an invoiced order",
                             reverse("sales:order_cancel", args=[billed.pk]), {})
        billed.refresh_from_db()
        self.expect("an order with a live invoice is not cancelled",
                    billed.status != "CANCELLED"
                    and any("invoiced" in m for m in self.flashes(client, response)),
                    billed.status)
        client.post(reverse("sales:invoice_cancel", args=[draft.pk]), {})
        self.send(client, "cancel once the invoice is cancelled",
                  reverse("sales:order_cancel", args=[billed.pk]), {})
        billed.refresh_from_db()
        self.expect("once its invoice is cancelled the order can be", billed.status == "CANCELLED")

        shipped = self.make_order(client, "FIX-17-SO-C", [self.line(self.service, "2", "100")])
        line = shipped.lines.first()
        self.send(client, "deliver one", reverse("sales:order_deliver_record", args=[shipped.pk]),
                  {"warehouse": self.warehouse.pk, f"deliver_qty_{line.pk}": "1"})
        self.send(client, "cancel a delivered order",
                  reverse("sales:order_cancel", args=[shipped.pk]), {})
        shipped.refresh_from_db()
        self.expect("an order with goods delivered is not cancelled",
                    shipped.status == "PARTIALLY_DELIVERED", shipped.status)

    def check_separation_of_duties(self, client):
        from apps.organizations.models import CompanyMembership
        membership = CompanyMembership.objects.get(company=self.company, user=self.user)
        try:
            membership.role = "SALES_REP"
            membership.save(update_fields=["role"])
            inv = self.make_invoice(client, "FIX-18-REP", [self.line(self.service, "1", "100")])
            self.expect("a sales rep's invoice is left as a draft for finance",
                        inv is not None and inv.status == "DRAFT" and not inv.journal_entry_id,
                        inv and inv.status)
            membership.role = "FINANCE_MANAGER"
            membership.save(update_fields=["role"])
            inv = self.make_invoice(client, "FIX-18-FM", [self.line(self.service, "1", "100")])
            self.expect("a finance manager's invoice posts on create",
                        inv is not None and inv.journal_entry_id, inv and inv.status)
        finally:
            membership.role = "ADMIN"
            membership.save(update_fields=["role"])
