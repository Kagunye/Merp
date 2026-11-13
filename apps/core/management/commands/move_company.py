"""Move one company's data into a database of its own, or back.

    python manage.py move_company "Acme Ltd" --url postgres://u:p@host:5432/acme
    python manage.py move_company "Acme Ltd" --env ACME_DATABASE_URL
    python manage.py move_company "Acme Ltd" --back
    python manage.py move_company "Acme Ltd" --url ... --dry-run   # count only

The target is migrated, the company's rows are copied as they are, and the
copy is checked -- rows table by table, journal lines and debit/credit totals
account by account -- before the company is switched over; a copy that does
not match is rolled back. The URL is stored encrypted on the company (with
TENANT_DB_KEY, else SECRET_KEY); with --env only the variable's name is kept
and the URL itself never touches the database.

The company's rows stay in the shared database as a snapshot unless
--purge-source is given (branches and departments always stay: memberships
point at them). --back removes those stale rows before copying back.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q


class Command(BaseCommand):
    help = "Move one company's data into its own database (or --back to the shared one), checked."

    def add_arguments(self, parser):
        parser.add_argument("company", help="Company name, registration number or id")
        parser.add_argument("--url", help="The company database URL (sqlite:///, postgres://, mysql://)")
        parser.add_argument("--env", help="An environment variable holding the URL (only its name is stored)")
        parser.add_argument("--back", action="store_true", help="Move the data back into the shared database")
        parser.add_argument("--purge-source", action="store_true",
                            help="After a checked move out, delete the company's rows from the shared database")
        parser.add_argument("--dry-run", action="store_true", help="Count what would move, change nothing")

    def handle(self, *args, **o):
        from apps.core import tenancy_move as move
        from apps.organizations.models import Company

        key = o["company"]
        q = Q(name__iexact=key) | Q(registration_number__iexact=key)
        try:
            import uuid
            q |= Q(pk=uuid.UUID(key))
        except ValueError:
            pass
        matches = list(Company.objects.using("default").filter(q))
        if len(matches) != 1:
            raise CommandError(f"{len(matches)} companies match {key!r}; give its id.")
        company = matches[0]

        out = self.stdout.write
        try:
            if o["back"]:
                result = move.move_back(company, dry_run=o["dry_run"], out=out)
            else:
                result = move.move_out(company, url=o["url"], env_var=o["env"],
                                       purge_source=o["purge_source"], dry_run=o["dry_run"], out=out)
        except move.MoveError as exc:
            raise CommandError(str(exc))
        for label, n in move.summary(result["owned"]):
            out(f"    {label:<45} {n:>8}")
        if o["dry_run"]:
            out("Dry run: nothing changed.")
            return
        book = move.ledger_totals(move.ledger(company, "default" if o["back"] else result["alias"]))
        out(f"  ledger: {book[0]} journal lines, debits {book[1]}, credits {book[2]} -- match the source")
        where = "the shared database" if o["back"] else "its own database"
        self.stdout.write(self.style.SUCCESS(f"{company} now keeps its data in {where}."))
