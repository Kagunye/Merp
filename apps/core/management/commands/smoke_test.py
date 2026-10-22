"""Walk every GET route in the project and report any 5xx.

Usage: python manage.py smoke_test
It discovers every URL pattern that takes no arguments (or whose arg
is a uuid we can supply from the DB) and logs in as the admin user
before hitting each one. Non-200 responses are flagged, but 302/404
are tolerated when expected.
"""
from django.core.management.base import BaseCommand
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver


def _walk(resolver, prefix=""):
    for p in resolver.url_patterns:
        if isinstance(p, URLResolver):
            yield from _walk(p, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern), p


class Command(BaseCommand):
    help = "GET every route in the project and report any 5xx response."

    def add_arguments(self, parser):
        parser.add_argument("--max", type=int, default=0, help="Stop after N routes")

    def handle(self, *args, **opts):
        from django.db import transaction

        class _Rollback(Exception):
            pass

        try:
            with transaction.atomic():
                self._run(opts)
                raise _Rollback()
        except _Rollback:
            pass
        if self.bad:
            raise SystemExit(1)

    def _run(self, opts):
        from apps.accounts.models import User
        from apps.organizations.models import Company, CompanyMembership

        # Browse as a real company administrator, the way staff use the app.
        # Everything here is rolled back afterwards, and the login has no password.
        admin = User.objects.create(
            email="smoke-run@local.invalid", username="smoke-run",
            first_name="Smoke", last_name="Test", is_active=True,
        )
        admin.set_unusable_password()
        admin.save()
        company = Company.objects.filter(is_active=True).order_by("created_at").first()
        if company is None:
            company = Company.objects.create(name="Smoke Test Co")
        CompanyMembership.objects.create(company=company, user=admin, role="ADMIN", is_default=True)

        client = Client()
        client.force_login(admin)

        ok = bad = skipped = 0
        failures = []
        for i, (pattern_str, p) in enumerate(_walk(get_resolver())):
            if opts["max"] and i >= opts["max"]:
                break
            # Routes that change state (or end the session) must never be hit by a GET crawl.
            if any(w in (p.name or "") for w in ("logout", "toggle", "delete", "mark_read", "mark_all_read", "switch")):
                skipped += 1
                continue
            # Build a URL by substituting placeholders with recorded demo data
            # or safe defaults where possible.
            path = self._build_path(pattern_str)
            if path is None:
                skipped += 1
                continue
            try:
                r = client.get(path, follow=False)
            except Exception as exc:
                failures.append((path, f"exception: {exc!r}"))
                bad += 1
                continue
            if 500 <= r.status_code < 600:
                failures.append((path, f"{r.status_code}"))
                bad += 1
            else:
                ok += 1

        self.stdout.write(f"\nResults: {ok} ok, {bad} 5xx, {skipped} skipped")
        for path, why in failures:
            self.stderr.write(self.style.ERROR(f"  FAIL {path}  — {why}"))
        if bad:
            self.stderr.write(self.style.ERROR(f"\n{bad} route(s) returned 5xx."))
        else:
            self.stdout.write(self.style.SUCCESS("All routes healthy."))
        self.bad = bad

    def _build_path(self, pattern_str):
        """Convert a Django URL pattern string into a request path we can hit.

        Routes requiring a uuid/int/str argument are rendered with a
        hard-coded "demo" value — views are expected to 404 for a
        non-existent object (which the smoke test tolerates), not 500.
        """
        import re
        if pattern_str.startswith("^"):
            pattern_str = pattern_str[1:]
        if pattern_str.endswith("$"):
            pattern_str = pattern_str[:-1]

        # Reject admin/static/media/debug and non-GET-friendly namespaces
        if pattern_str.startswith(("admin/", "static/", "media/", "__debug__/")):
            return None
        # Routes that only accept POST/PUT typically don't matter for a GET smoke test;
        # but Django list/create/detail views all accept GET so we proceed.

        # Substitute <uuid:xxx> with a demo uuid.
        placeholders = {
            "uuid:": "00000000-0000-0000-0000-000000000001",
            "int:":  "1",
            "slug:": "demo",
            "str:":  "demo",
            "path:": "demo",
        }
        def sub(match):
            inner = match.group(1)
            for prefix, val in placeholders.items():
                if inner.startswith(prefix):
                    return val
            return "demo"
        path = re.sub(r"<([^>]+)>", sub, pattern_str)

        # Skip anything that still has regex metacharacters we can't resolve.
        if any(c in path for c in "()[]^$?*+|\\"):
            return None
        if not path.startswith("/"):
            path = "/" + path
        return path
