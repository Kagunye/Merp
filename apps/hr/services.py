"""HR services: the steps that change more than one record at once."""
from django.db import transaction
from django.db.models import Q


@transaction.atomic
def terminate_employee(employee, last_day, status="TERMINATED"):
    """Record that somebody leaves on `last_day`, and end what pays them.

    Leaving is two facts that have to agree: the employee's status and leaving
    date, and the contract that pays them. Payroll pays the days a contract
    covers up to the leaving date, so the contract in force on that day ends on
    it — pro-rated pay for the last month, and nothing after it. A contract that
    would only have started after the person left never pays anything and is
    left as written, so the history shows it.

    Recording a leaving date a second time corrects the first. A later date
    moves the end of the contract the first date ended out to the new day, so
    the days in between are paid; without that the contract stayed ended on
    the old date and payroll silently paid nothing for days the person did
    work. It is refused when an approved or posted run already covers any of
    those days — that month was settled without them, and is put right by
    cancelling and re-running it first, not by changing the contract under it.

    Returns a list of plain-English warnings about runs that already paid the
    person beyond their last day: those runs are settled, so they are put right
    by cancelling and re-running, never by editing them here.
    """
    import datetime

    from django.core.exceptions import ValidationError

    from apps.payroll.models import APPROVED_RUNS, Payslip, PayrollRun

    previous = employee.date_terminated
    reopened = None
    if previous and last_day > previous:
        later_start = employee.contracts.filter(
            start_date__gt=previous, start_date__lte=last_day).exists()
        reopened = None if later_start else employee.contracts.filter(
            end_date=previous, start_date__lte=previous).order_by("-start_date").first()
        if reopened is not None:
            settled = PayrollRun.objects.filter(
                company_id=employee.company_id, status__in=APPROVED_RUNS,
                period_start__lte=last_day,
                period_end__gte=previous + datetime.timedelta(days=1),
            ).order_by("period_start").first()
            if settled:
                raise ValidationError(
                    f"{settled.reference} ({settled.period_start:%d %b} to "
                    f"{settled.period_end:%d %b %Y}) is "
                    f"{settled.get_status_display().lower()} and was worked out with "
                    f"{employee.full_name} leaving on {previous:%d %b %Y}. Moving the "
                    f"leaving date to {last_day:%d %b %Y} would leave the days in between "
                    "unpaid in a settled month. Cancel that run first, then record the "
                    "new date and run the month again.")

    employee.status = status
    employee.date_terminated = last_day
    employee.save(update_fields=["status", "date_terminated", "updated_at"])

    for contract in employee.contracts.filter(start_date__lte=last_day).filter(
            Q(end_date__isnull=True) | Q(end_date__gt=last_day)):
        contract.end_date = last_day
        # Contract.save takes the current flag off a contract that has ended.
        contract.save()
    if reopened is not None:
        # The contract the earlier leaving date ended now runs to the new one.
        reopened.end_date = last_day
        reopened.save()

    warnings = []
    paid_after = Payslip.objects.filter(
        employee=employee, payroll_run__period_end__gt=last_day,
        payroll_run__status__in=["APPROVED", "POSTED"],
    ).select_related("payroll_run").order_by("payroll_run__period_start")
    for slip in paid_after:
        run = slip.payroll_run
        warnings.append(
            f"{run.reference} ({run.period_start:%d %b} to {run.period_end:%d %b %Y}) is "
            f"{run.get_status_display().lower()} and paid {employee.full_name} for days "
            f"after {last_day:%d %b %Y}. Cancel it and run the month again to pay only "
            "the days worked.")
    return warnings
