"""Reporting views — read-only reports that query existing app data."""
import datetime
from decimal import Decimal

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView


def _parse_dates(request):
    today = datetime.date.today()
    first_of_month = today.replace(day=1)
    date_from = request.GET.get("date_from") or str(first_of_month)
    date_to = request.GET.get("date_to") or str(today)
    try:
        date_from = datetime.date.fromisoformat(date_from)
    except (ValueError, TypeError):
        date_from = first_of_month
    try:
        date_to = datetime.date.fromisoformat(date_to)
    except (ValueError, TypeError):
        date_to = today
    return date_from, date_to


class ReportingIndexView(LoginRequiredMixin, TemplateView):
    template_name = "reporting/index.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Reports"
        return ctx


class TrialBalanceView(LoginRequiredMixin, TemplateView):
    template_name = "reporting/trial_balance.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Trial Balance"
        company = self.request.active_company
        if not company:
            return ctx
        from apps.accounting.models import Account
        from django.db.models import Sum

        date_from, date_to = _parse_dates(self.request)
        # A retired account that moved money inside the window still has to
        # appear, otherwise the two sides of the trial balance disagree.
        accounts = Account.objects.filter(company=company).order_by("code")
        rows = []
        total_dr = Decimal("0")
        total_cr = Decimal("0")
        for acc in accounts:
            lines = acc.journal_lines.filter(
                journal_entry__status="POSTED",
                journal_entry__posting_date__gte=date_from,
                journal_entry__posting_date__lte=date_to,
            )
            dr = lines.aggregate(t=Sum("debit_amount"))["t"] or Decimal("0")
            cr = lines.aggregate(t=Sum("credit_amount"))["t"] or Decimal("0")
            if dr > 0 or cr > 0:
                rows.append({"account": acc, "debit": dr, "credit": cr,
                             "retired": not acc.is_active})
                total_dr += dr
                total_cr += cr
        ctx.update({
            "rows": rows,
            "total_debit": total_dr,
            "total_credit": total_cr,
            "is_balanced": abs(total_dr - total_cr) < Decimal("0.01"),
            "date_from": date_from,
            "date_to": date_to,
        })
        return ctx


class ProfitLossView(LoginRequiredMixin, TemplateView):
    template_name = "reporting/profit_loss.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Profit & Loss"
        company = self.request.active_company
        if not company:
            return ctx

        from apps.accounting.models import Account
        from django.db.models import Sum

        date_from, date_to = _parse_dates(self.request)

        def _get_balance(account_types):
            rows = []
            total = Decimal("0")
            accounts = Account.objects.filter(company=company, account_type__in=account_types).order_by("code")
            for acc in accounts:
                # Closing entries zero the profit and loss accounts into equity;
                # the statement still reports what the period earned, so it
                # reads the activity and leaves the carry-out to the ledger.
                lines = acc.journal_lines.filter(
                    journal_entry__status="POSTED",
                    journal_entry__posting_date__gte=date_from,
                    journal_entry__posting_date__lte=date_to,
                ).exclude(journal_entry__entry_type="CLOSING")
                dr = lines.aggregate(t=Sum("debit_amount"))["t"] or Decimal("0")
                cr = lines.aggregate(t=Sum("credit_amount"))["t"] or Decimal("0")
                bal = cr - dr  # Revenue/COGS = credit normal
                if bal != 0:
                    rows.append({"account": acc, "balance": bal})
                    total += bal
            return rows, total

        rev_rows, total_revenue = _get_balance(["REVENUE"])
        cogs_rows, total_cogs = _get_balance(["COST_OF_GOODS"])
        exp_rows, total_expenses = _get_balance(["EXPENSE"])

        gross_profit = total_revenue + total_cogs  # cogs is negative
        net_profit = gross_profit + total_expenses  # expenses are negative

        ctx.update({
            "date_from": date_from,
            "date_to": date_to,
            "revenue_rows": rev_rows,
            "total_revenue": total_revenue,
            "cogs_rows": cogs_rows,
            "total_cogs": abs(total_cogs),
            "gross_profit": gross_profit,
            "expense_rows": exp_rows,
            "total_expenses": abs(total_expenses),
            "net_profit": net_profit,
        })
        return ctx


class BalanceSheetView(LoginRequiredMixin, TemplateView):
    template_name = "reporting/balance_sheet.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Balance Sheet"
        company = self.request.active_company
        if not company:
            return ctx

        from apps.accounting.models import Account, INCOME_STATEMENT_TYPES
        from django.db.models import Sum

        _, date_to = _parse_dates(self.request)

        def _get_section(account_types, flip=False):
            rows = []
            total = Decimal("0")
            accounts = Account.objects.filter(company=company, account_type__in=account_types).order_by("code")
            for acc in accounts:
                lines = acc.journal_lines.filter(
                    journal_entry__status="POSTED",
                    journal_entry__posting_date__lte=date_to,
                )
                dr = lines.aggregate(t=Sum("debit_amount"))["t"] or Decimal("0")
                cr = lines.aggregate(t=Sum("credit_amount"))["t"] or Decimal("0")
                bal = (cr - dr) if flip else (dr - cr)
                if bal != 0:
                    rows.append({"account": acc, "balance": bal})
                    total += bal
            return rows, total

        asset_rows, total_assets = _get_section(["ASSET", "CASH", "BANK", "RECEIVABLE"])
        liab_rows, total_liab = _get_section(["LIABILITY", "PAYABLE", "TAX"], flip=True)
        equity_rows, total_equity = _get_section(["EQUITY"], flip=True)

        # The balancing figure is the year's own profit. Until a period is
        # closed it still sits in the revenue and expense accounts, so it has
        # to be carried onto the equity side or the sheet cannot balance.
        pl_rows, current_year_profit = _get_section(list(INCOME_STATEMENT_TYPES), flip=True)

        # The equity section total the sheet prints must foot above the fold:
        # the reserve accounts plus the year's own profit beneath them.
        equity_section = total_equity + current_year_profit

        ctx.update({
            "date_to": date_to,
            "asset_rows": asset_rows,
            "total_assets": total_assets,
            "liability_rows": liab_rows,
            "total_liabilities": total_liab,
            "equity_rows": equity_rows,
            "total_equity": equity_section,
            "current_year_profit": current_year_profit,
            "shows_profit": bool(pl_rows),
            "total_liab_equity": total_liab + equity_section,
            "balancing_error": total_assets - (total_liab + equity_section),
            "is_balanced": abs(total_assets - (total_liab + equity_section))
            < Decimal("0.01"),
        })
        return ctx


class AgedDebtorsView(LoginRequiredMixin, TemplateView):
    template_name = "reporting/aged_debtors.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Aged Debtors"
        company = self.request.active_company
        if not company:
            return ctx

        from apps.sales.models import Invoice
        today = datetime.date.today()

        invoices = Invoice.objects.filter(
            company=company,
            status__in=["SENT", "PARTIAL", "OVERDUE"],
        ).select_related("customer").order_by("customer__name", "due_date")

        buckets = {"0-30": [], "31-60": [], "61-90": [], "90+": []}
        totals = {"0-30": Decimal("0"), "31-60": Decimal("0"), "61-90": Decimal("0"), "90+": Decimal("0")}

        for inv in invoices:
            days = (today - inv.due_date).days
            # Bucketed in the book's money: a dollar owed and a shilling owed
            # are not one receivable, however the papers read.
            outstanding = inv.outstanding_base
            if inv.outstanding_amount <= 0:
                continue
            if days <= 30:
                bucket = "0-30"
            elif days <= 60:
                bucket = "31-60"
            elif days <= 90:
                bucket = "61-90"
            else:
                bucket = "90+"
            buckets[bucket].append(inv)
            totals[bucket] += outstanding

        ctx.update({
            "buckets": buckets,
            "totals": totals,
            "grand_total": sum(totals.values()),
            "book": (company.currency or "").upper(),
            "today": today,
        })
        return ctx


class AgedCreditorsView(LoginRequiredMixin, TemplateView):
    template_name = "reporting/aged_creditors.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Aged Creditors"
        company = self.request.active_company
        if not company:
            return ctx

        from apps.procurement.models import Bill
        today = datetime.date.today()

        bills = Bill.objects.filter(
            company=company,
            status__in=["RECEIVED", "PARTIAL", "OVERDUE"],
        ).select_related("supplier").order_by("supplier__name", "due_date")

        buckets = {"0-30": [], "31-60": [], "61-90": [], "90+": []}
        totals = {"0-30": Decimal("0"), "31-60": Decimal("0"), "61-90": Decimal("0"), "90+": Decimal("0")}

        for bill in bills:
            days = (today - bill.due_date).days
            outstanding = bill.outstanding_base
            if bill.outstanding_amount <= 0:
                continue
            if days <= 30:
                bucket = "0-30"
            elif days <= 60:
                bucket = "31-60"
            elif days <= 90:
                bucket = "61-90"
            else:
                bucket = "90+"
            buckets[bucket].append(bill)
            totals[bucket] += outstanding

        ctx.update({
            "buckets": buckets,
            "totals": totals,
            "grand_total": sum(totals.values()),
            "book": (company.currency or "").upper(),
            "today": today,
        })
        return ctx


class CashFlowView(LoginRequiredMixin, TemplateView):
    template_name = "reporting/cash_flow.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Cash Flow"
        company = self.request.active_company
        if not company:
            return ctx

        date_from, date_to = _parse_dates(self.request)
        from apps.accounting.models import Account
        from django.db.models import Sum

        # Cash in: payments received (credit side of cash/bank accounts from customer receipts)
        cash_accounts = Account.objects.filter(company=company, account_type__in=["CASH", "BANK"], is_active=True)
        total_in = Decimal("0")
        total_out = Decimal("0")
        for acc in cash_accounts:
            lines = acc.journal_lines.filter(
                journal_entry__status="POSTED",
                journal_entry__posting_date__gte=date_from,
                journal_entry__posting_date__lte=date_to,
            )
            total_in += lines.aggregate(t=Sum("debit_amount"))["t"] or Decimal("0")
            total_out += lines.aggregate(t=Sum("credit_amount"))["t"] or Decimal("0")

        ctx.update({
            "date_from": date_from,
            "date_to": date_to,
            "total_in": total_in,
            "total_out": total_out,
            "net_cash_flow": total_in - total_out,
        })
        return ctx
