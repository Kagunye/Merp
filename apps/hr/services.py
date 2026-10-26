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

    Returns a list of plain-English warnings about runs that already paid the
    person beyond their last day: those runs are settled, so they are put right
    by cancelling and re-running, never by editing them here.
    """
    from apps.payroll.models import Payslip

    employee.status = status
    employee.date_terminated = last_day
    employee.save(update_fields=["status", "date_terminated", "updated_at"])

    for contract in employee.contracts.filter(start_date__lte=last_day).filter(
            Q(end_date__isnull=True) | Q(end_date__gt=last_day)):
        contract.end_date = last_day
        # Contract.save takes the current flag off a contract that has ended.
        contract.save()

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
