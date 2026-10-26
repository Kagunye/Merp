"""Who may act for whom, in the people side of the book.

Leave, expense claims and timesheets are all a person asking the company for
something — days off, money back, hours paid. The person asking and the person
saying yes have to be two different people, and somebody whose job is not
running the staff may only ask for themselves: a clerk who can file leave in a
colleague's name, or claim money back on their behalf, can move days and cash
around without the colleague ever knowing.

The role is read from the company membership, the same row that decides which
book a login can open at all.
"""

# The roles whose job is the staff: they may raise, see and decide requests for
# anybody in the company.
PEOPLE_ROLES = {"ADMIN", "HR_MANAGER", "PAYROLL_OFFICER"}

# The roles whose job is the money: they may raise, see, approve and pay an
# expense claim for anybody in the company, because paying claims is finance's
# work.
FINANCE_ROLES = {"ADMIN", "FINANCE_MANAGER", "ACCOUNTANT", "PAYROLL_OFFICER"}


# The roles whose job is other people's hours: HR, and the project managers who
# approve time against their projects. Everybody else logs and sees their own —
# a colleague's timesheet says where they were and for how long, which is not
# a clerk's business.
HOURS_ROLES = {"ADMIN", "HR_MANAGER", "PROJECT_MANAGER"}


def role_of(user, company):
    """The user's role in this company, or None when they have no membership."""
    if not user or not user.is_authenticated or company is None:
        return None
    return user.get_company_role(company)


def _is_superuser(user):
    return bool(user and user.is_authenticated
                and (user.is_superuser or getattr(user, "is_system_admin", False)))


def manages_people(user, company):
    """May this user raise and decide leave, and see everybody's?"""
    return _is_superuser(user) or role_of(user, company) in PEOPLE_ROLES


def manages_claims(user, company):
    """May this user raise, see and settle expense claims for anybody?"""
    return _is_superuser(user) or role_of(user, company) in (FINANCE_ROLES | PEOPLE_ROLES)


def sees_all_hours(user, company):
    """May this user see everybody's timesheet entries, not only their own?"""
    return _is_superuser(user) or role_of(user, company) in HOURS_ROLES


def own_employee(user, company):
    """The employee record this login belongs to in this company, or None.

    A login with no employee record is not anybody on the staff list, so it has
    nothing of its own to see — it must never fall through to everybody's.
    """
    if not user or not user.is_authenticated or company is None:
        return None
    from .models import Employee
    return Employee.objects.filter(user=user, company=company).first()
