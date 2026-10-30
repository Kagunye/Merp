"""Regression checks for the payroll, HR, expenses and timesheet fixes.

Usage:
    python manage.py test_fix_payroll          # rolls the whole run back

Every check drives the real screens through the test client, as the person
whose job it is — HR writes contracts, the payroll officer raises runs, finance
approves and posts, an employee files their own leave and claims — so a check
fails for the same reason a user would be hurt:

    1. a run whose entry was already reversed from the journal can still be
       stood down, without a second reversal, and the month can be run again
    2. a dated rate change is a new row under the same code; a second tiered
       table under another code is still refused; and a book that has lost its
       tax table is told so instead of paying everybody untaxed
    3. somebody on leave is still paid
    4. the steps of a table that paid a settled run cannot be added or removed
    5. pay is charged to an expense account and paid out of money
    6. statutory codes are one case, so 'nssf' is NSSF
    7. a cancellation dated into a closed month says so
    8. expense claims: drafts only are edited, people claim for themselves,
       nobody approves or pays their own, the entry is dated on the payment
    9. leave: dates in order, within the entitlement, for yourself, and private
   10. an empty contract form is a form error, not a crash
   11. no negative or more-than-whole rates and figures in the payroll tables
   12. timesheet hours fit in a day, and the day is a plausible one
   13. basic pay is pro-rated by calendar days: a joiner, a mid-month raise, a
       contract that ends mid-month
   14. a leaver is paid for the days worked, from a contract that is no longer
       current; an ended contract is saved as history, not refused
   15. re-running a cancelled month pays from the contracts in force then
   16. HR terminates an employee: contract ended, paid to the day, then stopped
   17. re-activating a second tiered table is refused like creating one
   18. leave across the year end draws on each year's entitlement separately
   19. staff see only their own hours; HR and project managers see everybody's
   20. an employee sees their own approved payslips and nobody else's
   21. HR links a login to an employee, from this company's members only
   22. a run's payment date has to fall in or shortly after its period
   23. a reimbursement is left out of the statutory base; a pension deduction
       can come off taxable pay
   24. SHIF is never less than its 300 minimum for somebody who is paid

Third round (25-44, check_round_three):

   25. a run lies within one calendar month
   26. pay is a share of the calendar month: two half-month runs pay exactly
       one month's basic and allowance, and withhold what one month would
   27. fixed allowances are pro-rated for a joiner; a loan repayment is not
   28. pension relief (NSSF plus private schemes) is capped at 30,000 a month
   29. a back-dated contract cannot overlap a later one or take its flag
   30. a contract that paid an approved run keeps its pay, dates and employee
   31. pay elements are booked to expense (earnings) or liability (deductions),
       on the form and again when the run is posted
   32. correcting a leaving date to a later day extends the contract, unless
       a settled run already covers the days in between
   33. approval refuses a draft worked out before a contract changed
   34. the payroll entry is dated on the period end
   35. recalculating a settled run says it was not recalculated
   36. a junk run filter on the payslip list is not a server error
   37. duplicate staff numbers, pay element names and categories are form errors
   38. expense claims: raised, approved and paid by three different people
   39. HR does not approve leave it filed itself
   40. a new company has the standard leave types, and HR can manage them
   41. carry-forward brings only last year's unused days, capped
   42. leave is counted Monday to Friday, and the same days cannot be asked twice
   43. buttons a role cannot use are not shown
   44. a payroll officer sees leave but does not decide it

Exits non-zero when any check fails.
"""
import datetime
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.test import Client
from django.test.utils import setup_test_environment, teardown_test_environment
from django.urls import reverse

D = Decimal
ISO = "%Y-%m-%d"


class Command(BaseCommand):
    help = "Regression checks for the payroll / HR / expenses / timesheet fixes."

    def handle(self, *args, **opts):
        self.passed = 0
        self.failures = []
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
            for line in self.failures:
                self.stderr.write(self.style.ERROR(f"  FAIL {line}"))
        else:
            self.stdout.write(self.style.SUCCESS("Failures: 0"))
        self.stdout.write("Test data rolled back.")
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
        location = response.get("Location")
        page = client.get(location) if location else response
        ctx = getattr(page, "context", None)
        if not ctx or "messages" not in ctx:
            return []
        return [str(m.message) for m in ctx["messages"]]

    def said(self, client, response):
        return " | ".join(self.flashes(client, response))

    def errors(self, response):
        ctx = getattr(response, "context", None)
        form = ctx.get("form") if ctx else None
        if form is None:
            return ""
        return " ".join(str(e) for row in form.errors.values() for e in row)

    def refused(self, label, response, needle=None):
        """A form POST that must come back to the page with an error on it."""
        errs = self.errors(response)
        ok = response.status_code == 200 and bool(errs)
        if ok and needle:
            ok = needle.lower() in errs.lower()
        return self.expect(label, ok, f"HTTP {response.status_code}: {errs[:160]}")

    # ─────────────────────────────── set-up ──────────────────────────────────

    def user(self, tag, first, last):
        from apps.accounts.models import User
        u = User.objects.create(
            username=f"{tag}.{self.stamp}", email=f"{tag}.{self.stamp}@fix.local",
            first_name=first, last_name=last)
        u.set_password("fix-test-pw")
        u.save()
        return u

    def client_for(self, user):
        c = Client(raise_request_exception=False)
        c.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        return c

    def run(self):
        from apps.organizations.models import Company, CompanyMembership

        self.stamp = datetime.datetime.now().strftime("%H%M%S%f")
        admin = self.user("admin", "Ada", "Admin")
        ca = self.client_for(admin)
        name = f"Payroll Fix Ltd {self.stamp}"
        resp = ca.post(reverse("organizations:company_create"), {
            "name": name, "legal_name": f"{name} Limited",
            "registration_number": f"PVT-{self.stamp}", "tax_id": f"P{self.stamp}Z",
            "vat_number": "", "currency": "KES", "timezone": "Africa/Nairobi",
            "fiscal_year_start_month": "1", "industry": "Services",
            "phone": "+254700000000", "email": f"info@{self.stamp}.fix.local",
            "website": "", "address_line1": "1 Moi Ave", "address_line2": "",
            "city": "Nairobi", "state_province": "Nairobi", "postal_code": "00100",
            "country": "Kenya"})
        company = Company.objects.filter(name=name).first()
        if not self.expect("company created through the form",
                           resp.status_code in (302, 303) and company is not None,
                           f"HTTP {resp.status_code}"):
            raise SystemExit(1)
        self.company = company
        self.users = {"ADMIN": admin}
        self.c = {"ADMIN": ca}
        for key, role, first in (("HR", "HR_MANAGER", "Hana"),
                                 ("PO", "PAYROLL_OFFICER", "Peter"),
                                 ("FM", "FINANCE_MANAGER", "Fiona"),
                                 ("FM2", "FINANCE_MANAGER", "Faith"),
                                 ("EMP", "EMPLOYEE", "Evans"),
                                 ("VIEW", "VIEWER", "Victor"),
                                 ("PM", "PROJECT_MANAGER", "Paul")):
            u = self.user(key.lower(), first, role.title())
            CompanyMembership.objects.create(company=company, user=u, role=role,
                                             is_default=True)
            self.users[key] = u
            self.c[key] = self.client_for(u)

        self.check_contract_form()
        self.check_payroll()
        self.check_expenses()
        self.check_leave()
        self.check_timesheets()
        # Second round of fixes (13-24 below).
        self.check_contract_history()
        self.check_rule_toggle()
        self.check_leave_year_end()
        self.check_hours_privacy()
        self.check_employee_edit()
        # Third round (25-44).
        self.check_round_three()

    def employee(self, number, first, status="ACTIVE", user=None):
        from apps.hr.models import Employee
        return Employee.objects.create(
            company=self.company, employee_number=number, first_name=first,
            last_name="Test", status=status, date_hired=datetime.date(2025, 1, 1),
            job_title="Staff", user=user)

    def account(self, code):
        from apps.accounting.models import Account
        return Account.objects.get(company=self.company, code=code)

    # ───────────────────────────── 10. contracts ─────────────────────────────

    def check_contract_form(self):
        from apps.hr.models import Contract

        self.section("10. contract form")
        hr = self.c["HR"]
        self.high = self.employee("H1", "High")
        url = reverse("hr:contract_create", args=[self.high.pk])
        resp = hr.post(url, {})
        self.expect("an empty contract post is a form error, not a server error",
                    resp.status_code == 200, f"HTTP {resp.status_code}")
        resp = hr.post(url, {"basic_salary": "0"})
        self.expect("a partial contract post is a form error, not a server error",
                    resp.status_code == 200, f"HTTP {resp.status_code}")
        resp = hr.post(url, {
            "employee": str(self.high.pk), "contract_type": "PERMANENT",
            "job_title": "Staff", "department": "", "start_date": "2025-01-01",
            "end_date": "", "basic_salary": "150000", "is_current": "on"})
        self.expect("a whole contract still saves",
                    resp.status_code == 302
                    and Contract.objects.filter(employee=self.high).exists(),
                    f"HTTP {resp.status_code} {self.errors(resp)}")

    # ───────────────────────────── payroll ───────────────────────────────────

    def make_run(self, client, ref, start, end, **extra):
        from apps.payroll.models import PayrollRun
        data = {"reference": ref, "period_start": start, "period_end": end,
                "payment_date": end, "salary_account": "", "paid_into": "", "notes": ""}
        data.update(extra)
        resp = client.post(reverse("payroll:run_create"), data)
        return resp, PayrollRun.objects.filter(company=self.company, reference=ref).first()

    def check_payroll(self):
        from apps.accounting.models import JournalEntry
        from apps.hr.models import Contract
        from apps.payroll import services
        from apps.payroll.models import PayrollRun, Payslip, StatutoryRule

        company = self.company
        hr, po, fm, fm2 = self.c["HR"], self.c["PO"], self.c["FM"], self.c["FM2"]

        # ── 3. somebody on leave is still paid ──
        self.section("3. staff on leave are on the payroll")
        away = self.employee("L1", "Away", status="ON_LEAVE")
        Contract.objects.create(employee=away, start_date=datetime.date(2025, 1, 1),
                                basic_salary=D("45000"), job_title="Staff")
        home = po.get(reverse("payroll:index"))
        self.expect("the payroll home counts the person on leave as staff",
                    home.status_code == 200 and home.context["staff_count"] == 2,
                    str(home.context and home.context.get("staff_count")))

        # ── 5. the run's accounts ──
        self.section("5. salary and payment accounts")
        resp, bad = self.make_run(po, "PR-BAD1", "2026-07-01", "2026-07-31",
                                  salary_account=str(self.account("1200").pk))
        self.refused("the bank is refused as the salary account", resp)
        resp, bad = self.make_run(po, "PR-BAD2", "2026-07-01", "2026-07-31",
                                  paid_into=str(self.account("6100").pk))
        self.refused("an expense account is refused as the account pay leaves", resp)
        form = po.get(reverse("payroll:run_create")).context["form"]
        offered = set(form.fields["salary_account"].queryset.values_list(
            "account_type", flat=True))
        self.expect("the salary picker only offers expense accounts",
                    offered == {"EXPENSE"}, str(offered))

        # ── August: the person on leave is paid, the high earner is taxed ──
        resp, aug = self.make_run(po, "PR-AUG", "2026-08-01", "2026-08-31")
        if not self.expect("August is raised", aug is not None, self.said(po, resp)):
            return
        self.expect("the person on leave has a payslip",
                    aug.payslips.filter(employee=away).exists(),
                    f"{aug.payslips.count()} slip(s)")
        slip = aug.payslips.filter(employee=self.high).first()
        self.expect("150,000 basic nets 103,618.15 after PAYE",
                    slip is not None and slip.net_salary == D("103618.15"),
                    str(slip and slip.net_salary))

        # The posting service refuses a wrong account even when the form is bypassed.
        PayrollRun.objects.filter(pk=aug.pk).update(salary_account=self.account("1200"))
        fm.post(reverse("payroll:run_approve", args=[aug.pk]))
        resp = fm.post(reverse("payroll:run_post", args=[aug.pk]))
        aug.refresh_from_db()
        said = self.said(fm, resp)
        self.expect("posting refuses to charge pay to the bank",
                    aug.journal_entry_id is None and "expense account" in said, said)
        PayrollRun.objects.filter(pk=aug.pk).update(salary_account=None)
        resp = fm.post(reverse("payroll:run_post", args=[aug.pk]))
        aug.refresh_from_db()
        self.expect("August posts to the default salary account",
                    aug.status == "POSTED" and aug.journal_entry_id is not None,
                    aug.status)

        paye = StatutoryRule.objects.get(company=company, code="PAYE")

        # ── 4. settled bands ──
        self.section("4. a settled table's steps are frozen")
        top = paye.bands().last()
        count = paye.tax_bands.count()
        resp = po.post(reverse("payroll:band_delete", args=[top.pk]))
        self.expect("a band of a table that paid a posted run cannot be deleted",
                    paye.tax_bands.count() == count and "new PAYE table" in self.said(po, resp),
                    f"{paye.tax_bands.count()} band(s)")
        resp = po.post(reverse("payroll:band_create", args=[paye.pk]),
                       {"lower": "5000000", "upper": "", "percent": "40"})
        self.expect("nor can a band be added to it",
                    resp.status_code == 302 and paye.tax_bands.count() == count,
                    f"HTTP {resp.status_code}, {paye.tax_bands.count()} band(s)")
        page = po.get(reverse("payroll:band_create", args=[paye.pk]))
        self.expect("the add-band page turns the user away too",
                    page.status_code == 302, f"HTTP {page.status_code}")

        # ── 2a. a dated rate change is a new row under the same code ──
        self.section("2. dated changes to the tax table")
        rule_url = reverse("payroll:rule_create")
        paye_form = {
            "code": "PAYE", "name": "PAYE from October", "method": "BANDS",
            "basis": "GROSS", "employee_percent": "0", "employer_percent": "0",
            "employee_flat": "0", "employer_flat": "0", "basis_cap": "",
            "monthly_relief": "2400", "reduces_taxable_pay": "",
            "payable_account": str(paye.payable_account_id), "expense_account": "",
            "effective_from": "2026-10-01", "is_active": "on"}
        resp = po.post(rule_url, paye_form)
        new_paye = StatutoryRule.objects.filter(
            company=company, code="PAYE", effective_from="2026-10-01").first()
        self.expect("a new PAYE row from 1 October is accepted with the old one active",
                    resp.status_code == 302 and new_paye is not None,
                    self.errors(resp))
        resp = po.post(rule_url, dict(paye_form, code="PAYE2", name="Second tiers",
                                      effective_from="2026-12-01"))
        self.refused("a second tiered table under another code is still refused",
                     resp, "tiered table")
        if new_paye:
            for lower, upper, pct in (("0", "24000", "10"), ("24000", "32333", "25"),
                                      ("32333", "500000", "30"), ("500000", "800000", "32.5"),
                                      ("800000", "", "35")):
                po.post(reverse("payroll:band_create", args=[new_paye.pk]),
                        {"lower": lower, "upper": upper, "percent": pct})
            in_force = {r.code: r.pk for r in services.rules_as_at(
                company, datetime.date(2026, 10, 31))}
            self.expect("October reads the new row, August the old one",
                        in_force.get("PAYE") == new_paye.pk
                        and {r.code: r.pk for r in services.rules_as_at(
                            company, datetime.date(2026, 8, 31))}.get("PAYE") == paye.pk)

        # ── 11. junk figures in the tables ──
        self.section("11. figures the tables cannot hold")
        levy = {"code": "LEVY", "name": "Levy", "method": "PERCENT", "basis": "GROSS",
                "employee_percent": "1", "employer_percent": "0", "employee_flat": "0",
                "employer_flat": "0", "basis_cap": "", "monthly_relief": "0",
                "reduces_taxable_pay": "", "payable_account": str(self.account("2600").pk),
                "expense_account": "", "effective_from": "2026-12-01", "is_active": "on"}
        for label, change in (("a negative employee percent", {"employee_percent": "-10"}),
                              ("an employee percent over 100", {"employee_percent": "150"}),
                              ("a negative employer percent",
                               {"employer_percent": "-1",
                                "expense_account": str(self.account("6100").pk)}),
                              ("a negative pay ceiling", {"basis_cap": "-1000"}),
                              ("a negative flat figure",
                               {"method": "FLAT", "employee_percent": "0",
                                "employee_flat": "-500"}),
                              ("a negative employer flat", {"employer_flat": "-5"})):
            self.refused(f"a rule with {label} is refused", po.post(rule_url, dict(levy, **change)))
        self.expect("none of them was saved",
                    not StatutoryRule.objects.filter(company=company, code="LEVY").exists())
        comp_url = reverse("payroll:component_create")
        comp = {"employee": "", "name": "X", "component_type": "ALLOWANCE",
                "calculation": "FIXED", "amount": "100", "percent": "0",
                "is_taxable": "on", "gl_account": "", "is_active": "on"}
        for label, change in (("a negative allowance", {"name": "NegA", "amount": "-5000"}),
                              ("a negative deduction",
                               {"name": "NegD", "component_type": "DEDUCTION",
                                "amount": "-5000",
                                "gl_account": str(self.account("2600").pk)}),
                              ("500% of basic", {"name": "P500",
                                                 "calculation": "PERCENT_OF_BASIC",
                                                 "percent": "500"}),
                              ("a negative percent", {"name": "PNeg",
                                                      "calculation": "PERCENT_OF_GROSS",
                                                      "percent": "-5"})):
            self.refused(f"a pay element with {label} is refused",
                         po.post(comp_url, dict(comp, **change)))
        if new_paye:
            band_url = reverse("payroll:band_create", args=[new_paye.pk])
            for label, data in (("a negative rate", {"lower": "900000000", "upper": "",
                                                     "percent": "-10"}),
                                ("a rate over 100", {"lower": "900000000", "upper": "",
                                                     "percent": "120"}),
                                ("a negative lower bound", {"lower": "-100", "upper": "0",
                                                            "percent": "5"})):
                self.refused(f"a band with {label} is refused", po.post(band_url, data))

        # ── 6. codes are one case ──
        self.section("6. statutory codes ignore case")
        nssf = StatutoryRule.objects.get(company=company, code="NSSF")
        lower = {"code": "nssf", "name": "Pension (lower case)", "method": "PERCENT",
                 "basis": "GROSS", "employee_percent": "6", "employer_percent": "6",
                 "employee_flat": "0", "employer_flat": "0", "basis_cap": "120000",
                 "monthly_relief": "0", "reduces_taxable_pay": "on",
                 "payable_account": str(nssf.payable_account_id),
                 "expense_account": str(nssf.expense_account_id),
                 "effective_from": f"{nssf.effective_from:{ISO}}", "is_active": "on"}
        self.refused("'nssf' on the same start day as NSSF is refused as a duplicate",
                     po.post(rule_url, lower), "already")
        resp = po.post(rule_url, dict(lower, effective_from="2026-11-01"))
        saved = StatutoryRule.objects.filter(company=company,
                                             effective_from="2026-11-01").first()
        self.expect("'nssf' from November is stored as a dated NSSF change",
                    resp.status_code == 302 and saved is not None and saved.code == "NSSF",
                    str(saved and saved.code))
        codes = [r.code for r in services.rules_as_at(company, datetime.date(2026, 11, 30))]
        self.expect("and November charges one pension, not two",
                    codes.count("NSSF") == 1 and "nssf" not in codes, str(codes))

        # ── 2c. a lost tax table is refused, not paid untaxed ──
        resp = po.post(reverse("payroll:rule_toggle", args=[paye.pk]))
        resp, sep = self.make_run(po, "PR-SEP", "2026-09-01", "2026-09-30")
        said = self.said(po, resp)
        self.expect("with the old PAYE retired, September refuses to calculate",
                    sep is not None and not sep.payslips.exists()
                    and "income-tax table" in said, said)
        po.post(reverse("payroll:rule_toggle", args=[paye.pk]))
        resp = po.post(reverse("payroll:run_calculate", args=[sep.pk]))
        slip = sep.payslips.filter(employee=self.high).first()
        self.expect("re-activated, September taxes the pay again",
                    slip is not None and slip.lines.filter(code="PAYE").exists()
                    and slip.net_salary == D("103618.15"),
                    str(slip and slip.net_salary))

        # ── 1. cancelling a run whose entry was already reversed ──
        self.section("1. cancelling a run already reversed in the journal")
        fm.post(reverse("payroll:run_approve", args=[sep.pk]))
        fm.post(reverse("payroll:run_post", args=[sep.pk]))
        sep.refresh_from_db()
        entry = sep.journal_entry
        if self.expect("September is posted", entry is not None and sep.status == "POSTED",
                       sep.status):
            first_reversal = entry.reverse(user=self.users["FM"])
            reversals = JournalEntry.objects.filter(
                company=company, reference__startswith="JE-REV-").count()
            resp = fm.post(reverse("payroll:run_cancel", args=[sep.pk]), {"reason": "x"})
            sep.refresh_from_db()
            said = self.said(fm, resp)
            self.expect("the cancel is not a server error", resp.status_code == 302,
                        f"HTTP {resp.status_code}")
            self.expect("the run is stood down", sep.status == "CANCELLED", sep.status)
            self.expect("and nothing is reversed a second time",
                        JournalEntry.objects.filter(
                            company=company, reference__startswith="JE-REV-").count()
                        == reversals, said)
            self.expect("the message names the reversal that already answered it",
                        first_reversal.reference in said, said)
            resp, sep2 = self.make_run(po, "PR-SEP-2", "2026-09-01", "2026-09-30")
            self.expect("September can be run again",
                        sep2 is not None and sep2.payslips.exists(), self.said(po, resp))

        # ── 7. a cancellation dated into a closed month ──
        self.section("7. cancelling into a closed period warns")
        aug.refresh_from_db()
        period = aug.journal_entry.period
        if self.expect("August's entry sits in a period", period is not None):
            period.status = "CLOSED"
            period.save(update_fields=["status"])
            resp = fm.post(reverse("payroll:run_cancel", args=[aug.pk]), {"reason": "redo"})
            aug.refresh_from_db()
            said = self.said(fm, resp)
            self.expect("August is cancelled by a reversal",
                        aug.status == "CANCELLED"
                        and aug.journal_entry.reversed_by_id is not None, aug.status)
            self.expect("and the user is told the reversal is dated in a closed period",
                        "closed" in said.lower() and period.name in said, said)

    # ───────────────────────────── 8. expenses ───────────────────────────────

    def check_expenses(self):
        from apps.expenses.models import ExpenseCategory, ExpenseClaim

        self.section("8. expense claims")
        company = self.company
        emp, fm, fm2 = self.c["EMP"], self.c["FM"], self.c["FM2"]
        self.evans = self.employee("E1", "Evans", user=self.users["EMP"])
        self.other = self.employee("O1", "Other")
        fiona = self.employee("F1", "Fiona", user=self.users["FM"])
        cat = ExpenseCategory.objects.create(company=company, name="Travel")

        def claim(client, who, ref, day="2026-10-01"):
            resp = client.post(reverse("expenses:claim_create"), {
                "reference": ref, "employee": str(who.pk), "claim_date": day, "notes": ""})
            return resp, ExpenseClaim.objects.filter(company=company, reference=ref).first()

        def line(client, c, amount="2500"):
            return client.post(reverse("expenses:claim_add_line", args=[c.pk]), {
                "category": str(cat.pk), "description": "Taxi",
                "expense_date": "2026-09-30", "amount": amount})

        page = emp.get(reverse("expenses:claim_create"))
        offered = list(page.context["form"].fields["employee"].queryset) if page.context else []
        self.expect("the employee's claim form offers only their own name",
                    page.status_code == 200 and offered == [self.evans], str(offered))
        resp, bad = claim(emp, self.other, "EXP-FOR-OTHER")
        self.expect("an employee cannot claim in a colleague's name",
                    resp.status_code == 200 and bad is None, f"HTTP {resp.status_code}")
        resp, own = claim(emp, self.evans, "EXP-OWN")
        if not self.expect("an employee can claim for themselves", own is not None,
                           self.errors(resp)):
            return
        resp, others = claim(fm2, self.other, "EXP-OTHER")
        self.expect("finance can raise a claim for anybody", others is not None,
                    self.errors(resp))
        listed = [c.reference for c in emp.get(reverse("expenses:claim_list")).context["claims"]]
        self.expect("the employee's claim list shows only their own",
                    listed == ["EXP-OWN"], str(listed))
        self.expect("a colleague's claim is out of the employee's sight",
                    emp.get(reverse("expenses:claim_detail", args=[others.pk])).status_code
                    == 404)
        line(emp, others, "999")
        self.expect("and they cannot add a line to it", not others.lines.exists())

        line(emp, own)
        emp.post(reverse("expenses:claim_submit", args=[own.pk]))
        emp.post(reverse("expenses:claim_approve", args=[own.pk]))
        own.refresh_from_db()
        self.expect("an employee cannot approve their own claim",
                    own.status == "SUBMITTED", own.status)
        fm.post(reverse("expenses:claim_approve", args=[own.pk]))
        own.refresh_from_db()
        self.expect("finance approves it", own.status == "APPROVED", own.status)
        page = emp.get(reverse("expenses:claim_detail", args=[own.pk]))
        self.expect("the claimant's page offers no pay button on their own claim",
                    page.status_code == 200 and b"Pay &amp; post" not in page.content,
                    f"HTTP {page.status_code}")
        # Changed deliberately: the approver may not also pay a claim, so the
        # pay form is offered to a second finance manager, not to Fiona.
        page = fm.get(reverse("expenses:claim_detail", args=[own.pk]))
        self.expect("the approver's page does not offer the pay button",
                    page.status_code == 200 and b"Pay &amp; post" not in page.content,
                    f"HTTP {page.status_code}")
        page = fm2.get(reverse("expenses:claim_detail", args=[own.pk]))
        self.expect("another finance manager's page offers it, with the payment date",
                    page.status_code == 200 and b'name="payment_date"' in page.content,
                    f"HTTP {page.status_code}")

        resp = emp.post(reverse("expenses:claim_edit", args=[own.pk]), {
            "reference": "EXP-CHANGED", "employee": str(self.evans.pk),
            "claim_date": "2026-01-01", "notes": "edited"})
        own.refresh_from_db()
        self.expect("an approved claim cannot be edited",
                    resp.status_code == 302 and own.reference == "EXP-OWN"
                    and own.claim_date == datetime.date(2026, 10, 1), own.reference)
        self.expect("the edit screen of an approved claim turns the user away",
                    fm.get(reverse("expenses:claim_edit", args=[own.pk])).status_code == 302)

        resp = fm2.post(reverse("expenses:claim_pay", args=[own.pk]),
                        {"bank_account": "not-a-uuid"})
        own.refresh_from_db()
        self.expect("a mangled bank account is a message, not a server error",
                    resp.status_code == 302 and own.status == "APPROVED",
                    f"HTTP {resp.status_code}")
        resp = fm.post(reverse("expenses:claim_pay", args=[own.pk]),
                       {"bank_account": "", "payment_date": "2026-10-20"})
        own.refresh_from_db()
        self.expect("the person who approved it cannot pay it",
                    own.status == "APPROVED" and "approved" in self.said(fm, resp),
                    own.status)
        resp = fm2.post(reverse("expenses:claim_pay", args=[own.pk]),
                        {"bank_account": "", "payment_date": "2026-09-30"})
        own.refresh_from_db()
        self.expect("a payment dated before the claim is refused",
                    own.status == "APPROVED" and "claimed on" in self.said(fm2, resp),
                    own.status)
        resp = fm2.post(reverse("expenses:claim_pay", args=[own.pk]),
                        {"bank_account": "",
                         "payment_date": f"{datetime.date.today() + datetime.timedelta(days=3):{ISO}}"})
        own.refresh_from_db()
        self.expect("a payment dated in the future is refused",
                    own.status == "APPROVED" and "future" in self.said(fm2, resp),
                    own.status)
        resp = fm2.post(reverse("expenses:claim_pay", args=[own.pk]),
                        {"bank_account": "", "payment_date": "2026-10-20"})
        own.refresh_from_db()
        entry = own.journal_entry
        self.expect("the claim is paid", own.status == "PAID" and entry is not None,
                    own.status)
        if entry:
            self.expect("the entry is dated the day the money left, not the claim date",
                        entry.posting_date == datetime.date(2026, 10, 20)
                        and own.paid_at == datetime.date(2026, 10, 20)
                        and own.claim_date == datetime.date(2026, 10, 1),
                        f"entry {entry.posting_date}, claim {own.claim_date}")
        emp.post(reverse("expenses:claim_edit", args=[own.pk]), {
            "reference": "EXP-CHANGED", "employee": str(self.evans.pk),
            "claim_date": "2026-01-01", "notes": ""})
        own.refresh_from_db()
        self.expect("a paid claim cannot be edited either", own.reference == "EXP-OWN")

        resp, mine = claim(fm, fiona, "EXP-FM")
        line(fm, mine, "50000")
        fm.post(reverse("expenses:claim_submit", args=[mine.pk]))
        resp = fm.post(reverse("expenses:claim_approve", args=[mine.pk]))
        mine.refresh_from_db()
        self.expect("a finance manager cannot approve their own claim",
                    mine.status == "SUBMITTED" and "your own claim" in self.said(fm, resp),
                    mine.status)
        fm2.post(reverse("expenses:claim_approve", args=[mine.pk]))
        fm.post(reverse("expenses:claim_pay", args=[mine.pk]))
        mine.refresh_from_db()
        self.expect("nor pay it once a colleague has approved it",
                    mine.status == "APPROVED", mine.status)
        fm2.post(reverse("expenses:claim_pay", args=[mine.pk]))
        mine.refresh_from_db()
        self.expect("nor can the colleague who approved it",
                    mine.status == "APPROVED", mine.status)
        self.c["ADMIN"].post(reverse("expenses:claim_pay", args=[mine.pk]))
        mine.refresh_from_db()
        self.expect("a third person can pay it", mine.status == "PAID", mine.status)

    # ───────────────────────────── 9. leave ──────────────────────────────────

    def check_leave(self):
        from apps.hr.models import LeaveRequest, LeaveType

        self.section("9. leave")
        emp, hr = self.c["EMP"], self.c["HR"]
        # The company now arrives with the standard Kenyan leave types, and
        # its Annual leave carries unused days forward; these checks need a
        # plain 21 days with nothing carried, so they use a type of their own.
        annual = LeaveType.objects.create(company=self.company, name="Annual (test)",
                                          days_per_year=21)

        def ask(client, who, start, end):
            return client.post(reverse("hr:leave_create"), {
                "employee": str(who.pk), "leave_type": str(annual.pk),
                "start_date": start, "end_date": end, "notes": ""})

        page = emp.get(reverse("hr:leave_create"))
        offered = list(page.context["form"].fields["employee"].queryset) if page.context else []
        self.expect("the employee's leave form offers only their own name",
                    page.status_code == 200 and offered == [self.evans], str(offered))
        self.refused("an employee cannot file leave for a colleague",
                     ask(emp, self.other, "2026-11-02", "2026-11-06"))
        self.refused("leave that ends before it starts is refused",
                     ask(emp, self.evans, "2026-11-10", "2026-11-01"), "before it starts")
        self.refused("fourteen months against 21 days is refused",
                     ask(emp, self.evans, "2026-11-01", "2027-12-31"), "left")
        resp = ask(emp, self.evans, "2026-11-02", "2026-11-06")
        self.expect("five days of their own leave is accepted", resp.status_code == 302,
                    self.errors(resp))
        # Changed deliberately: leave is counted in working days now. 1-31
        # December 2026 is 23 working days (it was 1-20 December, 20 calendar
        # days, which is only 14 working days and would now fit).
        self.refused("23 more working days in the same year is beyond the entitlement",
                     ask(emp, self.evans, "2026-12-01", "2026-12-31"), "left")
        self.expect("nothing with a negative length was stored",
                    not LeaveRequest.objects.filter(employee=self.evans,
                                                    start_date="2026-11-10").exists())

        resp = ask(hr, self.other, "2026-11-02", "2026-11-03")
        theirs = LeaveRequest.objects.filter(employee=self.other).first()
        self.expect("HR can file leave for anybody", theirs is not None, self.errors(resp))
        if theirs:
            self.expect("an employee cannot open a colleague's leave",
                        emp.get(reverse("hr:leave_detail", args=[theirs.pk])).status_code
                        == 404)
        listed = list(emp.get(reverse("hr:leave_list")).context["leaves"])
        self.expect("the employee's leave list is their own",
                    listed and all(r.employee_id == self.evans.pk for r in listed),
                    str(len(listed)))
        listed = list(self.c["VIEW"].get(reverse("hr:leave_list")).context["leaves"])
        self.expect("a login with no employee record sees nobody's leave",
                    listed == [], str(len(listed)))

        own = LeaveRequest.objects.get(employee=self.evans)
        self.expect("the employee's own leave and HR's view of it both render",
                    emp.get(reverse("hr:leave_detail", args=[own.pk])).status_code == 200
                    and hr.get(reverse("hr:leave_detail", args=[own.pk])).status_code == 200)
        emp.post(reverse("hr:leave_approve", args=[own.pk]), {"action": "approve"})
        own.refresh_from_db()
        self.expect("an employee cannot approve their own leave",
                    own.status == "PENDING", own.status)
        hr.post(reverse("hr:leave_approve", args=[own.pk]), {"action": "approve"})
        own.refresh_from_db()
        self.expect("HR approves it", own.status == "APPROVED", own.status)

    # ───────────────────────────── 12. timesheets ────────────────────────────

    def check_timesheets(self):
        from apps.timesheets.models import TimesheetEntry

        self.section("12. timesheet hours")
        emp, hr = self.c["EMP"], self.c["HR"]
        url = reverse("timesheets:entry_create")

        def log(hours, day="2026-10-20", note=None):
            return emp.post(url, {"project": "", "task": "", "entry_date": day,
                                  "hours": hours, "description": note or f"h{hours}",
                                  "billable": "on"})

        self.expect("the time entry form renders",
                    emp.get(url).status_code == 200)
        for hours in ("0", "-5", "25", "999.99"):
            self.refused(f"{hours} hours in a day is refused", log(hours))
        resp = log("8")
        self.expect("eight hours is accepted", resp.status_code == 302, self.errors(resp))
        log("10", day="2026-10-21", note="day-one")
        self.refused("a second entry taking the day past 24 hours is refused",
                     log("20", day="2026-10-21", note="day-two"), "more than the day")
        self.refused("a day more than a year ahead is refused", log("8", day="2099-01-01"))
        self.refused("a day before 2000 is refused", log("8", day="1999-12-31"))
        stored = TimesheetEntry.objects.filter(company=self.company,
                                               user=self.users["EMP"])
        self.expect("only the sensible entries were stored",
                    sorted(str(e.hours) for e in stored) == ["10.00", "8.00"],
                    str([str(e.hours) for e in stored]))

        entry = stored.get(hours=D("8"))
        emp.post(reverse("timesheets:entry_submit", args=[entry.pk]))
        emp.post(reverse("timesheets:entry_approve", args=[entry.pk]))
        entry.refresh_from_db()
        self.expect("nobody approves their own hours", entry.status == "SUBMITTED",
                    entry.status)
        hr.post(reverse("timesheets:entry_approve", args=[entry.pk]))
        entry.refresh_from_db()
        self.expect("a colleague can", entry.status == "APPROVED", entry.status)

    # ─────────────────── second round: history, pro-rating, leavers ──────────

    def contract(self, employee, basic, start, end="", current=True, pk=None):
        """Write a contract through HR's own screen, as HR would."""
        data = {"employee": str(employee.pk), "contract_type": "PERMANENT",
                "job_title": "Staff", "department": "", "start_date": start,
                "end_date": end, "basic_salary": str(basic),
                "is_current": "on" if current else ""}
        url = (reverse("hr:contract_update", args=[pk]) if pk
               else reverse("hr:contract_create", args=[employee.pk]))
        return self.c["HR"].post(url, data)

    def basic_of(self, run, employee):
        slip = run.payslips.filter(employee=employee).first() if run else None
        return slip.basic_pay() if slip else None

    def check_contract_history(self):
        from apps.hr.models import Contract
        from apps.payroll.models import SalaryComponent

        company = self.company
        hr, po, fm = self.c["HR"], self.c["PO"], self.c["FM"]
        # 2027 is clear of the runs the first round raised, and the statutory
        # table (from October 2026) is in force all year.
        raised = self.employee("RB1", "Raised")
        leaver = self.employee("LD1", "Leaver")
        self.contract(raised, 50000, "2025-01-01")
        self.contract(leaver, 40000, "2025-01-01")

        self.section("15. re-running a cancelled month uses the contracts of the time")
        resp, aug = self.make_run(po, "PR-27-08", "2027-08-01", "2027-08-31")
        self.expect("August 2027 pays both at their August rates",
                    self.basic_of(aug, raised) == D("50000.00")
                    and self.basic_of(aug, leaver) == D("40000.00"),
                    f"{self.basic_of(aug, raised)} / {self.basic_of(aug, leaver)}")
        if aug:
            fm.post(reverse("payroll:run_cancel", args=[aug.pk]), {"reason": "re-run"})

        self.section("13. pro-rating by calendar days")
        joiner = self.employee("JN1", "Joiner")
        self.contract(joiner, 30000, "2027-09-15")
        self.contract(raised, 60000, "2027-09-16")
        old = Contract.objects.filter(employee=raised, basic_salary=D("50000")).first()
        self.expect("a raise ends the contract it supersedes the day before it starts",
                    old is not None and old.end_date == datetime.date(2027, 9, 15)
                    and not old.is_current,
                    str(old and (old.end_date, old.is_current)))
        theirs = Contract.objects.get(employee=leaver)
        self.contract(leaver, 40000, "2025-01-01", end="2027-09-15", current=False,
                      pk=theirs.pk)
        future = self.employee("FL1", "LeavesNov")
        self.contract(future, 30000, "2025-01-01", end="2027-11-15")

        self.section("14. an ended contract is history, not an error")
        lapsed = self.employee("XL1", "Lapsed")
        resp = self.contract(lapsed, 30000, "2025-01-01", end="2026-01-31", current=True)
        saved = Contract.objects.filter(employee=lapsed).first()
        self.expect("a contract that ended in the past saves even when ticked current",
                    resp.status_code == 302 and saved is not None,
                    f"HTTP {resp.status_code} {self.errors(resp)}")
        self.expect("and is stored as not current",
                    saved is not None and not saved.is_current)

        # The people the statutory and termination checks need, all on the books
        # before September is worked out.
        low = self.employee("LW1", "LowPay")
        self.contract(low, 8000, "2025-01-01")
        mileage = self.employee("MI1", "Mileage")
        self.contract(mileage, 100000, "2025-01-01")
        pension = self.employee("PN1", "Pension")
        self.contract(pension, 100000, "2025-01-01")
        leaving = self.employee("TM1", "Terminated")
        self.contract(leaving, 30000, "2025-01-01")
        self.contract(self.evans, 50000, "2027-09-01")
        hana = self.employee("HN1", "Hana", user=self.users["HR"])
        self.contract(hana, 40000, "2027-09-01")

        self.section("23. reimbursements and tax-deductible deductions")
        comp_url = reverse("payroll:component_create")
        resp = po.post(comp_url, {
            "employee": str(mileage.pk), "name": "Mileage", "component_type": "ALLOWANCE",
            "calculation": "FIXED", "amount": "20000", "percent": "0", "is_taxable": "",
            "counts_as_statutory_pay": "False", "gl_account": "", "is_active": "on"})
        self.expect("a mileage reimbursement is saved as not statutory pay",
                    SalaryComponent.objects.filter(
                        company=company, name="Mileage",
                        counts_as_statutory_pay=False).exists(), self.errors(resp))
        resp = po.post(comp_url, {
            "employee": str(pension.pk), "name": "Private pension",
            "component_type": "DEDUCTION", "calculation": "FIXED", "amount": "5000",
            "percent": "0", "is_taxable": "", "reduces_taxable_pay": "on",
            "gl_account": str(self.account("2600").pk), "is_active": "on"})
        self.expect("a private pension deduction is saved as reducing taxable pay",
                    SalaryComponent.objects.filter(
                        company=company, name="Private pension",
                        reduces_taxable_pay=True).exists(), self.errors(resp))
        self.refused("an allowance cannot reduce taxable pay", po.post(comp_url, {
            "employee": str(pension.pk), "name": "Odd allowance",
            "component_type": "ALLOWANCE", "calculation": "FIXED", "amount": "100",
            "percent": "0", "is_taxable": "on", "reduces_taxable_pay": "on",
            "gl_account": "", "is_active": "on"}), "earning")
        po.post(comp_url, {
            "employee": str(low.pk), "name": "Lunch", "component_type": "ALLOWANCE",
            "calculation": "FIXED", "amount": "1", "percent": "0", "is_taxable": "on",
            "gl_account": "", "is_active": ""})
        lunch = SalaryComponent.objects.filter(company=company, name="Lunch").first()
        self.expect("an element posted without the statutory flag still counts as pay",
                    lunch is not None and lunch.counts_as_statutory_pay)

        resp, rerun = self.make_run(po, "PR-27-08R", "2027-08-01", "2027-08-31")
        self.expect("the re-run August still pays the raised employee 50,000",
                    self.basic_of(rerun, raised) == D("50000.00"),
                    str(self.basic_of(rerun, raised)))
        self.expect("and the leaver 40,000, from a contract that is no longer current",
                    self.basic_of(rerun, leaver) == D("40000.00"),
                    str(self.basic_of(rerun, leaver)))

        resp, sep = self.make_run(po, "PR-27-09", "2027-09-01", "2027-09-30")
        said = self.said(po, resp)
        if not self.expect("September 2027 is raised",
                           sep is not None and sep.payslips.exists(), said):
            return
        self.expect("a joiner from 15 September at 30,000 is paid 16,000",
                    self.basic_of(sep, joiner) == D("16000.00"),
                    str(self.basic_of(sep, joiner)))
        slip = sep.payslips.get(employee=joiner)
        nssf = slip.lines.filter(code="NSSF", kind="DEDUCTION").first()
        self.expect("and the pension is worked on the pro-rated pay",
                    nssf is not None and nssf.basis == D("16000.00")
                    and nssf.amount == D("960.00"), str(nssf and (nssf.basis, nssf.amount)))
        self.expect("the slip says how the pay was pro-rated",
                    "16 of 30 days" in slip.lines.get(code="BASIC").description)
        slip = sep.payslips.get(employee=raised)
        self.expect("a raise from 16 September pays 50,000 then 60,000: 55,000",
                    slip.basic_pay() == D("55000.00")
                    and slip.lines.filter(code="BASIC").count() == 2,
                    f"{slip.basic_pay()} on {slip.lines.filter(code='BASIC').count()} line(s)")
        self.expect("a leaver whose contract ended 15 September is paid 20,000",
                    self.basic_of(sep, leaver) == D("20000.00"),
                    str(self.basic_of(sep, leaver)))
        self.expect("and is not named as left off the run", "Leaver" not in said, said)

        self.section("24. the SHIF minimum")
        slip = sep.payslips.get(employee=low)
        shif = slip.lines.filter(code="SHIF", kind="DEDUCTION").first()
        self.expect("8,000 of pay pays SHIF of 300, not 2.75% of it (220)",
                    shif is not None and shif.amount == D("300.00"),
                    str(shif and shif.amount))

        slip = sep.payslips.get(employee=mileage)
        bases = {l.code: l.basis for l in slip.lines.filter(kind="DEDUCTION")}
        self.expect("mileage is paid but left out of the NSSF, SHIF and housing base",
                    slip.gross_salary == D("120000.00")
                    and bases.get("NSSF") == D("100000.00")
                    and bases.get("SHIF") == D("100000.00")
                    and bases.get("HOUSING") == D("100000.00"),
                    f"gross {slip.gross_salary}, bases {bases}")
        slip = sep.payslips.get(employee=pension)
        paye = slip.lines.filter(code="PAYE").first()
        self.expect("a private pension comes off the pay before PAYE",
                    slip.taxable_pay == D("84750.00")
                    and paye is not None and paye.basis == D("84750.00"),
                    f"taxable {slip.taxable_pay}, PAYE on {paye and paye.basis}")
        fm.post(reverse("payroll:run_approve", args=[sep.pk]))
        sep.refresh_from_db()

        self.section("16. terminating an employee")
        url = reverse("hr:employee_terminate", args=[leaving.pk])
        self.expect("the terminate screen opens for HR", hr.get(url).status_code == 200)
        self.expect("and not for an employee",
                    self.c["EMP"].post(url, {"date_terminated": "2027-10-15"}).status_code
                    == 403)
        self.refused("a leaving date before the hire date is refused",
                     hr.post(url, {"date_terminated": "2020-01-01", "status": "TERMINATED"}),
                     "hired")
        resp = hr.post(url, {"date_terminated": "2027-10-15", "status": "TERMINATED"})
        leaving.refresh_from_db()
        ended = Contract.objects.get(employee=leaving)
        self.expect("termination sets the status and the leaving date",
                    resp.status_code == 302 and leaving.status == "TERMINATED"
                    and leaving.date_terminated == datetime.date(2027, 10, 15),
                    f"HTTP {resp.status_code}, {leaving.status} {leaving.date_terminated}")
        self.expect("and ends the contract on that day",
                    ended.end_date == datetime.date(2027, 10, 15), str(ended.end_date))
        resp, octr = self.make_run(po, "PR-27-10", "2027-10-01", "2027-10-31")
        self.expect("October pays the leaver 15 of 31 days of 30,000: 14,516.13",
                    self.basic_of(octr, leaving) == D("14516.13"),
                    str(self.basic_of(octr, leaving)))
        resp, nov = self.make_run(po, "PR-27-11", "2027-11-01", "2027-11-30")
        said = self.said(po, resp)
        self.expect("November does not pay them",
                    nov is not None and not nov.payslips.filter(employee=leaving).exists())
        self.expect("nor names them as missing a contract", "Terminated" not in said, said)
        self.expect("a contract ending 15 November at 30,000 pays 15,000",
                    self.basic_of(nov, future) == D("15000.00"),
                    str(self.basic_of(nov, future)))
        past = self.employee("TM2", "PastLeaver")
        self.contract(past, 30000, "2025-01-01")
        hr.post(reverse("hr:employee_terminate", args=[past.pk]),
                {"date_terminated": "2026-10-10", "status": "RESIGNED"})
        gone = Contract.objects.get(employee=past)
        self.expect("a leaving date in the past takes the current flag off the contract",
                    gone.end_date == datetime.date(2026, 10, 10) and not gone.is_current,
                    str((gone.end_date, gone.is_current)))

        self.section("22. the payment date belongs to the period")
        resp, bad = self.make_run(po, "PR-PD1", "2027-12-01", "2027-12-31",
                                  payment_date="2019-01-01")
        self.refused("a December 2027 run paid in 2019 is refused", resp, "before the pay")
        resp, bad = self.make_run(po, "PR-PD2", "2027-12-01", "2027-12-31",
                                  payment_date="2028-03-01")
        self.refused("a payment date 61 days after the period is refused", resp, "60 days")
        resp, ok = self.make_run(po, "PR-PD3", "2027-12-01", "2027-12-31",
                                 payment_date="2028-02-29")
        self.expect("60 days after the period is accepted", ok is not None,
                    self.errors(resp))

        self.section("20. my payslips")
        from django.test import RequestFactory
        from apps.payroll.views import MyPayslipListView

        def own(user):
            # Called directly: the role table has to open this page to the
            # EMPLOYEE role (see the report), but the filtering is the view's.
            request = RequestFactory().get(reverse("payroll:my_payslips"))
            request.user, request.active_company = user, company
            view = MyPayslipListView()
            view.setup(request)
            return list(view.get_queryset())

        mine = own(self.users["EMP"])
        self.expect("an employee's own payslips are theirs alone, from approved runs",
                    mine and [s.employee_id for s in mine] == [self.evans.pk]
                    and all(s.payroll_run.status in ("APPROVED", "POSTED") for s in mine),
                    str([(s.employee.full_name, s.payroll_run.reference) for s in mine]))
        self.expect("a login with no employee record has none", own(self.users["VIEW"]) == [])
        page = hr.get(reverse("payroll:my_payslips"))
        listed = list(page.context["payslips"]) if page.context else []
        self.expect("the page shows HR only their own slip, not the company's",
                    page.status_code == 200 and [s.employee_id for s in listed] == [hana.pk],
                    f"HTTP {page.status_code}, {len(listed)} slip(s)")
        other_slip = sep.payslips.get(employee=raised)
        self.expect("somebody else's payslip is a 404 on the own-payslip page",
                    hr.get(reverse("payroll:my_payslip_detail", args=[other_slip.pk]))
                    .status_code == 404)
        self.expect("their own opens",
                    bool(listed) and hr.get(reverse("payroll:my_payslip_detail",
                                                    args=[listed[0].pk])).status_code == 200)

    def check_rule_toggle(self):
        from apps.payroll.models import StatutoryRule

        self.section("17. re-activating a second tiered table")
        paye = StatutoryRule.objects.filter(company=self.company, code="PAYE").first()
        second = StatutoryRule.objects.create(
            company=self.company, code="TAX2", name="Second tiers", method="BANDS",
            payable_account=paye.payable_account, effective_from=datetime.date(2027, 1, 1),
            is_active=False)
        resp = self.c["PO"].post(reverse("payroll:rule_toggle", args=[second.pk]))
        second.refresh_from_db()
        said = self.said(self.c["PO"], resp)
        self.expect("a retired second tiered table cannot be switched back on",
                    not second.is_active and "tiered table" in said, said)
        self.c["PO"].post(reverse("payroll:rule_toggle", args=[paye.pk]))
        self.c["PO"].post(reverse("payroll:rule_toggle", args=[paye.pk]))
        paye.refresh_from_db()
        self.expect("retiring and re-activating PAYE itself still works", paye.is_active)

    def check_leave_year_end(self):
        from apps.hr.models import LeaveType

        self.section("18. leave across the year end")
        annual = LeaveType.objects.get(company=self.company, name="Annual (test)")

        def ask(start, end):
            return self.c["EMP"].post(reverse("hr:leave_create"), {
                "employee": str(self.evans.pk), "leave_type": str(annual.pk),
                "start_date": start, "end_date": end, "notes": ""})

        # 5 days of 2026 are already granted. 21 Dec to 8 Jan is 9 working days
        # of 2026 and 6 of 2027 (changed deliberately: leave is counted Monday
        # to Friday now, so the dates below are working-day counts).
        resp = ask("2026-12-21", "2027-01-08")
        self.expect("leave over the year end fits each year's allowance",
                    resp.status_code == 302, self.errors(resp))
        self.refused("16 more working days of 2027 beside those 6 is beyond 2027's 21",
                     ask("2027-02-01", "2027-02-22"), "2027")
        resp = ask("2027-02-01", "2027-02-19")
        self.expect("15 more working days of 2027 is exactly the allowance",
                    resp.status_code == 302,
                    self.errors(resp))

    def check_hours_privacy(self):
        from apps.timesheets.models import TimesheetEntry

        self.section("19. whose hours are shown")
        self.c["HR"].post(reverse("timesheets:entry_create"), {
            "project": "", "task": "", "entry_date": "2026-10-22", "hours": "6",
            "description": "hr-own", "billable": "on"})
        theirs = TimesheetEntry.objects.filter(company=self.company,
                                               description="hr-own").first()
        emp = self.c["EMP"]
        listed = list(emp.get(reverse("timesheets:entry_list")).context["entries"])
        self.expect("an employee's list shows only their own hours",
                    listed and all(e.user_id == self.users["EMP"].pk for e in listed),
                    str(len(listed)))
        home = emp.get(reverse("timesheets:home")).context
        self.expect("and so does their home page",
                    all(e.user_id == self.users["EMP"].pk for e in home["recent_entries"]))
        self.expect("a colleague's entry is a 404 to them",
                    theirs is not None
                    and emp.get(reverse("timesheets:entry_detail", args=[theirs.pk]))
                    .status_code == 404)
        for key in ("HR", "PM"):
            users = {e.user_id for e in
                     self.c[key].get(reverse("timesheets:entry_list")).context["entries"]}
            self.expect(f"{key} sees everybody's hours",
                        self.users["EMP"].pk in users and self.users["HR"].pk in users,
                        str(len(users)))
        listed = list(self.c["VIEW"].get(reverse("timesheets:entry_list")).context["entries"])
        self.expect("a viewer with no hours of their own sees none", listed == [])

    def check_employee_edit(self):
        from apps.accounts.models import User

        self.section("21. editing an employee and linking a login")
        hr = self.c["HR"]
        other = self.other
        url = reverse("hr:employee_update", args=[other.pk])
        outsider = self.user("outsider", "Olly", "Outsider")
        page = hr.get(url)
        offered = set(page.context["form"].fields["user"].queryset) if page.context else set()
        self.expect("the login list is this company's members not already linked",
                    self.users["VIEW"] in offered and self.users["PM"] in offered
                    and self.users["EMP"] not in offered and outsider not in offered,
                    f"HTTP {page.status_code}, {len(offered)} offered")
        self.expect("an employee cannot open the edit screen",
                    self.c["EMP"].get(url).status_code == 403)
        base = {"employee_number": other.employee_number, "first_name": other.first_name,
                "last_name": other.last_name, "other_names": "", "gender": "",
                "date_of_birth": "", "date_hired": f"{other.date_hired:{ISO}}",
                "status": "ACTIVE", "department": "", "branch": "", "job_title": "Staff",
                "reports_to": "", "user": "", "national_id": "", "kra_pin": "",
                "nssf_number": "", "nhif_number": "", "phone": "", "email": "",
                "bank_name": "", "bank_account_number": "", "notes": ""}
        self.refused("a login from outside the company is refused",
                     hr.post(url, dict(base, user=str(outsider.pk))))
        self.refused("leaving is not a status the edit screen sets",
                     hr.post(url, dict(base, status="TERMINATED")), "Terminate")
        resp = hr.post(url, dict(base, user=str(self.users["VIEW"].pk), job_title="Clerk"))
        other.refresh_from_db()
        self.expect("HR links a member's login and edits the record",
                    resp.status_code == 302 and other.user_id == self.users["VIEW"].pk
                    and other.job_title == "Clerk",
                    f"HTTP {resp.status_code} {self.errors(resp)}")
        self.expect("the outsider was never linked",
                    not User.objects.filter(pk=outsider.pk, employee__isnull=False).exists())

    # ─────────────────────────── third round (25-44) ─────────────────────────

    @staticmethod
    def paye(taxable, fraction=D("1")):
        """PAYE by hand, independent of the code under test: the Kenyan steps
        and relief, each scaled by the share of the month a run covers."""
        def m(v):
            return D(v).quantize(D("0.01"))
        tax = D("0")
        for lo, hi, pct in ((0, 24000, 10), (24000, 32333, 25), (32333, 500000, 30),
                            (500000, 800000, D("32.5")), (800000, None, 35)):
            lo = D(lo) * fraction
            top = None if hi is None else D(hi) * fraction
            if taxable <= lo:
                break
            ceiling = taxable if top is None else min(taxable, top)
            tax += (ceiling - lo) * D(pct) / 100
        return max(m(tax) - m(D("2400") * fraction), D("0"))

    def line(self, slip, code, kind="DEDUCTION"):
        return slip.lines.filter(code=code, kind=kind).first() if slip else None

    def check_round_three(self):
        from apps.accounting.models import JournalEntry
        from apps.expenses.models import ExpenseCategory, ExpenseClaim
        from apps.hr.models import Contract, LeaveRequest, LeaveType
        from apps.payroll.models import PayrollRun, SalaryComponent

        company = self.company
        hr, po, fm, fm2 = self.c["HR"], self.c["PO"], self.c["FM"], self.c["FM2"]
        admin, emp = self.c["ADMIN"], self.c["EMP"]
        comp_url = reverse("payroll:component_create")
        comp = {"employee": "", "name": "", "component_type": "ALLOWANCE",
                "calculation": "FIXED", "amount": "0", "percent": "0",
                "is_taxable": "on", "gl_account": "", "is_active": "on"}

        # The people the October 2026 run needs, all on the books first.
        split = self.employee("SP1", "Split")
        self.contract(split, 80000, "2025-01-01")
        po.post(comp_url, dict(comp, employee=str(split.pk), name="House",
                               amount="10000"))
        joiner = self.employee("JO2", "Joiner2")
        self.contract(joiner, 31000, "2026-10-16")
        po.post(comp_url, dict(comp, employee=str(joiner.pk), name="Transport",
                               amount="3100"))
        po.post(comp_url, dict(comp, employee=str(joiner.pk), name="Loan",
                               component_type="DEDUCTION", amount="1000", is_taxable="",
                               gl_account=str(self.account("2700").pk)))
        big = self.employee("BP1", "BigPension")
        self.contract(big, 600000, "2025-01-01")
        po.post(comp_url, dict(comp, employee=str(big.pk), name="Pension scheme",
                               component_type="DEDUCTION", amount="40000", is_taxable="",
                               reduces_taxable_pay="on",
                               gl_account=str(self.account("2600").pk)))
        back = self.employee("BD1", "Backdate")
        self.contract(back, 50000, "2025-01-01")
        moved2 = self.employee("MV2", "MovedTwo")
        self.contract(moved2, 40000, "2025-01-01")
        hr.post(reverse("hr:employee_terminate", args=[moved2.pk]),
                {"date_terminated": "2026-10-10", "status": "TERMINATED"})

        self.section("27. allowances pro-rate with basic; a loan repayment does not")
        loan = SalaryComponent.objects.filter(company=company, name="Loan").first()
        transport = SalaryComponent.objects.filter(company=company, name="Transport").first()
        self.expect("an allowance posted without the flag is pro-rated, a deduction is not",
                    transport is not None and transport.prorate_part_months
                    and loan is not None and not loan.prorate_part_months)

        self.section("31. pay elements are booked to the right kind of account")
        self.refused("an allowance charged to a liability is refused",
                     po.post(comp_url, dict(comp, name="Odd1", amount="100",
                                            gl_account=str(self.account("2600").pk))),
                     "expense account")
        self.refused("a deduction owed to an expense account is refused",
                     po.post(comp_url, dict(comp, name="Odd2", amount="100",
                                            component_type="DEDUCTION",
                                            gl_account=str(self.account("6100").pk))),
                     "liability")
        self.refused("an allowance charged to the bank is refused",
                     po.post(comp_url, dict(comp, name="Odd3", amount="100",
                                            gl_account=str(self.account("1200").pk))))
        bank_allow = self.employee("BA1", "BankAllow")
        self.contract(bank_allow, 30000, "2025-01-01")
        sneaky = SalaryComponent.objects.create(
            company=company, employee=bank_allow, name="Sneaky", component_type="ALLOWANCE",
            amount=D("1000"), gl_account=self.account("1200"))
        resp, nov = self.make_run(po, "PR-26-11", "2026-11-01", "2026-11-30")
        if self.expect("November 2026 is raised", nov is not None and nov.payslips.exists(),
                       self.said(po, resp)):
            fm.post(reverse("payroll:run_approve", args=[nov.pk]))
            resp = fm.post(reverse("payroll:run_post", args=[nov.pk]))
            nov.refresh_from_db()
            said = self.said(fm, resp)
            self.expect("posting refuses an allowance booked to the bank",
                        nov.journal_entry_id is None and "expense account" in said, said)
            fm.post(reverse("payroll:run_cancel", args=[nov.pk]), {"reason": "test"})
        sneaky.delete()

        self.section("25. a run lies within one calendar month")
        self.refused("a run from 20 January to 10 February is refused",
                     self.make_run(po, "PR-X2M", "2028-01-20", "2028-02-10")[0],
                     "calendar month")

        self.section("26-28. October 2026, worked out as a share of the month")
        resp, octr = self.make_run(po, "PR-26-10", "2026-10-01", "2026-10-31",
                                   payment_date="2026-11-05")
        if not self.expect("October 2026 is raised", octr is not None and octr.payslips.exists(),
                           self.said(po, resp)):
            return
        slip = octr.payslips.get(employee=split)
        paye = self.line(slip, "PAYE")
        self.expect("80,000 basic and a 10,000 allowance tax as one month: 16,615.85 PAYE",
                    slip.gross_salary == D("90000.00") and slip.taxable_pay == D("80775.00")
                    and paye is not None and paye.amount == self.paye(D("80775"))
                    == D("16615.85"),
                    f"gross {slip.gross_salary}, taxable {slip.taxable_pay}, "
                    f"PAYE {paye and paye.amount}")
        slip = octr.payslips.get(employee=joiner)
        allowance = slip.lines.filter(component__name="Transport").first()
        loan_line = slip.lines.filter(component__name="Loan").first()
        self.expect("a joiner from 16 October pays 16 of 31 days: 16,000 basic",
                    slip.basic_pay() == D("16000.00")
                    and "16 of 31 days" in slip.lines.get(code="BASIC").description,
                    str(slip.basic_pay()))
        self.expect("and 16/31 of a 3,100 allowance: 1,600",
                    allowance is not None and allowance.amount == D("1600.00"),
                    str(allowance and allowance.amount))
        self.expect("while the 1,000 loan repayment is taken in full",
                    loan_line is not None and loan_line.amount == D("1000.00"),
                    str(loan_line and loan_line.amount))
        slip = octr.payslips.get(employee=big)
        nssf = self.line(slip, "NSSF")
        scheme = slip.lines.filter(component__name="Pension scheme").first()
        self.expect("NSSF 6,480 plus a 40,000 scheme are relieved only up to 30,000",
                    scheme is not None and scheme.tax_relief == D("30000.00")
                    and nssf is not None and nssf.tax_relief == D("0.00")
                    and slip.taxable_pay == D("544500.00"),
                    f"scheme relief {scheme and scheme.tax_relief}, NSSF relief "
                    f"{nssf and nssf.tax_relief}, taxable {slip.taxable_pay}")
        self.expect("the whole 40,000 is still deducted",
                    scheme is not None and scheme.amount == D("40000.00"))

        self.section("29. a back-dated contract")
        self.refused("an open-ended 2024 contract over the 2025 one is refused",
                     self.contract(back, 45000, "2024-01-01"), "one contract pays")
        resp = self.contract(back, 45000, "2024-01-01", end="2024-12-31", current=True)
        old = Contract.objects.filter(employee=back, start_date="2024-01-01").first()
        live = Contract.objects.get(employee=back, start_date="2025-01-01")
        self.expect("a 2024 contract that ends before 2025 saves as history, not current",
                    resp.status_code == 302 and old is not None and not old.is_current
                    and live.is_current and live.end_date is None,
                    f"HTTP {resp.status_code} {self.errors(resp)}")
        self.refused("a later contract that is not current may not overlap either",
                     self.contract(back, 52000, "2026-06-01", current=False), "same days")

        self.section("33. approval notices an out-of-date draft")
        resp = fm.post(reverse("payroll:run_approve", args=[octr.pk]))
        octr.refresh_from_db()
        said = self.said(fm, resp)
        self.expect("a contract added after the run was worked out stops approval",
                    octr.status == "DRAFT" and "recalculate" in said, said)
        po.post(reverse("payroll:run_calculate", args=[octr.pk]))
        fm.post(reverse("payroll:run_approve", args=[octr.pk]))
        octr.refresh_from_db()
        self.expect("recalculated, it is approved", octr.status == "APPROVED", octr.status)

        self.section("35. recalculating a settled run")
        resp = po.post(reverse("payroll:run_calculate", args=[octr.pk]))
        said = self.said(po, resp)
        self.expect("the message says it was not recalculated, not 'saved as a draft'",
                    "not recalculated" in said and "saved as a draft" not in said, said)

        self.section("34. the payroll entry is dated on the period end")
        fm.post(reverse("payroll:run_post", args=[octr.pk]))
        octr.refresh_from_db()
        entry = octr.journal_entry
        self.expect("October paid on 5 November is booked on 31 October",
                    entry is not None and entry.posting_date == datetime.date(2026, 10, 31),
                    str(entry and entry.posting_date))

        self.section("30. a contract that paid an approved run is locked")
        url = reverse("hr:contract_update", args=[live.pk])
        data = {"employee": str(back.pk), "contract_type": "PERMANENT", "job_title": "Staff",
                "department": "", "start_date": "2025-01-01", "end_date": "",
                "basic_salary": "50000", "is_current": "on"}
        for label, change in (("its pay", {"basic_salary": "55000"}),
                              ("its start date", {"start_date": "2025-02-01"}),
                              ("an end date inside a paid month", {"end_date": "2026-10-15"}),
                              ("its employee", {"employee": str(split.pk)})):
            self.refused(f"changing {label} is refused", hr.post(url, dict(data, **change)),
                         "approved or posted")
        live.refresh_from_db()
        self.expect("and nothing changed", live.basic_salary == D("50000.00")
                    and live.employee_id == back.pk and live.end_date is None)
        self.refused("a raise from inside the settled month is refused",
                     self.contract(back, 60000, "2026-10-15"), "settled month")
        resp = self.contract(back, 60000, "2026-11-01")
        live.refresh_from_db()
        self.expect("a raise from the next month is a new contract that ends the old one",
                    resp.status_code == 302 and live.end_date == datetime.date(2026, 10, 31),
                    f"HTTP {resp.status_code} {self.errors(resp)} {live.end_date}")

        self.section("32. correcting a leaving date")
        url = reverse("hr:employee_terminate", args=[moved2.pk])
        resp = hr.post(url, {"date_terminated": "2026-10-20", "status": "TERMINATED"})
        moved2.refresh_from_db()
        self.refused("a later leaving date inside an approved month is refused", resp,
                     "cancel that run")
        self.expect("and the leaving date is unchanged",
                    moved2.date_terminated == datetime.date(2026, 10, 10))
        moved3 = self.employee("MV3", "MovedThree")
        self.contract(moved3, 40000, "2025-01-01")
        url = reverse("hr:employee_terminate", args=[moved3.pk])
        hr.post(url, {"date_terminated": "2027-03-10", "status": "TERMINATED"})
        resp = hr.post(url, {"date_terminated": "2027-03-20", "status": "TERMINATED"})
        said = self.said(hr, resp)
        ended = Contract.objects.get(employee=moved3)
        self.expect("a later leaving date moves the contract end out to it",
                    ended.end_date == datetime.date(2027, 3, 20), str(ended.end_date))
        self.expect("and the message says the date was corrected",
                    "corrected" in said and "ends that day" in said, said)

        self.section("26. two half-month runs add up to one month")
        resp, h1 = self.make_run(po, "PR-26-12A", "2026-12-01", "2026-12-15")
        resp2, h2 = self.make_run(po, "PR-26-12B", "2026-12-16", "2026-12-31")
        if self.expect("both halves of December are raised",
                       h1 is not None and h2 is not None
                       and h1.payslips.exists() and h2.payslips.exists(),
                       f"{self.said(po, resp)} / {self.said(po, resp2)}"):
            a, b = h1.payslips.get(employee=split), h2.payslips.get(employee=split)
            house = [s.lines.get(component__name="House").amount for s in (a, b)]
            self.expect("the basic halves are 15/31 and 16/31 and add up to 80,000",
                        a.basic_pay() == D("38709.68") and b.basic_pay() == D("41290.32")
                        and a.basic_pay() + b.basic_pay() == D("80000.00"),
                        f"{a.basic_pay()} + {b.basic_pay()}")
            self.expect("and the allowance halves add up to 10,000",
                        sum(house) == D("10000.00"), str(house))
            paye = self.line(a, "PAYE").amount + self.line(b, "PAYE").amount
            self.expect("the two halves withhold the month's PAYE, to the cent or two",
                        abs(paye - D("16615.85")) <= D("0.05"), str(paye))
            self.expect("the slip says the tax steps were applied for 15 of 31 days",
                        "15 of 31 days" in self.line(a, "PAYE").description,
                        self.line(a, "PAYE").description)
            self.expect("each half's PAYE matches the hand calculation",
                        self.line(a, "PAYE").amount
                        == self.paye(a.taxable_pay, D(15) / D(31))
                        and self.line(b, "PAYE").amount
                        == self.paye(b.taxable_pay, D(16) / D(31)),
                        f"{self.line(a, 'PAYE').amount} / {self.line(b, 'PAYE').amount}")

        self.section("36. a junk run filter")
        for junk in ("junk", "99999999999999999999", "2026-02-30"):
            resp = po.get(reverse("payroll:payslip_list") + f"?run={junk}")
            self.expect(f"?run={junk} is a page, not a server error",
                        resp.status_code == 200 and len(resp.context["payslips"]) == 0,
                        f"HTTP {resp.status_code}")

        self.section("37. duplicates are form errors, not server errors")
        base = {"other_names": "", "gender": "", "date_of_birth": "", "date_hired": "2025-01-01",
                "status": "ACTIVE", "department": "", "branch": "", "job_title": "Staff",
                "reports_to": "", "national_id": "", "kra_pin": "", "nssf_number": "",
                "nhif_number": "", "phone": "", "email": "", "bank_name": "",
                "bank_account_number": "", "notes": "", "first_name": "Dup",
                "last_name": "Test"}
        self.refused("a typed staff number already in use is refused on create",
                     hr.post(reverse("hr:employee_create"), dict(base, employee_number="H1")),
                     "already")
        self.refused("and on edit",
                     hr.post(reverse("hr:employee_update", args=[split.pk]),
                             dict(base, employee_number="H1", user="",
                                  first_name="Split")), "already")
        self.refused("a second 'House' for the same person is refused",
                     po.post(comp_url, dict(comp, employee=str(split.pk), name="House",
                                            amount="5")), "already")
        po.post(comp_url, dict(comp, name="Canteen", amount="500"))
        self.refused("a second company-wide 'Canteen' is refused",
                     po.post(comp_url, dict(comp, name="Canteen", amount="600")), "already")
        SalaryComponent.objects.filter(company=company, name="Canteen").update(is_active=False)
        self.refused("a second 'Travel' expense category is refused",
                     fm.post(reverse("expenses:category_create"),
                             {"name": "travel", "gl_account": ""}), "already")

        self.section("38. three people for one expense claim")
        cat = ExpenseCategory.objects.get(company=company, name="Travel")
        fm2.post(reverse("expenses:claim_create"), {
            "reference": "EXP-SOD", "employee": str(self.other.pk),
            "claim_date": "2026-10-01", "notes": ""})
        claim = ExpenseClaim.objects.filter(company=company, reference="EXP-SOD").first()
        if self.expect("finance raises a claim for a colleague, recorded as theirs",
                       claim is not None and claim.created_by_id == self.users["FM2"].pk):
            fm2.post(reverse("expenses:claim_add_line", args=[claim.pk]), {
                "category": str(cat.pk), "description": "Taxi",
                "expense_date": "2026-09-30", "amount": "1200"})
            fm2.post(reverse("expenses:claim_submit", args=[claim.pk]))
            resp = fm2.post(reverse("expenses:claim_approve", args=[claim.pk]))
            claim.refresh_from_db()
            self.expect("the person who raised it cannot approve it",
                        claim.status == "SUBMITTED" and "raised" in self.said(fm2, resp),
                        claim.status)
            fm.post(reverse("expenses:claim_approve", args=[claim.pk]))
            fm.post(reverse("expenses:claim_pay", args=[claim.pk]))
            fm2.post(reverse("expenses:claim_pay", args=[claim.pk]))
            claim.refresh_from_db()
            self.expect("neither the approver nor the creator can pay it",
                        claim.status == "APPROVED", claim.status)
            admin.post(reverse("expenses:claim_pay", args=[claim.pk]))
            claim.refresh_from_db()
            entry = claim.journal_entry
            self.expect("a third person pays it, and is recorded",
                        claim.status == "PAID" and claim.paid_by_id == self.users["ADMIN"].pk,
                        claim.status)
            self.expect("the journal's own Reverse button refuses the claim's entry",
                        entry is not None and entry.source_document_type == "ExpenseClaim"
                        and bool(entry.generic_reversal_refusal()),
                        entry and entry.generic_reversal_refusal())

        self.section("39. HR does not approve leave it filed")
        theirs = LeaveRequest.objects.filter(employee=self.other, status="PENDING").first()
        if self.expect("the leave HR filed for a colleague is waiting", theirs is not None):
            self.expect("and records who filed it", theirs.created_by_id == self.users["HR"].pk)
            resp = hr.post(reverse("hr:leave_approve", args=[theirs.pk]), {"action": "approve"})
            theirs.refresh_from_db()
            self.expect("HR cannot approve the request it filed",
                        theirs.status == "PENDING" and "You filed" in self.said(hr, resp),
                        theirs.status)

        self.section("43-44. buttons a role cannot use, and who decides leave")
        paye_rule = self.line(octr.payslips.get(employee=split), "PAYE").rule
        toggle = reverse("payroll:rule_toggle", args=[paye_rule.pk]).encode()
        self.expect("HR is not shown the retire button on a table",
                    toggle not in hr.get(reverse("payroll:rule_detail",
                                                 args=[paye_rule.pk])).content)
        self.expect("the payroll officer is",
                    toggle in po.get(reverse("payroll:rule_detail",
                                             args=[paye_rule.pk])).content)
        seed = reverse("payroll:rules_seed").encode()
        self.expect("HR is not shown 'Load standard table'",
                    seed not in hr.get(reverse("payroll:rule_list")).content)
        if theirs:
            approve = reverse("hr:leave_approve", args=[theirs.pk]).encode()
            self.expect("the payroll officer sees the leave but no approve button",
                        approve not in po.get(reverse("hr:leave_list")).content)
            self.expect("the administrator sees the approve button",
                        approve in admin.get(reverse("hr:leave_list")).content)
            self.expect("a payroll officer cannot decide leave",
                        po.post(reverse("hr:leave_approve", args=[theirs.pk]),
                                {"action": "approve"}).status_code == 403)
            admin.post(reverse("hr:leave_approve", args=[theirs.pk]), {"action": "approve"})
            theirs.refresh_from_db()
            self.expect("somebody else in charge of the staff can", theirs.status == "APPROVED")
        staff = reverse("hr:employee_list").encode()
        self.expect("an employee's leave page does not link to the staff list",
                    staff not in emp.get(reverse("hr:leave_list")).content)
        self.expect("nor does their expenses page",
                    staff not in emp.get(reverse("expenses:home")).content)

        self.section("40. leave types")
        seeded = set(LeaveType.objects.filter(company=company).values_list("name", flat=True))
        self.expect("the new company arrived with the standard Kenyan leave types",
                    {"Annual", "Sick", "Maternity", "Paternity", "Compassionate"} <= seeded,
                    str(sorted(seeded)))
        self.expect("HR opens the leave types", hr.get(reverse("hr:leavetype_list"))
                    .status_code == 200)
        self.expect("an employee cannot", emp.get(reverse("hr:leavetype_list"))
                    .status_code == 403)
        form = {"name": "Study", "days_per_year": "10", "is_paid": "on",
                "requires_approval": "on", "carry_forward": "", "max_carry_forward_days": "0",
                "is_active": "on"}
        self.expect("a payroll officer cannot add one",
                    po.post(reverse("hr:leavetype_create"), form).status_code == 403)
        resp = hr.post(reverse("hr:leavetype_create"), form)
        study = LeaveType.objects.filter(company=company, name="Study").first()
        self.expect("HR adds a leave type", resp.status_code == 302 and study is not None,
                    self.errors(resp))
        self.refused("a second 'annual' is refused",
                     hr.post(reverse("hr:leavetype_create"), dict(form, name="annual")),
                     "already")
        if study:
            resp = hr.post(reverse("hr:leavetype_update", args=[study.pk]),
                           dict(form, days_per_year="12"))
            study.refresh_from_db()
            self.expect("and edits it", study.days_per_year == 12, self.errors(resp))

        self.section("41. carry-forward is last year's unused days, capped")
        annual = LeaveType.objects.get(company=company, name="Annual")

        def took(person, start, end):
            LeaveRequest.objects.create(employee=person, leave_type=annual, start_date=start,
                                        end_date=end, status="APPROVED")

        def entitled(person, year=2026):
            return LeaveRequest(employee=person, leave_type=annual,
                                start_date=datetime.date(year, 6, 1),
                                end_date=datetime.date(year, 6, 1)).entitlement(year)

        cf1, cf2 = self.employee("CF1", "CarryFive"), self.employee("CF2", "CarryTwo")
        took(cf1, datetime.date(2025, 3, 3), datetime.date(2025, 3, 24))   # 16 days
        took(cf2, datetime.date(2025, 3, 3), datetime.date(2025, 3, 27))   # 19 days
        cf3 = self.employee("CF3", "Newcomer")
        cf3.date_hired = datetime.date(2026, 2, 1)
        cf3.save()
        self.expect("5 unused days are carried, capped at 5: 26",
                    entitled(cf1) == 26, str(entitled(cf1)))
        self.expect("2 unused days carry only 2: 23", entitled(cf2) == 23, str(entitled(cf2)))
        self.expect("a new joiner carries nothing: 21", entitled(cf3) == 21, str(entitled(cf3)))

        def ask(who, start, end, client=hr, kind=annual):
            return client.post(reverse("hr:leave_create"), {
                "employee": str(who.pk), "leave_type": str(kind.pk),
                "start_date": start, "end_date": end, "notes": ""})

        resp = ask(cf2, "2026-06-01", "2026-06-26")
        self.expect("20 working days of 23 are granted", resp.status_code == 302,
                    self.errors(resp))
        self.refused("4 more are beyond the 23", ask(cf2, "2026-07-06", "2026-07-09"), "left")
        resp = ask(cf2, "2026-07-06", "2026-07-08")
        self.expect("3 more is exactly the 23", resp.status_code == 302, self.errors(resp))

        self.section("42. working days, and no day asked twice")
        five = LeaveRequest.objects.filter(employee=self.evans,
                                           start_date="2026-11-02").first()
        self.expect("Monday to Friday is 5 days", five is not None and five.days_requested == 5)
        self.refused("a weekend-only request is refused",
                     ask(cf1, "2026-11-14", "2026-11-15"), "weekend")
        self.refused("days already asked for cannot be asked again",
                     ask(self.evans, "2026-11-04", "2026-11-05", client=emp), "already has")
