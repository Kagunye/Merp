"""Bring every company database up to date (after `migrate` on the shared one).

    python manage.py migrate_companies

Companies that keep their data in the shared database need nothing; each one
with its own database (Company.own_database) is migrated in turn. One that
cannot be reached is reported and the rest carry on; the exit status is 1 if
any failed.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run migrations on every company's own database."

    def handle(self, *args, **opts):
        from apps.core import tenancy
        from apps.organizations.models import Company

        failed = 0
        companies = list(Company.objects.using("default").filter(own_database=True))
        if not companies:
            self.stdout.write("No company keeps its data in its own database.")
            return
        for company in companies:
            try:
                alias = tenancy.register(company)
                tenancy.migrate(alias, verbosity=max(0, opts["verbosity"] - 1), stdout=self.stdout)
                tenancy.sync_company_rows(company, alias, force=True)
                self.stdout.write(self.style.SUCCESS(f"  {company}: up to date"))
            except Exception as exc:
                failed += 1
                self.stderr.write(self.style.ERROR(f"  {company}: {type(exc).__name__}: {exc}"))
        if failed:
            raise SystemExit(1)
