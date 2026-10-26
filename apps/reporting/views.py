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
    """Each account's balance as at the end date, with how it got there.

    A trial balance is a list of closing balances: every account, debit or
    credit, as it stood at the close of the last day asked for. Reading only
    the window's postings left out everything brought forward — the bank's
    opening balance, last month's sales — so the "balance" printed for an
    account was its turnover, not what it held. The window is still useful,
    so each row also shows the balance brought forward to the first day and
    the debits and credits posted inside it; opening plus movement is the
    closing figure, and the closing columns are what must agree.
    """
    template_name = "reporting/trial_balance.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Trial Balance"
        company = self.request.active_company
        if not company:
            return ctx
        from apps.accounting.models import JournalEntryLine
        from apps.accounting.models import Account
        from django.db.models import Q, Sum

        date_from, date_to = _parse_dates(self.request)
        ctx.update({"date_from": date_from, "date_to": date_to})
        if date_from > date_to:
            ctx.update({
                "rows": [], "error": (
                    f"The window starts on {date_from:%d %b %Y}, after it ends on "
                    f"{date_to:%d %b %Y}. Swap the two dates."),
                "is_balanced": None,
            })
            return ctx

        # One pass over the posted lines up to the end date, split into what
        # was brought forward and what moved inside the window. A retired
        # account that still holds money has to appear, or the two sides of
        # the trial balance disagree.
        posted = JournalEntryLine.objects.filter(
            account__company=company, journal_entry__status="POSTED",
            journal_entry__posting_date__lte=date_to,
        )
        before = Q(journal_entry__posting_date__lt=date_from)
        inside = Q(journal_entry__posting_date__gte=date_from)
        figures = {
            row["account_id"]: row for row in posted.values("account_id").annotate(
                open_dr=Sum("debit_amount", filter=before),
                open_cr=Sum("credit_amount", filter=before),
                move_dr=Sum("debit_amount", filter=inside),
                move_cr=Sum("credit_amount", filter=inside),
            )
        }
        zero = Decimal("0")
        rows = []
        total_dr = total_cr = zero
        total_move_dr = total_move_cr = zero
        for acc in Account.objects.filter(pk__in=figures).order_by("code"):
            row = figures[acc.pk]
            opening = (row["open_dr"] or zero) - (row["open_cr"] or zero)
            move_dr, move_cr = row["move_dr"] or zero, row["move_cr"] or zero
            closing = opening + move_dr - move_cr
            if not (opening or move_dr or move_cr or closing):
                continue
            rows.append({
                "account": acc, "retired": not acc.is_active,
                # Brought forward, as a signed debit-positive figure.
                "opening": opening,
                "opening_debit": opening if opening > 0 else zero,
                "opening_credit": -opening if opening < 0 else zero,
                "period_debit": move_dr, "period_credit": move_cr,
                # The trial balance proper: the closing balance, on its side.
                "debit": closing if closing > 0 else zero,
                "credit": -closing if closing < 0 else zero,
            })
            total_dr += closing if closing > 0 else zero
            total_cr += -closing if closing < 0 else zero
            total_move_dr += move_dr
            total_move_cr += move_cr
        ctx.update({
            "rows": rows,
            "total_debit": total_dr,
            "total_credit": total_cr,
            "total_period_debit": total_move_dr,
            "total_period_credit": total_move_cr,
            "is_balanced": total_dr == total_cr,
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

        # The accounts are credit-normal in this reading, so costs arrive
        # negative. The statement prints them as the positive charges they are;
        # an unusual credit balance still shows as a negative charge.
        for row in cogs_rows + exp_rows:
            row["balance"] = -row["balance"]

        gross_profit = total_revenue + total_cogs  # cogs is negative
        net_profit = gross_profit + total_expenses  # expenses are negative

        ctx.update({
            "date_from": date_from,
            "date_to": date_to,
            "revenue_rows": rev_rows,
            "total_revenue": total_revenue,
            "cogs_rows": cogs_rows,
            "total_cogs": -total_cogs,
            "gross_profit": gross_profit,
            "expense_rows": exp_rows,
            "total_expenses": -total_expenses,
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

        buckets = {"Current": [], "1-30": [], "31-60": [], "61-90": [], "90+": []}
        totals = {key: Decimal("0") for key in buckets}

        for inv in invoices:
            days = (today - inv.due_date).days
            # Bucketed in the book's money: a dollar owed and a shilling owed
            # are not one receivable, however the papers read.
            outstanding = inv.outstanding_base
            if inv.outstanding_amount <= 0:
                continue
            # Not yet due is its own column: a 45-day term is not an ageing
            # bucket, and a negative count must never be read as overdue.
            if days <= 0:
                bucket = "Current"
            elif days <= 30:
                bucket = "1-30"
            elif days <= 60:
                bucket = "31-60"
            elif days <= 90:
                bucket = "61-90"
            else:
                bucket = "90+"
            buckets[bucket].append(inv)
            totals[bucket] += outstanding

        # Money customers paid that no invoice has taken yet is a credit in AR.
        # It is not owed by anyone, so it is not aged, but leaving it out made
        # this report total more than the control account by exactly that
        # money. It is listed on its own line and taken off for the net.
        from apps.sales.receivables import unapplied_receipts
        on_account = unapplied_receipts(company)
        grand_total = sum(totals.values())
        unapplied_total = sum((row["base"] for row in on_account), Decimal("0"))

        ctx.update({
            "buckets": buckets,
            "totals": totals,
            # The open invoices, aged. `net_total` is the receivable the
            # ledger carries: invoices less money on account, equal to 1300.
            "grand_total": grand_total,
            "unapplied": on_account,
            "unapplied_total": unapplied_total,
            "net_total": grand_total - unapplied_total,
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

        buckets = {"Current": [], "1-30": [], "31-60": [], "61-90": [], "90+": []}
        totals = {key: Decimal("0") for key in buckets}

        for bill in bills:
            days = (today - bill.due_date).days
            outstanding = bill.outstanding_base
            if bill.outstanding_amount <= 0:
                continue
            if days <= 0:
                bucket = "Current"
            elif days <= 30:
                bucket = "1-30"
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
