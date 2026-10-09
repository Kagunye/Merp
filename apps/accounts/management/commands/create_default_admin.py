"""Create / refresh a default admin user from environment variables.

Idempotent: safe to run on every deploy. Reads ADMIN_EMAIL,
ADMIN_PASSWORD, ADMIN_USERNAME, ADMIN_FIRST_NAME and ADMIN_LAST_NAME
from the environment. ADMIN_EMAIL and ADMIN_PASSWORD are REQUIRED — if
either is missing the command no-ops rather than shipping a default
password into the deployment.
"""
import os

from django.core.management.base import BaseCommand
from django.db import IntegrityError

from apps.accounts.models import User


class Command(BaseCommand):
    help = "Create or update the default admin user from ADMIN_* env vars."

    def handle(self, *args, **options):
        email = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()
        password = os.environ.get("ADMIN_PASSWORD") or ""
        if not email or not password:
            self.stdout.write(self.style.WARNING(
                "Admin seed skipped: set ADMIN_EMAIL and ADMIN_PASSWORD env "
                "vars on Vercel to provision a login."
            ))
            return

        username = os.environ.get("ADMIN_USERNAME", "admin")
        first = os.environ.get("ADMIN_FIRST_NAME", "System")
        last = os.environ.get("ADMIN_LAST_NAME", "Admin")

        try:
            user, created = User.objects.get_or_create(
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
        except IntegrityError:
            # Username collision — reuse by email lookup only.
            user = User.objects.get(email=email)
            created = False

        user.is_staff = True
        user.is_superuser = True
        user.is_system_admin = True
        user.is_active = True
        if first:
            user.first_name = first
        if last:
            user.last_name = last
        user.set_password(password)
        user.save()

        verb = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} admin user: {email}"
        ))
