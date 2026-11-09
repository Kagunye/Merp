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
  20. reversing a receipt applied to one invoice in slices takes back all of it
  21. cancelling a posted direct invoice puts its goods back and its cost of
      sales with them
  22. goods-returned notes on several invoices of one order return no more
      than the order delivered
  23. an order's draft cannot carry a stocked product the order does not have
  24. a statement with a date range values a receipt as at that range
  25. a delivery or credit of 0.00001 is refused
  26. a credit note is dated as chosen, and never before its invoice
  27. a zero-value invoice for stocked goods posts its stock issue
  28. an order's draft cannot bill more than was ordered
  29. the dashboard chart's expenses include cost of goods, and skip the future
  30. a cancelled invoice or a credit note cannot be posted
  31. Post, Cancel and Reverse are hidden from roles that may not use them
   32. an INV- number typed by hand is never issued again; stock follows the
       invoice, not its number
   33. an order's draft keeps the order's customer and currency
   34. a foreign invoice paid in slices leaves nothing in AR
   35. a receipt or invoice whose posting fails is not kept
   36. a delivery form sent twice delivers once
   37. a line too large for the books is refused, not a 500
   38. order numbers: optional, unique, one order per form
   39. credit notes are not dated ahead; credit is not applied in a closed month
   40. an order whose invoice was credited in full can be billed again or cancelled
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
        self.section("20. Reversing a receipt takes back every slice it settled")
        self.check_reverse_sliced_credit(client)
        self.section("21. Cancelling a direct stock invoice brings the goods back")
        self.check_cancel_restocks(client)
        self.section("22. Returns on an order's invoices are capped by its deliveries")
        self.check_order_return_cap(client)
        self.section("23. An order's draft cannot carry stocked goods the order never had")
        self.check_order_draft_stray_stock(client)
        self.section("24. A dated statement values receipts as at its window")
        self.check_dated_statement(client)
        self.section("25. Quantities finer than four places are refused")
        self.check_fine_quantities(client)
        self.section("26. A credit note takes the date it is given, never before its invoice")
        self.check_credit_note_date(client)
        self.section("27. A zero-value invoice still issues its goods")
        self.check_zero_value_invoice(client)
        self.section("28. An order's draft bills no more than was ordered")
        self.check_order_draft_cap(client)
        self.section("29. The dashboard chart's expenses include cost of goods")
        self.check_chart_expenses(client)
        self.section("30. A cancelled invoice or a credit note cannot be posted")
        self.check_cancelled_not_posted(client)
        self.section("31. Buttons a role cannot use are not shown")
        self.check_hidden_buttons(client)
        self.section("32. Invoice numbers are issued once; stock follows the invoice")
        self.check_invoice_numbers(client)
        self.section("33. An order's draft keeps the order's customer and currency")
        self.check_order_draft_party(client)
        self.section("34. A USD invoice paid in two slices leaves nothing in AR")
        self.check_sliced_fx_settlement(client)
        self.section("35. A document whose posting fails is not kept")
        self.check_failed_posting_rolls_back(client)
        self.section("36. A delivery form sent twice delivers once")
        self.check_delivery_token(client)
        self.section("37. A line too large for the books is refused")
        self.check_overflow(client)
        self.section("38. Order numbers: optional, unique, one order per form")
        self.check_order_numbers(client)
        self.section("39. Credit-note dates and credit applied in a closed month")
        self.check_round4_dates(client)
        self.section("40. A fully credited order's invoice no longer bills it")
        self.check_fully_credited_order(client)

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

    def direct_draft(self, client, reference, lines, currency="KES"):
        """A direct invoice left as a draft: raised by a sales rep, for finance.

        Section 2 used to switch the currency of a draft raised from an order.
        An order's draft now keeps the order's customer and currency (section
        33), so the re-stamping is checked on a direct draft instead.
        """
        self.as_role("SALES_REP")
        try:
            return self.make_invoice(client, reference, lines, currency=currency)
        finally:
            self.as_role("ADMIN")

    def check_draft_currency_restamped(self, client):
        draft = self.direct_draft(client, "FIX-2-DRAFT",
                                  [self.line(self.service, "1", "100", tax=False)])
        if not self.expect("a KES direct draft", draft and draft.status == "DRAFT"):
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
        draft = self.direct_draft(client, "FIX-2-DRAFT-B",
                                  [self.line(self.service, "1", "100", tax=False)],
                                  currency="USD")
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

    # ──────────────────────────── round-three fixes ──────────────────────────

    def stocked(self, sku, name, price, on_hand, cost):
        """A stocked product with opening stock on the default warehouse."""
        from apps.inventory.models import Product, StockMovement
        product = Product.objects.create(
            company=self.company, sku=sku, name=name, product_type="GOODS",
            unit_of_measure=self.cement.unit_of_measure, selling_price=D(price),
            track_inventory=True)
        StockMovement.objects.create(
            product=product, warehouse=self.warehouse, movement_type="OPENING",
            quantity=D(on_hand), unit_cost=D(cost), movement_date=self.today,
            reference=f"OPEN-{sku}")
        return product

    def shelf(self, product):
        from apps.inventory.models import StockLevel
        level = StockLevel.objects.filter(product=product, warehouse=self.warehouse).first()
        return level.quantity if level else Z

    def net_moved(self, code, since_ids):
        """Debits less credits on an account from entries not in `since_ids`."""
        return (self.account_moved(code, since_ids, "debit")
                - self.account_moved(code, since_ids, "credit"))

    def as_role(self, role):
        from apps.organizations.models import CompanyMembership
        membership = CompanyMembership.objects.get(company=self.company, user=self.user)
        membership.role = role
        membership.save(update_fields=["role"])

    def apply(self, client, payment, invoice, amount, on_date=None):
        return self.send(client, f"apply {amount} of {payment.reference}",
                         reverse("sales:payment_apply", args=[payment.pk]),
                         {f"allocate_{invoice.pk}": amount,
                          "allocation_date": str(on_date or self.today)})

    def check_reverse_sliced_credit(self, client):
        from apps.crm.models import Customer
        from apps.sales.models import CustomerPayment
        # KES: 5,000 on account, applied 2,000 then 3,000 to one 6,000 invoice.
        slice_co = Customer.objects.create(company=self.company, name="Slice Traders")
        self.pay(client, "FIX-20-PAY", "5000", customer=slice_co)
        payment = CustomerPayment.objects.get(company=self.company, reference="FIX-20-PAY")
        inv = self.make_invoice(client, "FIX-20-INV", [self.line(self.service, "1", "6000", tax=False)],
                                customer=slice_co)
        self.apply(client, payment, inv, "2000")
        self.apply(client, payment, inv, "3000")
        inv.refresh_from_db()
        self.expect("two slices of credit settle 5,000 of the invoice",
                    inv.paid_amount == D("5000.00") and payment.allocations.count() == 2,
                    f"paid {inv.paid_amount}")
        response = self.send(client, "reverse the sliced receipt",
                             reverse("sales:payment_reverse", args=[payment.pk]), {"reason": "bounced"})
        inv.refresh_from_db()
        self.expect("reversing it takes back both slices: the invoice is owed in full",
                    inv.paid_amount == Z and inv.outstanding_amount == D("6000.00")
                    and inv.status == "SENT",
                    f"{inv.status} paid {inv.paid_amount} out {inv.outstanding_amount}")
        self.expect("the message counts one invoice put back",
                    any("1 invoice(s)" in m for m in self.flashes(client, response)))
        self.expect("Aged Debtors still nets to AR 1300 after the reversal",
                    client.get(reverse("reporting:aged_debtors")).context["net_total"]
                    == self.ar_control())

        # USD: a deposit at 131.25 applied in two slices to an invoice at 135.00,
        # each slice booking its own realised exchange.
        slice_usd = Customer.objects.create(company=self.company, name="Slice Exporters")
        earlier = self.today - datetime.timedelta(days=3)
        self.accepted(client, "USD 500 deposit at 131.25", reverse("sales:payment_create"), {
            "reference": "FIX-20-USD-PAY", "customer": slice_usd.pk,
            "payment_date": str(earlier), "amount": "500", "currency": "USD",
            "payment_method": "BANK_TRANSFER", "bank_account": "",
            "transaction_reference": "", "notes": ""})
        deposit = CustomerPayment.objects.get(company=self.company, reference="FIX-20-USD-PAY")
        usd_inv = self.make_invoice(client, "FIX-20-USD-INV",
                                    [self.line(self.service, "1", "600", tax=False)],
                                    currency="USD", customer=slice_usd)
        before = self.entry_ids()
        self.apply(client, deposit, usd_inv, "200")
        self.apply(client, deposit, usd_inv, "300")
        usd_inv.refresh_from_db()
        self.expect("two USD slices settle USD 500 and book exchange",
                    usd_inv.paid_amount == D("500.00")
                    and self.account_moved("7300", before, "debit") > Z,
                    f"paid {usd_inv.paid_amount}")
        self.send(client, "reverse the USD deposit",
                  reverse("sales:payment_reverse", args=[deposit.pk]), {"reason": "recalled"})
        usd_inv.refresh_from_db()
        self.expect("reversing the USD deposit leaves the invoice owed in full",
                    usd_inv.paid_amount == Z and usd_inv.outstanding_amount == D("600.00")
                    and usd_inv.status == "SENT",
                    f"{usd_inv.status} paid {usd_inv.paid_amount}")
        self.expect("and both slices' exchange is undone",
                    self.net_moved("7300", before) == Z and self.net_moved("4310", before) == Z,
                    f"7300 {self.net_moved('7300', before)}")
        self.expect("Aged Debtors nets to AR 1300 after the USD reversal",
                    client.get(reverse("reporting:aged_debtors")).context["net_total"]
                    == self.ar_control())

    def check_cancel_restocks(self, client):
        from apps.inventory.models import StockMovement
        sheet = self.stocked("SHT-28", "Iron sheet 28g", "1100", "50", "700")
        before = self.entry_ids()
        inv = self.make_invoice(client, "FIX-21-INV", [self.line(sheet, "20", "1100", tax=False)])
        self.expect("the direct invoice issued 20 sheets at 700",
                    self.shelf(sheet) == D("30") and self.net_moved("5000", before) == D("14000.00"),
                    f"shelf {self.shelf(sheet)} COGS {self.net_moved('5000', before)}")
        response = self.send(client, "cancel the posted stock invoice",
                             reverse("sales:invoice_cancel", args=[inv.pk]), {})
        inv.refresh_from_db()
        self.expect("cancelling it puts the 20 sheets back on the shelf",
                    inv.status == "CANCELLED" and self.shelf(sheet) == D("50"),
                    f"{inv.status} shelf {self.shelf(sheet)}")
        back = StockMovement.objects.filter(product=sheet, movement_type="RETURN_IN",
                                            reference=inv.reference).first()
        self.expect("at the cost they left at, dated with the invoice",
                    back is not None and back.unit_cost == D("700")
                    and back.movement_date == inv.invoice_date)
        self.expect("cost of sales, inventory and revenue all net to nothing",
                    self.net_moved("5000", before) == Z and self.net_moved("1400", before) == Z
                    and self.net_moved("1300", before) == Z
                    and self.revenue_moved(before) == Z,
                    f"5000 {self.net_moved('5000', before)} 1400 {self.net_moved('1400', before)}")
        self.expect("the message says the goods are back",
                    any("back on the shelf" in m for m in self.flashes(client, response)))

    def revenue_moved(self, since_ids):
        """Net movement on every revenue account from entries not in `since_ids`."""
        from apps.accounting.models import JournalEntryLine
        agg = JournalEntryLine.objects.filter(
            journal_entry__company=self.company, journal_entry__status="POSTED",
            account__account_type="REVENUE").exclude(journal_entry_id__in=since_ids).aggregate(
            dr=Sum("debit_amount"), cr=Sum("credit_amount"))
        return (agg["dr"] or Z) - (agg["cr"] or Z)

    def check_order_return_cap(self, client):
        from apps.sales.models import Invoice
        maize = self.stocked("MZE-90", "Maize 90kg", "1500", "100", "1200")
        order = self.make_order(client, "FIX-22-SO", [self.line(maize, "10", "1500", tax=False)])
        if not order:
            return
        ol = order.lines.first()
        self.send(client, "deliver 5 of 10", reverse("sales:order_deliver_record", args=[order.pk]),
                  {"warehouse": self.warehouse.pk, f"deliver_qty_{ol.pk}": "5"})
        first = self.convert(client, order)
        saved = first.lines.first()
        self.accepted(client, "cut the first draft to 5",
                      reverse("sales:invoice_edit", args=[first.pk]),
                      self.edit_data(first, [(saved, {"quantity": "5"})]))
        second = self.convert(client, order)
        if not self.expect("two invoices of 5 each on one order", first and second):
            return
        for inv in (first, second):
            client.post(reverse("sales:invoice_post", args=[inv.pk]), {})
            inv.refresh_from_db()
        level = self.shelf(maize)
        self.credit(client, first, [(first.lines.first(), "5")], returned=True)
        self.expect("the first invoice's 5 come back", self.shelf(maize) == level + 5,
                    f"shelf {level} -> {self.shelf(maize)}")
        response = self.credit(client, second, [(second.lines.first(), "5")], returned=True)
        self.expect("the second invoice cannot bring back 5 more: only 5 were delivered",
                    self.shelf(maize) == level + 5
                    and not Invoice.objects.filter(original_invoice=second).exists(),
                    f"shelf {self.shelf(maize)}")
        self.expect("the refusal names the order's deliveries",
                    any("order FIX-22-SO" in m for m in self.flashes(client, response)))

    def check_order_draft_stray_stock(self, client):
        from apps.accounting.gl_service import post_invoice
        from apps.sales.models import InvoiceLine
        nails = self.stocked("NL-4", "Nails 4in", "300", "40", "150")
        order = self.make_order(client, "FIX-23-SO", [self.line(self.service, "1", "2500")])
        draft = self.convert(client, order) if order else None
        if not self.expect("a draft raised from a service order", draft is not None):
            return
        saved = draft.lines.first()
        data = self.edit_data(draft, [(saved, {})])
        data["lines-TOTAL_FORMS"] = "2"
        for key, value in {"id": "", "invoice": draft.pk, "product": nails.pk,
                           "description": "Nails", "quantity": "10", "unit_price": "300",
                           "discount_percent": "0", "tax_rate": ""}.items():
            data[f"lines-1-{key}"] = str(value)
        self.refused(client, "adding stocked nails to the order's draft",
                     reverse("sales:invoice_edit", args=[draft.pk]), data, needle="not on order")
        self.expect("the draft still has its one line", draft.lines.count() == 1)
        data["lines-1-product"] = ""
        data["lines-1-description"] = "Carriage"
        self.accepted(client, "an unstocked charge added to the order's draft",
                      reverse("sales:invoice_edit", args=[draft.pk]), data)
        # A draft written some other way still cannot post the stray goods.
        stray = InvoiceLine.objects.create(invoice=draft, product=nails, description="Nails",
                                           quantity=D("10"), unit_price=D("300"))
        response = self.send(client, "post the draft carrying stray nails",
                             reverse("sales:invoice_post", args=[draft.pk]), {})
        draft.refresh_from_db()
        self.expect("posting is refused and nothing leaves the shelf",
                    draft.status == "DRAFT" and not draft.journal_entry_id
                    and self.shelf(nails) == D("40")
                    and any("does not carry" in m for m in self.flashes(client, response)),
                    f"{draft.status} shelf {self.shelf(nails)}")
        stray.delete()
        try:
            post_invoice(draft, user=self.user)
            ok = draft.journal_entry_id is not None
        except Exception as exc:
            ok = False
        self.expect("without the stray line the draft posts", ok)

    def check_dated_statement(self, client):
        from apps.crm.models import Customer
        from apps.sales.models import CustomerPayment
        acme = Customer.objects.create(company=self.company, name="Acme Dated")
        earlier = self.today - datetime.timedelta(days=3)
        yesterday = self.today - datetime.timedelta(days=1)
        self.accepted(client, "USD 500 on account at 131.25", reverse("sales:payment_create"), {
            "reference": "FIX-24-PAY", "customer": acme.pk, "payment_date": str(earlier),
            "amount": "500", "currency": "USD", "payment_method": "BANK_TRANSFER",
            "bank_account": "", "transaction_reference": "", "notes": ""})
        payment = CustomerPayment.objects.get(company=self.company, reference="FIX-24-PAY")
        inv = self.make_invoice(client, "FIX-24-INV", [self.line(self.service, "1", "464", tax=False)],
                                currency="USD", customer=acme)
        self.apply(client, payment, inv, "464")
        url = reverse("sales:customer_statement", args=[acme.pk])
        upto = client.get(url, {"to": str(yesterday)}).context
        self.expect("to yesterday the receipt stands whole at its own rate (65,625.00)",
                    upto["closing_balance"] == D("-65625.00")
                    and [r["credit"] for r in upto["rows"]] == [D("65625.00")],
                    f"{upto['closing_balance']} {[r['credit'] for r in upto['rows']]}")
        since = client.get(url, {"from": str(self.today)}).context
        self.expect("from today: opens at -65,625.00 and closes at what is on account",
                    since["opening_balance"] == D("-65625.00")
                    and since["closing_balance"] == D("-4725.00")
                    and since["closing_balance"] == since["opening_balance"]
                    + since["total_debit"] - since["total_credit"],
                    f"{since['opening_balance']} -> {since['closing_balance']}")
        self.expect("the exchange on applying the credit is its own line",
                    any(r["type"] == "Credit applied" and r["credit"] == D("1740.00")
                        for r in since["rows"]), str([(r["type"], r["credit"]) for r in since["rows"]]))
        whole = client.get(url).context
        self.expect("undated, the statement closes at the same -4,725.00",
                    whole["closing_balance"] == D("-4725.00"), str(whole["closing_balance"]))

    def check_fine_quantities(self, client):
        from apps.inventory.models import StockMovement
        grit = self.stocked("GRT-1", "Grit bag", "200", "10", "100")
        order = self.make_order(client, "FIX-25-SO", [self.line(grit, "5", "200")])
        if not order:
            return
        ol = order.lines.first()
        response = self.send(client, "deliver 0.00001", reverse("sales:order_deliver_record",
                                                              args=[order.pk]),
                             {"warehouse": self.warehouse.pk, f"deliver_qty_{ol.pk}": "0.00001"})
        ol.refresh_from_db()
        self.expect("a delivery of 0.00001 is refused and moves nothing",
                    ol.delivered_quantity == Z
                    and not StockMovement.objects.filter(sales_order=order).exists()
                    and any("four decimal places" in m for m in self.flashes(client, response)),
                    f"delivered {ol.delivered_quantity}")
        self.send(client, "deliver 0.0001", reverse("sales:order_deliver_record", args=[order.pk]),
                  {"warehouse": self.warehouse.pk, f"deliver_qty_{ol.pk}": "0.0001"})
        ol.refresh_from_db()
        self.expect("0.0001, the finest a quantity holds, is delivered",
                    ol.delivered_quantity == D("0.0001"), str(ol.delivered_quantity))

    def check_credit_note_date(self, client):
        from apps.sales.models import Invoice
        ahead = self.today + datetime.timedelta(days=10)
        data = {"reference": "FIX-26-INV", "customer": self.customer.pk,
                "invoice_date": str(ahead), "due_date": str(ahead + datetime.timedelta(days=30)),
                "currency": "KES", "notes": ""}
        self.accepted(client, "an invoice dated ten days ahead", reverse("sales:invoice_create"),
                      merge(data, formset("lines", [self.line(self.service, "2", "1000")])))
        inv = Invoice.objects.get(company=self.company, reference="FIX-26-INV")
        line = inv.lines.first()
        response = self.credit(client, inv, [(line, "1")])
        self.expect("a credit note left on today's date is refused before its invoice",
                    not Invoice.objects.filter(original_invoice=inv).exists()
                    and any("cannot come before" in m for m in self.flashes(client, response)))
        url = reverse("sales:invoice_credit_note", args=[inv.pk])
        response = self.send(client, "credit dated a day after the invoice, still ahead", url,
                             {f"credit_qty_{line.pk}": "1", "reason": "dated",
                              "credit_date": str(ahead + datetime.timedelta(days=1))})
        self.expect("a credit note dated after today is refused",
                    not Invoice.objects.filter(original_invoice=inv).exists()
                    and any("still to come" in m for m in self.flashes(client, response)))
        past = self.today - datetime.timedelta(days=10)
        data = {"reference": "FIX-26-PAST", "customer": self.customer.pk,
                "invoice_date": str(past), "due_date": str(past + datetime.timedelta(days=30)),
                "currency": "KES", "notes": ""}
        self.accepted(client, "an invoice dated ten days ago", reverse("sales:invoice_create"),
                      merge(data, formset("lines", [self.line(self.service, "2", "1000")])))
        older = Invoice.objects.get(company=self.company, reference="FIX-26-PAST")
        self.send(client, "credit dated a day after that invoice",
                  reverse("sales:invoice_credit_note", args=[older.pk]),
                  {f"credit_qty_{older.lines.first().pk}": "1", "reason": "dated",
                   "credit_date": str(past + datetime.timedelta(days=1))})
        note = Invoice.objects.filter(original_invoice=older).first()
        self.expect("the note takes the chosen date, and so does its entry",
                    note is not None and note.invoice_date == past + datetime.timedelta(days=1)
                    and note.journal_entry.posting_date == note.invoice_date,
                    f"{note and note.invoice_date}")
        page = client.get(reverse("sales:invoice_detail", args=[inv.pk])).content.decode()
        self.expect("the credit form offers a date box opening on the invoice's date",
                    'name="credit_date"' in page and f'value="{ahead:%Y-%m-%d}"' in page)

    def check_zero_value_invoice(self, client):
        sample = self.stocked("SMP-1", "Sample pack", "850", "10", "300")
        before = self.entry_ids()
        inv = self.make_invoice(client, "FIX-27-FREE", [self.line(sample, "5", "850", disc="100")])
        self.expect("a 100%-discount invoice for 5 samples is posted, settled, with no revenue entry",
                    inv is not None and inv.status == "PAID" and not inv.journal_entry_id
                    and inv.total_amount == Z, f"{inv and inv.status}")
        self.expect("the 5 samples left the shelf: Dr COGS 1,500 / Cr Inventory 1,500",
                    self.shelf(sample) == D("5") and self.net_moved("5000", before) == D("1500.00")
                    and self.net_moved("1400", before) == D("-1500.00")
                    and self.net_moved("1300", before) == Z,
                    f"shelf {self.shelf(sample)} COGS {self.net_moved('5000', before)}")
        self.send(client, "post the free invoice again",
                  reverse("sales:invoice_post", args=[inv.pk]), {})
        self.expect("posting it again issues nothing more", self.shelf(sample) == D("5"))
        self.send(client, "cancel the free invoice",
                  reverse("sales:invoice_cancel", args=[inv.pk]), {})
        inv.refresh_from_db()
        self.expect("cancelling it brings the samples back and their cost off",
                    inv.status == "CANCELLED" and self.shelf(sample) == D("10")
                    and self.net_moved("5000", before) == Z, f"{inv.status} {self.shelf(sample)}")

    def check_order_draft_cap(self, client):
        order = self.make_order(client, "FIX-28-SO", [self.line(self.service, "2", "2500")])
        draft = self.convert(client, order) if order else None
        if not self.expect("a draft for 2 raised from the order", draft is not None):
            return
        saved = draft.lines.first()
        url = reverse("sales:invoice_edit", args=[draft.pk])
        self.refused(client, "billing 20 of 2 ordered", url,
                     self.edit_data(draft, [(saved, {"quantity": "20"})]),
                     needle="cannot bill 20 of installation")
        saved.refresh_from_db()
        order.refresh_from_db()
        self.expect("the draft and the order still say 2",
                    saved.quantity == D("2") and order.lines.first().invoiced_quantity == D("2"))
        self.accepted(client, "billing 1 of 2", url,
                      self.edit_data(draft, [(saved, {"quantity": "1"})]))
        second = self.convert(client, order)
        if not self.expect("the other 1 goes on a second draft", second is not None):
            return
        self.refused(client, "the second draft billing 2 when 1 is billed already",
                     reverse("sales:invoice_edit", args=[second.pk]),
                     self.edit_data(second, [(second.lines.first(), {"quantity": "2"})]),
                     needle="already bill")

    def check_chart_expenses(self, client):
        from apps.accounting.models import JournalEntryLine
        month = self.today.replace(day=1)

        def net(types, sign):
            agg = JournalEntryLine.objects.filter(
                journal_entry__company=self.company, journal_entry__status="POSTED",
                account__account_type__in=types, journal_entry__posting_date__gte=month,
                journal_entry__posting_date__lte=self.today).exclude(
                journal_entry__entry_type="CLOSING").aggregate(
                dr=Sum("debit_amount"), cr=Sum("credit_amount"))
            return sign * ((agg["dr"] or Z) - (agg["cr"] or Z))

        tomorrow = self.today + datetime.timedelta(days=1)
        if tomorrow.month == self.today.month:
            data = {"reference": "FIX-29-AHEAD", "customer": self.customer.pk,
                    "invoice_date": str(tomorrow), "due_date": str(tomorrow),
                    "currency": "KES", "notes": ""}
            self.accepted(client, "an invoice dated tomorrow", reverse("sales:invoice_create"),
                          merge(data, formset("lines", [self.line(self.service, "1", "9999")])))
        chart = client.get(reverse("dashboard:sales_chart")).json()["datasets"]
        expenses = net(["EXPENSE", "COST_OF_GOODS"], 1)
        self.expect("this month's expense bar includes cost of goods",
                    net(["COST_OF_GOODS"], 1) > Z and D(str(chart[1]["data"][-1])) == expenses,
                    f"chart {chart[1]['data'][-1]} vs {expenses}")
        self.expect("and the revenue bar leaves out entries dated after today",
                    D(str(chart[0]["data"][-1])) == net(["REVENUE"], -1),
                    f"chart {chart[0]['data'][-1]} vs {net(['REVENUE'], -1)}")

    def check_cancelled_not_posted(self, client):
        from apps.accounting.gl_service import post_invoice
        from django.core.exceptions import ValidationError
        order = self.make_order(client, "FIX-30-SO", [self.line(self.service, "1", "500")])
        draft = self.convert(client, order) if order else None
        if not self.expect("a draft to cancel", draft is not None):
            return
        self.send(client, "cancel the draft", reverse("sales:invoice_cancel", args=[draft.pk]), {})
        before = self.entry_ids()
        response = self.send(client, "post the cancelled draft",
                             reverse("sales:invoice_post", args=[draft.pk]), {})
        draft.refresh_from_db()
        self.expect("a cancelled invoice is not posted or revived",
                    draft.status == "CANCELLED" and not draft.journal_entry_id
                    and self.entry_ids() == before
                    and any("cannot be posted" in m for m in self.flashes(client, response)),
                    draft.status)
        try:
            post_invoice(draft, user=self.user)
            refused = False
        except ValidationError:
            refused = True
        self.expect("post_invoice refuses a cancelled invoice itself", refused)
        from apps.sales.models import Invoice
        note = Invoice.objects.filter(company=self.company, status="CREDIT_NOTE").first()
        if note:
            note.journal_entry = None
            try:
                post_invoice(note, user=self.user)
                refused = False
            except ValidationError:
                refused = True
            self.expect("post_invoice refuses a credit note", refused)

    def check_hidden_buttons(self, client):
        from apps.sales.models import CustomerPayment
        order = self.make_order(client, "FIX-31-SO", [self.line(self.service, "1", "500")])
        draft = self.convert(client, order)
        self.pay(client, "FIX-31-PAY", "50")
        payment = CustomerPayment.objects.get(company=self.company, reference="FIX-31-PAY")
        post_url = reverse("sales:invoice_post", args=[draft.pk])
        cancel_url = reverse("sales:invoice_cancel", args=[draft.pk])
        reverse_url = reverse("sales:payment_reverse", args=[payment.pk])
        detail = reverse("sales:invoice_detail", args=[draft.pk])
        listing = reverse("sales:payment_list")
        try:
            for role, post, cancel in (("SALES_REP", False, False),
                                       ("SALES_MANAGER", False, True),
                                       ("AUDITOR", False, False), ("VIEWER", False, False),
                                       ("ACCOUNTANT", True, True),
                                       ("FINANCE_MANAGER", True, True)):
                self.as_role(role)
                page = client.get(detail).content.decode()
                self.expect(f"{role}: Post {'shown' if post else 'hidden'}, "
                            f"Cancel {'shown' if cancel else 'hidden'}",
                            (post_url in page) == post and (cancel_url in page) == cancel)
            for role, shown in (("ACCOUNTANT", False), ("SALES_REP", False),
                                ("SALES_MANAGER", False), ("FINANCE_MANAGER", True)):
                self.as_role(role)
                page = client.get(listing).content.decode()
                self.expect(f"{role}: Reverse {'shown' if shown else 'hidden'} on receipts",
                            (reverse_url in page) == shown)
        finally:
            self.as_role("ADMIN")

    # ─────────────────────────── fourth round ────────────────────────────────

    def check_invoice_numbers(self, client):
        from apps.core.models import SequenceCounter
        from apps.sales.models import Invoice
        rod = self.stocked("ROD-32", "Steel rod 32", "1100", "40", "600")
        preview = client.get(reverse("sales:invoice_create")).context["form"]["reference"].value()
        self.expect("the new-invoice form previews the next INV- number",
                    preview == SequenceCounter.peek_number(self.company, "INV-"), str(preview))
        header = {"customer": self.customer.pk, "invoice_date": str(self.today),
                  "due_date": str(self.today + datetime.timedelta(days=30)),
                  "currency": "KES", "notes": ""}
        self.accepted(client, f"invoice typed as {preview}", reverse("sales:invoice_create"),
                      merge(header, {"reference": preview},
                            formset("lines", [self.line(rod, "5", "1100", tax=False)])))
        first = Invoice.objects.get(company=self.company, reference=preview)
        self.expect("the first invoice took 5 rods off the shelf", self.shelf(rod) == D("35"),
                    str(self.shelf(rod)))
        before = set(Invoice.objects.filter(company=self.company).values_list("pk", flat=True))
        self.accepted(client, "the next invoice left blank", reverse("sales:invoice_create"),
                      merge(header, {"reference": ""},
                            formset("lines", [self.line(rod, "7", "1100", tax=False)])))
        second = Invoice.objects.filter(company=self.company).exclude(pk__in=before).first()
        self.expect("the blank invoice gets a number of its own",
                    second is not None and second.reference != first.reference
                    and Invoice.objects.filter(company=self.company,
                                               reference__iexact=first.reference).count() == 1,
                    f"{first.reference} / {second and second.reference}")
        self.expect("and its own 7 rods leave the shelf", self.shelf(rod) == D("28"),
                    str(self.shelf(rod)))
        if second is not None:
            self.send(client, "cancel the second invoice",
                      reverse("sales:invoice_cancel", args=[second.pk]), {})
            first.refresh_from_db()
            self.expect("cancelling it brings back its 7 only; the first's 5 stay sold",
                        self.shelf(rod) == D("35") and first.status == "SENT",
                        f"shelf {self.shelf(rod)} first {first.status}")
            self.send(client, "cancel the second invoice again",
                      reverse("sales:invoice_cancel", args=[second.pk]), {})
            self.expect("and nothing comes back twice", self.shelf(rod) == D("35"))
        self.refused(client, "an invoice typed with the first number in lower case",
                     reverse("sales:invoice_create"),
                     merge(header, {"reference": first.reference.lower()},
                           formset("lines", [self.line(self.service)])),
                     needle="already used")

    def check_order_draft_party(self, client):
        order = self.make_order(client, "FIX-33-SO", [self.line(self.service, "3", "1500")])
        draft = self.convert(client, order)
        if not self.expect("a draft raised from the order", draft is not None):
            return
        form = client.get(reverse("sales:invoice_edit", args=[draft.pk])).context["form"]
        self.expect("the draft shows customer and currency fixed",
                    form.fields["customer"].disabled and form.fields["currency"].disabled)
        saved = draft.lines.first()
        self.send(client, "re-point the draft to another customer in USD",
                  reverse("sales:invoice_edit", args=[draft.pk]),
                  self.edit_data(draft, [(saved, {})], customer=self.other_customer.pk,
                                 currency="USD"))
        draft.refresh_from_db()
        self.expect("the draft still bills the order's customer in the order's currency",
                    draft.customer_id == order.customer_id and draft.currency == order.currency,
                    f"{draft.customer} {draft.currency}")

    def check_sliced_fx_settlement(self, client):
        from apps.accounting.models import Currency, ExchangeRate
        from apps.crm.models import Customer
        from apps.sales.models import Invoice
        usd = Currency.objects.get(code="USD")
        dated = self.today - datetime.timedelta(days=45)
        # 3.00 at 131.255 is 393.765, rounded up to 393.77; 6.00 is 787.53.
        # Two halves cleared at their own value would take 787.54 off AR.
        ExchangeRate.objects.create(company=self.company, currency=usd, effective_date=dated,
                                    rate=D("131.255"), source="MANUAL")
        payer = Customer.objects.create(company=self.company, name="Sliced Exporters")
        before = self.entry_ids()
        data = {"reference": "FIX-34-INV", "customer": payer.pk, "invoice_date": str(dated),
                "due_date": str(self.today), "currency": "USD", "notes": ""}
        self.accepted(client, "USD 6.00 invoice at 131.255", reverse("sales:invoice_create"),
                      merge(data, formset("lines", [self.line(self.service, "1", "6", tax=False)])))
        inv = Invoice.objects.get(company=self.company, reference="FIX-34-INV")
        self.expect("it went into AR at 787.53", inv.total_amount_base == D("787.53"),
                    str(inv.total_amount_base))
        self.pay(client, "FIX-34-PAY1", "3", currency="USD", customer=payer)
        inv.refresh_from_db()
        held = self.net_moved("1300", before)
        self.expect("part paid, the invoice's book balance is what AR holds",
                    inv.outstanding_base == held and held == D("393.76"),
                    f"outstanding_base {inv.outstanding_base} AR {held}")
        self.pay(client, "FIX-34-PAY2", "3", currency="USD", customer=payer)
        inv.refresh_from_db()
        self.expect("paid in full, AR holds nothing for it",
                    inv.status == "PAID" and self.net_moved("1300", before) == Z
                    and inv.outstanding_base == Z,
                    f"{inv.status} AR {self.net_moved('1300', before)}")

    def closed_month(self, on_date):
        from apps.accounting.models import period_for
        period = period_for(self.company, on_date)
        return period

    def check_failed_posting_rolls_back(self, client):
        from apps.core.models import SequenceCounter
        from apps.crm.models import Customer
        from apps.sales.models import CustomerPayment, Invoice
        day = self.today - datetime.timedelta(days=70)
        period = self.closed_month(day)
        if not self.expect("an accounting period to close", period is not None):
            return
        payer = Customer.objects.create(company=self.company, name="Closed Month Ltd")
        status = period.status
        period.status = "CLOSED"
        period.save(update_fields=["status"])
        try:
            preview = SequenceCounter.peek_number(self.company, "PAY-")
            data = {"reference": "", "customer": payer.pk, "payment_date": str(day),
                    "amount": "1000", "currency": "KES", "payment_method": "MPESA",
                    "bank_account": "", "transaction_reference": "QXA32FAIL", "notes": ""}
            response = self.send(client, "receipt dated in a closed month",
                                 reverse("sales:payment_create"), data)
            self.expect("the form comes back saying why",
                        response is not None and response.status_code == 200
                        and any("not recorded" in e and "closed" in e
                                for e in self.errors(response)),
                        str(self.errors(response) if response else ""))
            self.expect("no receipt is kept, and its number is not used up",
                        not CustomerPayment.objects.filter(customer=payer).exists()
                        and SequenceCounter.peek_number(self.company, "PAY-") == preview)
            data = {"reference": "FIX-35-INV", "customer": payer.pk, "invoice_date": str(day),
                    "due_date": str(self.today), "currency": "KES", "notes": ""}
            response = self.send(client, "invoice dated in a closed month",
                                 reverse("sales:invoice_create"),
                                 merge(data, formset("lines", [self.line(self.service)])))
            self.expect("an invoice that cannot be posted is not kept either",
                        response is not None and response.status_code == 200
                        and not Invoice.objects.filter(company=self.company,
                                                       reference="FIX-35-INV").exists(),
                        str(self.errors(response) if response else ""))
        finally:
            period.status = status
            period.save(update_fields=["status"])
        data = {"reference": "", "customer": payer.pk, "payment_date": str(self.today),
                "amount": "1000", "currency": "KES", "payment_method": "MPESA",
                "bank_account": "", "transaction_reference": "QXA32FAIL", "notes": ""}
        # The M-Pesa code is free again: the failed receipt never existed.
        response = self.send(client, "the same receipt on an open date",
                             reverse("sales:payment_create"), data)
        self.expect("goes through with the same M-Pesa code",
                    response is not None and response.status_code == 302
                    and CustomerPayment.objects.filter(customer=payer).count() == 1)

    def check_delivery_token(self, client):
        order = self.make_order(client, "FIX-36-SO", [self.line(self.service, "20", "1500")])
        line = order.lines.first()
        page = client.get(reverse("sales:order_deliver", args=[order.pk]))
        token = page.context.get("form_token")
        self.expect("the delivery page carries a one-time ticket",
                    bool(token) and 'name="form_token"' in page.content.decode())
        data = {"warehouse": self.warehouse.pk, f"deliver_qty_{line.pk}": "5",
                "form_token": token}
        url = reverse("sales:order_deliver_record", args=[order.pk])
        self.send(client, "deliver 5", url, data)
        response = self.send(client, "the same delivery form again", url, data)
        line.refresh_from_db()
        self.expect("one form for 5, sent twice, delivers 5",
                    line.delivered_quantity == D("5"), str(line.delivered_quantity))
        self.expect("the second copy is told it was already sent",
                    any("already sent" in m for m in self.flashes(client, response)))

    def check_overflow(self, client):
        from apps.sales.models import Invoice, SalesOrder
        header = {"reference": "FIX-37-INV", "customer": self.customer.pk,
                  "invoice_date": str(self.today),
                  "due_date": str(self.today + datetime.timedelta(days=30)),
                  "currency": "KES", "notes": ""}
        self.refused(client, "a line of 99,999,999 at 9,999,999", reverse("sales:invoice_create"),
                     merge(header, formset("lines", [self.line(self.service, "99999999",
                                                               "9999999")])),
                     needle="more than a document can carry")
        rows = [self.line(self.service, "99999999", "90000", tax=False) for _ in range(2)]
        self.refused(client, "lines that fit alone but not together",
                     reverse("sales:invoice_create"), merge(header, formset("lines", rows)),
                     needle="add up to")
        self.expect("nothing was saved", not Invoice.objects.filter(
            company=self.company, reference="FIX-37-INV").exists())
        order = {"reference": "FIX-37-SO", "customer": self.customer.pk,
                 "order_date": str(self.today), "delivery_date": "",
                 "warehouse": self.warehouse.pk, "sales_rep": "", "currency": "KES",
                 "discount_percent": "0", "branch": "", "notes": ""}
        self.refused(client, "an order line of 99,999,999 at 9,999,999",
                     reverse("sales:order_create"),
                     merge(order, formset("lines", [self.line(self.service, "99999999",
                                                              "9999999")])),
                     needle="more than a document can carry")
        self.expect("no order was saved", not SalesOrder.objects.filter(
            company=self.company, reference="FIX-37-SO").exists())

    def check_order_numbers(self, client):
        from apps.core.models import SequenceCounter
        from apps.sales.models import SalesOrder
        page = client.get(reverse("sales:order_create"))
        preview = page.context["form"]["reference"].value()
        self.expect("the order form previews the next SO- number and carries a ticket",
                    preview == SequenceCounter.peek_number(self.company, "SO-")
                    and bool(page.context.get("form_token")), str(preview))
        header = {"customer": self.customer.pk, "order_date": str(self.today),
                  "delivery_date": "", "warehouse": self.warehouse.pk, "sales_rep": "",
                  "currency": "KES", "discount_percent": "0", "branch": "", "notes": ""}
        lines = formset("lines", [self.line(self.service, "1", "2500")])
        count = SalesOrder.objects.filter(company=self.company).count()
        self.accepted(client, "an order with no number", reverse("sales:order_create"),
                      merge(header, {"reference": ""}, lines))
        made = SalesOrder.objects.filter(company=self.company).order_by("-created_at").first()
        self.expect("it is numbered from the SO- series",
                    SalesOrder.objects.filter(company=self.company).count() == count + 1
                    and made.reference.startswith("SO-"), made.reference)
        self.accepted(client, "order FIX-38-SO", reverse("sales:order_create"),
                      merge(header, {"reference": "FIX-38-SO"}, lines))
        self.refused(client, "a second order numbered fix-38-so", reverse("sales:order_create"),
                     merge(header, {"reference": "fix-38-so"}, lines), needle="already used")
        token = client.get(reverse("sales:order_create")).context["form_token"]
        data = merge(header, {"reference": "", "form_token": token}, lines)
        count = SalesOrder.objects.filter(company=self.company).count()
        self.send(client, "order form", reverse("sales:order_create"), data)
        self.send(client, "the same order form again", reverse("sales:order_create"), data)
        self.expect("an order form sent twice books one order",
                    SalesOrder.objects.filter(company=self.company).count() == count + 1)

    def check_round4_dates(self, client):
        from apps.crm.models import Customer
        from apps.sales.models import CustomerPayment, Invoice
        inv = self.make_invoice(client, "FIX-39-INV", [self.line(self.service, "2", "2500")])
        response = self.send(client, "credit note dated in 2031",
                             reverse("sales:invoice_credit_note", args=[inv.pk]),
                             {f"credit_qty_{inv.lines.first().pk}": "1", "reason": "x",
                              "credit_date": "2031-01-01"})
        self.expect("a credit note dated years ahead is refused",
                    not Invoice.objects.filter(original_invoice=inv).exists()
                    and any("still to come" in m for m in self.flashes(client, response)))

        day = self.today - datetime.timedelta(days=70)
        period = self.closed_month(day)
        if period is None:
            return
        payer = Customer.objects.create(company=self.company, name="Closed Credit Ltd")
        data = {"reference": "FIX-39-PAY", "customer": payer.pk, "payment_date": str(day),
                "amount": "1000", "currency": "KES", "payment_method": "CASH",
                "bank_account": "", "transaction_reference": "", "notes": ""}
        self.send(client, "money on account", reverse("sales:payment_create"), data)
        bill = {"reference": "FIX-39-OWED", "customer": payer.pk, "invoice_date": str(day),
                "due_date": str(self.today), "currency": "KES", "notes": ""}
        self.accepted(client, "an invoice the same day", reverse("sales:invoice_create"),
                      merge(bill, formset("lines", [self.line(self.service, "1", "1000",
                                                              tax=False)])))
        owed = Invoice.objects.get(company=self.company, reference="FIX-39-OWED")
        payment = CustomerPayment.objects.get(company=self.company, reference="FIX-39-PAY")
        status = period.status
        period.status = "CLOSED"
        period.save(update_fields=["status"])
        try:
            # Same day as the receipt and the invoice: "the day after" falls in
            # the next (open) month whenever `day` is a month's last day.
            response = self.apply(client, payment, owed, "500", on_date=day)
            owed.refresh_from_db()
            error = response.context.get("error") if response is not None and response.context else ""
            self.expect("credit applied on a date in a closed month is refused",
                        owed.paid_amount == Z and "closed" in (error or ""), str(error))
        finally:
            period.status = status
            period.save(update_fields=["status"])

    def check_fully_credited_order(self, client):
        for reference, then in (("FIX-40-SO-A", "reinvoice"), ("FIX-40-SO-B", "cancel")):
            order = self.make_order(client, reference, [self.line(self.service, "1", "2500")])
            inv = self.convert(client, order)
            if not self.expect(f"{reference} invoiced", inv is not None):
                continue
            self.send(client, "post it", reverse("sales:invoice_post", args=[inv.pk]), {})
            inv.refresh_from_db()
            self.credit(client, inv, [(inv.lines.first(), "1")])
            order.refresh_from_db()
            self.expect(f"{reference}: once credited in full the order is billable again",
                        order.status != "INVOICED", order.status)
            if then == "reinvoice":
                again = self.convert(client, order)
                self.expect("it can be invoiced again", again is not None and again.pk != inv.pk)
            else:
                response = self.send(client, "cancel the order",
                                     reverse("sales:order_cancel", args=[order.pk]), {})
                order.refresh_from_db()
                self.expect("it can be cancelled", order.status == "CANCELLED",
                            f"{order.status} {self.flashes(client, response)}")
