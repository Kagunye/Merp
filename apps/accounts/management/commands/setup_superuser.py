"""Interactive-or-flag-driven superuser creator.

Usage:
    python manage.py setup_superuser                       # interactive
    python manage.py setup_superuser --email you@you.com --password '***'
    python manage.py setup_superuser --email you@you.com   # prompts for password

Idempotent: if the user exists it is updated and re-granted superuser.
"""
import getpass

from django.core.management.base import BaseCommand

from apps.accounts.models import User


class Command(BaseCommand):
    help = "Create or update a superuser (interactive by default)."

    def add_arguments(self, parser):
        parser.add_argument("--email", help="Admin email (login identifier)")
        parser.add_argument("--password", help="Admin password; omit to be prompted")
        parser.add_argument("--username", default="admin")
        parser.add_argument("--first-name", default="System")
        parser.add_argument("--last-name", default="Admin")
        parser.add_argument(
            "--noinput", "--no-input", dest="noinput", action="store_true",
            help="Error out if --email/--password missing (CI mode)",
        )

    def handle(self, *args, **opts):
        email = (opts.get("email") or "").strip().lower()
        password = opts.get("password") or ""
        username = opts["username"]
        first = opts["first_name"]
        last = opts["last_name"]
        noinput = opts["noinput"]

        if not email:
            if noinput:
                self.stderr.write("Email required with --noinput")
                return
            email = input("Email: ").strip().lower()
        if not password:
            if noinput:
                self.stderr.write("Password required with --noinput")
                return
            password = getpass.getpass("Password: ")
            confirm = getpass.getpass("Password (again): ")
            if password != confirm:
                self.stderr.write("Passwords did not match.")
                return

        user, created = User.objects.update_or_create(
            email=email,
            defaults={
                "username": username,
                "first_name": first,
                "last_name": last,
                "is_staff": True,
                "is_superuser": True,
                "is_system_admin": True,
                "is_active": True,
            },
        )
        user.set_password(password)
        user.save()

        verb = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{verb} superuser: {email}"))
