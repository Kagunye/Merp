"""Regression checks for the "My Performance" dashboard workspace.

Usage:
    python manage.py test_fix_dashboard          # rolls the whole run back
    python manage.py test_fix_dashboard --keep   # leave the test company behind

One company, a handful of people in it, and documents spread over this month
and last year; then the dashboard is read the way each of them reads it:

    1. the six tiles          -> each tile's "my" and total figures equal a
                                 direct count and sum of the same documents
    2. the period control     -> this month, last year, a quarter chip and a
                                 custom range each move the figures with them
    3. approvals              -> each role sees the queues it may decide, and
                                 nobody's own requests are counted
    4. top 20                 -> largest first, at most twenty, right amounts
    5. calendar               -> a month's leads, tasks and invoice due dates,
                                 each kind only for a role that may open it
    6. role gating            -> a sales rep sees no purchase tiles; a viewer
                                 sees counts but no money anywhere
    7. junk input             -> odd period, quarter and month parameters
                                 never answer with a 500
"""
import datetime
import re
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.test import Client
from django.test.utils import setup_test_environment, teardown_test_environment
from django.urls import reverse

D = Decimal
Z = D("0")


class Command(BaseCommand):
    help = "Regression checks for the My Performance dashboard."

    def add_arguments(self, parser):
        parser.add_argument("--keep", action="store_true",
                            help="Commit the test data instead of rolling it back.")

    def handle(self, *args, **opts):
        self.passed = 0
        self.failures = []
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

    # ─────────────────────────────── set-up ──────────────────────────────────

    def stamp(self):
        return datetime.datetime.now().strftime("%H%M%S%f")

    def book(self):
        from apps.accounts.models import User
        from apps.organizations.models import Company

        stamp = self.stamp()
        user = User.objects.create(username=f"dash.admin.{stamp}",
                                   email=f"dash.admin.{stamp}@dash.local",
                                   first_name="Dash", last_name="Admin")
        client = Client()
        client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        name = f"Dashboard Fix {stamp}"
        client.post(reverse("organizations:company_create"), {
            "name": name, "legal_name": f"{name} Ltd", "registration_number": f"PVT-{stamp}",
            "tax_id": f"A{stamp}Z", "vat_number": "VAT-1", "currency": "KES",
            "timezone": "Africa/Nairobi", "fiscal_year_start_month": "1",
            "industry": "Trading", "phone": "+254700000000", "email": f"i@{stamp}.dash.local",
            "website": "", "address_line1": "1 Dash St", "address_line2": "",
            "city": "Nairobi", "state_province": "Nairobi", "postal_code": "00100",
            "country": "Kenya",
        })
        self.company = Company.objects.get(name=name)
        self.admin, self.client = user, self.login(user)

    def login(self, user):
        client = Client()
        client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        session = client.session
        session["active_company_id"] = str(self.company.id)
        session.save()
        return client

    def member(self, role):
        from apps.accounts.models import User
        from apps.organizations.models import CompanyMembership
        stamp = self.stamp()
        user = User.objects.create(username=f"dash.{role.lower()}.{stamp}",
                                   email=f"dash.{role.lower()}.{stamp}@dash.local",
                                   first_name=role.title(), last_name="Member")
        CompanyMembership.objects.create(company=self.company, user=user, role=role,
                                         is_default=True)
        return user, self.login(user)

    def employee(self, user, number):
        from apps.hr.models import Employee
        return Employee.objects.create(company=self.company, employee_number=number,
                                       first_name="Staff", last_name=number, user=user,
                                       date_hired=self.today, job_title="Clerk")

    def invoice(self, ref, on, total, customer, paid=Z, order=None, status="SENT"):
        from apps.sales.models import Invoice
        inv = Invoice.objects.create(
            company=self.company, customer=customer, reference=ref, invoice_date=on,
            due_date=on + datetime.timedelta(days=10), status=status, sales_order=order,
            subtotal=D(total), total_amount=D(total), total_amount_base=D(total),
            paid_amount=D(paid))
        inv.refresh_from_db()
        return inv

    # ─────────────────────────────── the run ─────────────────────────────────

    def run(self):
        from apps.core.performance import company_today
        self.book()
        self.today = company_today(self.company)
        self.last_year = datetime.date(self.today.year - 1, 6, 15)
        self.seed()

        self.section("1. The six tiles equal direct queries")
        self.check_tiles()
        self.section("2. The period control moves every figure")
        self.check_periods()
        self.section("3. Approvals, per role, never one's own")
        self.check_approvals()
        self.section("4. Top 20")
        self.check_top20()
        self.section("5. The calendar")
        self.check_calendar()
        self.section("6. Role gating")
        self.check_gating()
        self.section("7. Junk parameters")
        self.check_junk()

    def seed(self):
        from apps.crm.models import Customer, Lead, Supplier
        from apps.procurement.models import Bill, PurchaseOrder, PurchaseRequisition
        from apps.projects.models import Project, Task
        from apps.sales.models import SalesOrder, SalesQuote

        co, t = self.company, self.today
        self.rep, self.rep_client = self.member("SALES_REP")
        self.viewer, self.viewer_client = self.member("VIEWER")
        self.fm, self.fm_client = self.member("FINANCE_MANAGER")
        self.hr, self.hr_client = self.member("HR_MANAGER")
        self.pm, self.pm_client = self.member("PROJECT_MANAGER")
        self.buyer, _ = self.member("PROCUREMENT")

        self.mine_cust = Customer.objects.create(company=co, name="Rep's Account Ltd",
                                                 assigned_to=self.rep)
        self.other_cust = Customer.objects.create(company=co, name="Open Account Ltd")
        # The rep is named on this order, for a customer nobody manages.
        rep_order = SalesOrder.objects.create(company=co, customer=self.other_cust,
                                              reference="DSO-1", order_date=t, sales_rep=self.rep,
                                              total_amount=D("5000"), total_amount_base=D("5000"))
        # Somebody else's order for the rep's customer: the named rep wins.
        SalesOrder.objects.create(company=co, customer=self.mine_cust, reference="DSO-2",
                                  order_date=t, sales_rep=self.admin,
                                  total_amount=D("700"), total_amount_base=D("700"))
        SalesOrder.objects.create(company=co, customer=self.mine_cust, reference="DSO-X",
                                  order_date=t, status="CANCELLED",
                                  total_amount=D("900"), total_amount_base=D("900"))
        SalesOrder.objects.create(company=co, customer=self.mine_cust, reference="DSO-OLD",
                                  order_date=self.last_year,
                                  total_amount=D("444"), total_amount_base=D("444"))

        self.invoice("DINV-1", t, "10000", self.mine_cust, paid="4000")      # mine (account)
        self.invoice("DINV-2", t, "5000", self.other_cust, paid="5000", order=rep_order)  # mine (rep)
        self.invoice("DINV-3", t, "2500", self.other_cust)                   # not mine
        self.invoice("DINV-D", t, "99999", self.mine_cust, status="DRAFT")   # draft: out
        self.invoice("DINV-OLD", self.last_year, "3333", self.mine_cust)     # last year
        # Twenty-one small customers, to check Top 20 stops at twenty.
        for i in range(21):
            cust = Customer.objects.create(company=co, name=f"Small Buyer {i:02d}")
            self.invoice(f"DINV-S{i:02d}", t, str(100 + i), cust)

        SalesQuote.objects.create(company=co, customer=self.mine_cust, reference="DQ-1",
                                  quote_date=t, status="CONVERTED",
                                  total_amount=D("8000"), total_amount_base=D("8000"))
        SalesQuote.objects.create(company=co, customer=self.mine_cust, reference="DQ-2",
                                  quote_date=t, status="EXPIRED",
                                  total_amount=D("1200"), total_amount_base=D("1200"))
        SalesQuote.objects.create(company=co, customer=self.other_cust, reference="DQ-3",
                                  quote_date=t, total_amount=D("300"), total_amount_base=D("300"))

        self.big_supplier = Supplier.objects.create(company=co, name="Big Supplier Ltd")
        small_supplier = Supplier.objects.create(company=co, name="Small Supplier Ltd")
        my_req = PurchaseRequisition.objects.create(
            company=co, reference="DPR-1", requested_by=self.admin, required_date=t,
            status="APPROVED", total_amount=D("6000"))
        PurchaseRequisition.objects.create(
            company=co, reference="DPR-2", requested_by=self.buyer, required_date=t,
            status="DRAFT", total_amount=D("1500"))
        my_po = PurchaseOrder.objects.create(company=co, supplier=self.big_supplier,
                                             reference="DPO-1", order_date=t, requisition=my_req,
                                             total_amount=D("6000"), total_amount_base=D("6000"))
        PurchaseOrder.objects.create(company=co, supplier=small_supplier, reference="DPO-2",
                                     order_date=t, total_amount=D("800"),
                                     total_amount_base=D("800"))
        Bill.objects.create(company=co, supplier=self.big_supplier, reference="DB-1",
                            bill_date=t, due_date=t, status="RECEIVED", purchase_order=my_po,
                            total_amount=D("6000"), total_amount_base=D("6000"),
                            outstanding_amount=D("6000"))
        Bill.objects.create(company=co, supplier=small_supplier, reference="DB-2",
                            bill_date=t, due_date=t, status="RECEIVED",
                            total_amount=D("800"), total_amount_base=D("800"),
                            outstanding_amount=D("800"))

        project = Project.objects.create(company=co, reference="DPJ-1", name="Fit-out",
                                         start_date=t)
        day = datetime.timedelta(days=1)
        for title, due, status in [("Late", t - day, "TODO"), ("Today", t, "TODO"),
                                   ("Later", t + day, "TODO"), ("Undated", None, "TODO"),
                                   ("Busy", t + day, "IN_PROGRESS"), ("Done", t - day, "DONE")]:
            Task.objects.create(project=project, title=f"Dash {title}", status=status,
                                assigned_to=self.admin, due_date=due)
        Task.objects.create(project=project, title="Dash Someone else's", assigned_to=self.rep,
                            due_date=t)
        self.lead = Lead.objects.create(company=co, name="Dash Lead", assigned_to=self.rep,
                                        expected_close_date=t)
        Lead.objects.create(company=co, name="Dash Lost Lead", status="LOST",
                            expected_close_date=t)

    # ─────────────────────────────── helpers ─────────────────────────────────

    def board(self, client, query=""):
        resp = client.get(reverse("dashboard:index") + query)
        return resp, resp.context

    def tiles(self, ctx):
        return {t["key"]: t for t in (ctx.get("tiles") or [])}

    def expected(self, user, start, end):
        """The six tiles worked out the long way, one document at a time."""
        from apps.accounting.currency import book_value
        from apps.core.performance import day_bounds
        from apps.procurement.models import Bill, PurchaseOrder, PurchaseRequisition
        from apps.sales.models import Invoice, SalesOrder, SalesQuote

        co = self.company

        def rep_owns(rep_id, customer):
            return rep_id == user.pk or (rep_id is None and customer.assigned_to_id == user.pk)

        def tally(docs, mine, value=book_value):
            docs = list(docs)
            m = [d for d in docs if mine(d)]
            return {"my_count": len(m), "total_count": len(docs),
                    "my_amount": sum((value(d) for d in m), Z),
                    "total_amount": sum((value(d) for d in docs), Z)}

        lo, hi = day_bounds(co, start, end)
        return {
            "sales_invoices": tally(
                [i for i in Invoice.objects.filter(company=co, invoice_date__range=(start, end))
                 if i.status not in ("DRAFT", "CANCELLED", "CREDIT_NOTE")],
                lambda i: rep_owns(i.sales_order.sales_rep_id if i.sales_order else None,
                                   i.customer)),
            "sales_orders": tally(
                SalesOrder.objects.filter(company=co, order_date__range=(start, end))
                .exclude(status="CANCELLED"), lambda o: rep_owns(o.sales_rep_id, o.customer)),
            "sales_quotes": tally(
                SalesQuote.objects.filter(company=co, quote_date__range=(start, end))
                .exclude(status="CANCELLED"), lambda q: rep_owns(q.sales_rep_id, q.customer)),
            "purchase_invoices": tally(
                [b for b in Bill.objects.filter(company=co, bill_date__range=(start, end))
                 if b.status not in ("DRAFT", "CANCELLED", "DEBIT_NOTE")],
                lambda b: bool(b.purchase_order and b.purchase_order.requisition
                               and b.purchase_order.requisition.requested_by_id == user.pk)),
            "purchase_orders": tally(
                PurchaseOrder.objects.filter(company=co, order_date__range=(start, end))
                .exclude(status="CANCELLED"),
                lambda p: bool(p.requisition and p.requisition.requested_by_id == user.pk)),
            "requisitions": tally(
                PurchaseRequisition.objects.filter(company=co, created_at__gte=lo,
                                                   created_at__lt=hi)
                .exclude(status="CANCELLED"),
                lambda r: r.requested_by_id == user.pk, value=lambda r: r.total_amount),
        }

    def same(self, tile, want, money=True):
        keys = ["my_count", "total_count"] + (["my_amount", "total_amount"] if money else [])
        return all(D(str(tile[k])) == D(str(want[k])) for k in keys)

    # ─────────────────────────────── checks ──────────────────────────────────

    def check_tiles(self):
        from apps.core.performance import month_end, month_start
        t = self.today
        for who, user, client, keys in [
            ("admin", self.admin, self.client, None),
            ("sales rep", self.rep, self.rep_client,
             ["sales_invoices", "sales_orders", "sales_quotes"]),
        ]:
            resp, ctx = self.board(client, "?period=month")
            got = self.tiles(ctx)
            want = self.expected(user, month_start(t), month_end(t))
            for key in keys or want:
                self.expect(f"{who}: {key} tile equals a direct query this month",
                            key in got and self.same(got[key], want[key]),
                            f"{ {k: got.get(key, {}).get(k) for k in want[key]} } vs {want[key]}")
        rep = self.tiles(self.board(self.rep_client, "?period=month")[1])
        self.expect("the rep's invoices are the account's and the order's, not the draft",
                    rep["sales_invoices"]["my_count"] == 2
                    and rep["sales_invoices"]["my_amount"] == D("15000.00"),
                    f"{rep['sales_invoices']['my_count']} / {rep['sales_invoices']['my_amount']}")
        self.expect("the named rep wins over the account manager on an order",
                    rep["sales_orders"]["my_count"] == 1
                    and rep["sales_orders"]["my_amount"] == D("5000.00"))
        adm = self.tiles(self.board(self.client, "?period=month")[1])
        self.expect("my bills, orders and requisitions follow my requisition",
                    adm["purchase_invoices"]["my_amount"] == D("6000.00")
                    and adm["purchase_orders"]["my_count"] == 1
                    and adm["requisitions"]["my_count"] == 1
                    and adm["requisitions"]["total_count"] == 2)
        share = rep["sales_invoices"]["share"]
        total = rep["sales_invoices"]["total_amount"]
        self.expect("the bar is my share of the total value",
                    share == (D("15000") / total * 100).quantize(D("0.1")), f"{share}%")

        gauge = self.board(self.rep_client, "?period=month")[1]["gauge"]
        self.expect("the gauge is my collection rate: paid over invoiced",
                    gauge["rate"] == D("60.0") and gauge["invoiced"] == D("15000.00")
                    and gauge["paid"] == D("9000.00"), str(gauge["rate"]))
        self.expect("and the quote conversion rate beside it",
                    gauge["conversion"] == D("50.0"), str(gauge["conversion"]))
        eff = self.board(self.rep_client, "?period=month")[1]["efficiency"]
        self.expect("efficiency: my month's sales and lost sales",
                    eff["mine"]["sales"] == [15000.0] and eff["mine"]["lost"] == [2100.0],
                    f"{eff['mine']}")
        tasks = {c["key"]: c["count"] for c in self.board(self.client)[1]["tasks"]}
        self.expect("my task list: overdue, due today, current, in progress",
                    tasks == {"overdue": 1, "today": 1, "current": 2, "in_progress": 1},
                    str(tasks))

    def check_periods(self):
        from apps.core.performance import quarter_of, quarter_range
        t, ly = self.today, self.last_year
        last = self.tiles(self.board(self.client, "?period=last_year")[1])
        want = self.expected(self.admin, datetime.date(ly.year, 1, 1),
                             datetime.date(ly.year, 12, 31))
        self.expect("last year shows last year's documents only",
                    self.same(last["sales_invoices"], want["sales_invoices"])
                    and last["sales_invoices"]["total_count"] == 1
                    and last["sales_invoices"]["total_amount"] == D("3333.00"),
                    str(last["sales_invoices"]["total_amount"]))
        ytd = self.tiles(self.board(self.client)[1])
        self.expect("the default is year to date",
                    self.board(self.client)[1]["period"].key == "ytd"
                    and ytd["sales_invoices"]["total_count"] == 24)
        code = f"{ly.year}-Q{quarter_of(ly)}"
        resp, ctx = self.board(self.client, f"?quarter={code}")
        qs, qe = quarter_range(ly.year, quarter_of(ly))
        self.expect("a quarter chip shows that quarter",
                    ctx["period"].start == qs and ctx["period"].end == qe
                    and self.tiles(ctx)["sales_orders"]["total_count"] == 1)
        resp, ctx = self.board(self.client, f"?period=custom&from={t}&to={ly}")
        self.expect("a backwards custom range is turned round",
                    ctx["period"].start == ly and ctx["period"].end == t
                    and self.tiles(ctx)["sales_invoices"]["total_count"] == 25)
        resp, ctx = self.board(self.client, "?period=month")
        self.expect("this month leaves last year out",
                    self.tiles(ctx)["sales_invoices"]["total_count"] == 24)

    def check_approvals(self):
        from apps.expenses.models import ExpenseClaim
        from apps.hr.models import LeaveRequest, LeaveType
        from apps.payroll.models import PayrollRun
        from apps.procurement.models import PurchaseRequisition
        from apps.timesheets.models import TimesheetEntry

        co, t = self.company, self.today
        PurchaseRequisition.objects.create(company=co, reference="DPR-P1", requested_by=self.buyer,
                                           submitted_by=self.buyer, required_date=t,
                                           status="PENDING_APPROVAL")
        PurchaseRequisition.objects.create(company=co, reference="DPR-P2", requested_by=self.fm,
                                           submitted_by=self.fm, required_date=t,
                                           status="PENDING_APPROVAL")
        PurchaseRequisition.objects.create(company=co, reference="DPR-P3", requested_by=self.buyer,
                                           submitted_by=self.fm, required_date=t,
                                           status="PENDING_APPROVAL")
        e_fm, e_hr = self.employee(self.fm, "D-FM"), self.employee(self.hr, "D-HR")
        e_rep = self.employee(self.rep, "D-REP")
        for ref, emp, by in [("DEC-1", e_rep, self.rep), ("DEC-2", e_fm, self.fm),
                             ("DEC-3", e_hr, self.hr), ("DEC-4", e_rep, self.fm)]:
            ExpenseClaim.objects.create(company=co, reference=ref, employee=emp, claim_date=t,
                                        status="SUBMITTED", created_by=by,
                                        total_amount=D("100"))
        lt, _ = LeaveType.objects.get_or_create(company=co, name="Dash Leave")
        for emp, by in [(e_rep, self.rep), (e_hr, self.hr), (e_fm, self.hr)]:
            LeaveRequest.objects.create(employee=emp, leave_type=lt, start_date=t, end_date=t,
                                        status="PENDING", created_by=by)
        for user in (self.rep, self.pm, self.hr):
            TimesheetEntry.objects.create(company=co, user=user, entry_date=t, hours=D("2"),
                                          status="SUBMITTED")
        TimesheetEntry.objects.create(company=co, user=self.rep,
                                      entry_date=t + datetime.timedelta(days=3),
                                      hours=D("2"), status="SUBMITTED")
        for ref, by in [("DPAY-1", self.admin), ("DPAY-2", self.fm)]:
            PayrollRun.objects.create(company=co, reference=ref, period_start=t, period_end=t,
                                      payment_date=t, status="APPROVED", approved_by=by)

        def counts(client):
            return {a["key"]: a["count"] for a in self.board(client)[1]["approvals"]}

        fm = counts(self.fm_client)
        self.expect("finance manager: requisitions not raised or submitted by them",
                    fm.get("requisitions") == 1, str(fm))
        self.expect("finance manager: claims not theirs and not typed by them",
                    fm.get("expense_claims") == 2, str(fm))
        self.expect("finance manager: payroll runs to post, not ones they approved",
                    fm.get("payroll_post") == 1 and fm.get("payroll_approve") == 0, str(fm))
        self.expect("finance manager: no leave or timesheet queues",
                    "leave" not in fm and "timesheets" not in fm, str(sorted(fm)))
        hr = counts(self.hr_client)
        self.expect("HR: leave not their own and not filed by them",
                    hr.get("leave") == 1, str(hr))
        self.expect("HR: claims and timesheets, never their own, never not yet worked",
                    hr.get("expense_claims") == 3 and hr.get("timesheets") == 2, str(hr))
        self.expect("HR: no requisitions or payroll", not {"requisitions", "payroll_post",
                                                           "payroll_approve"} & set(hr))
        pm = counts(self.pm_client)
        self.expect("project manager: only timesheets, without their own",
                    pm == {"timesheets": 2}, str(pm))
        self.expect("sales rep: nothing to approve", counts(self.rep_client) == {})
        adm = counts(self.client)
        self.expect("admin: every queue", {"requisitions", "leave", "expense_claims",
                                           "timesheets", "payroll_approve",
                                           "payroll_post"} <= set(adm), str(sorted(adm)))

    def check_top20(self):
        top = {t["key"]: t for t in self.board(self.client, "?period=month")[1]["top20"]}
        cust = top["customers"]["rows"]
        amounts = [r["amount"] for r in cust]
        self.expect("customers: at most twenty, largest first",
                    len(cust) == 20 and amounts == sorted(amounts, reverse=True), str(len(cust)))
        self.expect("customers: the biggest buyer heads the list with its sales",
                    cust[0]["name"] == "Rep's Account Ltd" and cust[0]["amount"] == D("10000.00")
                    and cust[1]["name"] == "Open Account Ltd"
                    and cust[1]["amount"] == D("7500.00"), f"{cust[0]} {cust[1]}")
        vend = top["vendors"]["rows"]
        self.expect("vendors: by purchases, largest first",
                    [r["name"] for r in vend] == ["Big Supplier Ltd", "Small Supplier Ltd"]
                    and vend[0]["amount"] == D("6000.00"), str(vend))
        self.expect("the inventory tab is there", "inventory" in top)
        last = {t["key"]: t for t in self.board(self.client, "?period=last_year")[1]["top20"]}
        self.expect("top 20 follows the period",
                    [r["name"] for r in last["customers"]["rows"]] == ["Rep's Account Ltd"]
                    and not last["vendors"]["rows"])

    def check_calendar(self):
        month = self.today.strftime("%Y-%m")
        url = reverse("dashboard:calendar") + f"?month={month}"
        data = self.client.get(url).json()
        kinds = {}
        for e in data["events"]:
            kinds.setdefault(e["kind"], []).append(e["title"])
        self.expect("the month lists open leads, not lost ones",
                    kinds.get("lead") == ["Dash Lead"], str(kinds.get("lead")))
        mine_due = sorted(t for t in kinds.get("task", []))
        want = sorted(f"Dash {x}" for x in ("Late", "Today", "Later", "Busy")
                      if self.month_has(x))
        self.expect("my open tasks' due dates, not others' or done ones",
                    mine_due == want, f"{mine_due} vs {want}")
        from apps.core.performance import month_end, month_start
        from apps.sales.models import Invoice
        open_due = set(Invoice.objects.filter(
            company=self.company, status__in=["SENT", "PARTIAL", "OVERDUE"],
            due_date__range=(month_start(self.today), month_end(self.today)),
        ).values_list("reference", flat=True))
        invoices = {e["title"] for e in data["events"] if e["kind"] == "invoice"}
        self.expect("open invoices' due dates in the month, not drafts or paid ones",
                    invoices == open_due and "DINV-D" not in invoices
                    and "DINV-2" not in invoices, f"{len(invoices)} vs {len(open_due)}")
        self.expect("the page carries this month's calendar",
                    self.board(self.client)[1]["calendar"]["month"] == month)
        rep = self.rep_client.get(url).json()
        rkinds = {e["kind"] for e in rep["events"]}
        self.expect("a sales rep's calendar has leads, their tasks and invoices, no interviews",
                    "interview" not in rkinds and "lead" in rkinds
                    and [e["title"] for e in rep["events"] if e["kind"] == "task"]
                    == ["Dash Someone else's"], str(rkinds))
        blocked = [e for e in rep["events"] if e["url"].startswith("/recruitment/")]
        self.expect("and no links it may not open", not blocked)

    def month_has(self, title):
        t, day = self.today, datetime.timedelta(days=1)
        due = {"Late": t - day, "Today": t, "Later": t + day, "Busy": t + day}[title]
        return due.month == t.month and due.year == t.year

    def check_gating(self):
        from django.urls import Resolver404, resolve
        from apps.core import access

        resp, ctx = self.board(self.rep_client, "?period=month")
        keys = set(self.tiles(ctx))
        self.expect("a sales rep sees the three sales tiles and no purchase tiles",
                    keys == {"sales_invoices", "sales_orders", "sales_quotes"}, str(keys))
        body = resp.content.decode()
        self.expect("...and no purchase figures on the page",
                    "My Purchase" not in body and "Big Supplier Ltd" not in body)
        self.expect("...nor a cash book or journal shortcut",
                    not any(q["label"] in ("Cash Book", "Journal Entry", "Purchase Invoices")
                            for q in ctx["quick_access"]))

        resp, ctx = self.board(self.viewer_client, "?period=month")
        body = resp.content.decode()
        tiles = self.tiles(ctx)
        self.expect("a viewer sees the tiles as counts",
                    tiles and all(t["my_amount"] is None and t["total_amount"] is None
                                  for t in tiles.values()), str(sorted(tiles)))
        self.expect("a viewer sees no gauge, efficiency or top 20",
                    ctx["gauge"] is None and ctx["efficiency"] is None and not ctx["top20"])
        self.expect("a viewer sees no money on the page",
                    not any(s in body for s in ("10,000.00", "7,500.00", "6,000.00", "2,500.00",
                                                "15,000.00", "5,000.00")))
        self.expect("a viewer's calendar carries no amounts",
                    all("KES" not in e["meta"] for e in ctx["calendar"]["events"]))

        for role, client, user in [("SALES_REP", self.rep_client, self.rep),
                                   ("VIEWER", self.viewer_client, self.viewer),
                                   ("FINANCE_MANAGER", self.fm_client, self.fm)]:
            body = client.get(reverse("dashboard:index")).content.decode()
            main = re.sub(r"<aside.*?</aside>", "", body, flags=re.S)
            blocked = set()
            for href in re.findall(r'href="(/(?!static/|media/)[^"#?]*)', main):
                try:
                    match = resolve(href)
                except Resolver404:
                    continue
                if not access.can(user, role, match.namespace, match.url_name, "GET"):
                    blocked.add(href)
            self.expect(f"{role}: every link on the workspace opens for them",
                        not blocked, str(sorted(blocked)[:5]))
            self.expect(f"{role}: the sidebar is still the only <aside>",
                        body.count("<aside") == 1)

    def check_junk(self):
        junk = ["?period=nonsense", "?period=custom", "?period=custom&from=abc&to=xyz",
                "?period=custom&from=0001-01-01&to=9999-12-31", "?period=custom&to=2026-02-30",
                "?quarter=2026-Q0", "?quarter=Q3", "?quarter=-Q-", "?quarter=99999-Q1",
                "?period=custom&from=1900-01-01&to=2099-12-31", "?from=%00&to=%ff",
                "?period=month&quarter=2026-Q2&from=x"]
        bad = []
        for q in junk:
            resp = self.client.get(reverse("dashboard:index") + q)
            if resp.status_code != 200:
                bad.append((q, resp.status_code))
        self.expect("odd period parameters still draw the page", not bad, str(bad))
        bad = []
        for m in ["", "junk", "2026-13", "0-0", "99999-01", "2026", "2026-02-31"]:
            resp = self.client.get(reverse("dashboard:calendar") + f"?month={m}")
            if resp.status_code != 200:
                bad.append((m, resp.status_code))
        self.expect("odd calendar months still answer", not bad, str(bad))
        resp, ctx = self.board(self.client, "?period=custom&from=1900-01-01&to=2099-12-31")
        self.expect("a custom range is held to five years of months",
                    len(ctx["period"].months()) <= 60 and len(ctx["efficiency"]["labels"]) <= 60)
