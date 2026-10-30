"""Regression checks for the platform screens: company set-up, users and
sign-in, the document library and every other upload, the help desk, CRM,
projects, recruitment and notifications.

Usage:
    python manage.py test_fix_platform          # rolls the whole run back
    python manage.py test_fix_platform --keep   # leave the test companies behind

Two companies, A and B, each set up through the company screen the way a new
customer would. A's admin also holds a VIEWER seat in B. Uploaded files are
written to a temporary folder that is deleted afterwards.

    1. company record          -> A's admin cannot rewrite B through B's edit
                                  page; B's own admin can; Edit is only offered
                                  on companies the user runs
    2. downloads               -> documents, CVs and receipts come through
                                  protected views, as attachments with nosniff;
                                  another company gets a 404; /media/ does not
                                  serve them
    3. upload checks           -> extension whitelist, HTML/SVG/script content
                                  refused whatever the name says, size cap; on
                                  every upload field in the project
    4. emailed links           -> a reset link uses SITE_URL, never the Host
                                  header; with no SITE_URL in production no
                                  link is mailed
    5. password reset & login  -> a new request spends older links; emails match
                                  in any case; password validators on reset,
                                  sign-up and admin create; five failures lock
                                  the account and the address out
    6. one email, one account  -> a case variant of a taken email is refused on
                                  create and on edit; emails are stored lower
    7. members                 -> an admin adds an existing login with a role,
                                  changes a member's role, cannot change their
                                  own, and the last admin cannot be demoted
    8. no 500s                 -> duplicate category, standalone task, fiscal
                                  month 13, ?category=abc
    9. help desk               -> a closed ticket can't be resolved, closed or
                                  commented on; blank replies are refused;
                                  ticket numbers are issued and never shared
   10. organisation rules      -> one HQ, one default warehouse, no department
                                  loops, unique branch codes, real timezones
   11. range checks            -> credit limit, lead value and date, project
                                  budget and dates, task hours, job openings,
                                  candidates, interviews
   12. notification links      -> only relative or http(s) links, on save and
                                  when read back
   13. CRM                     -> a cashier sees a customer but not what they
                                  owe or their invoices
   14. links a role can't use  -> user management, workshop tiles, department
                                  create/edit are hidden from roles refused them
"""
import datetime
import re
import shutil
import tempfile
from decimal import Decimal

from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand
from django.db import transaction
from django.test import Client
from django.test.utils import override_settings, setup_test_environment, teardown_test_environment
from django.urls import reverse
from django.utils import timezone

D = Decimal
PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"
STRONG_PW = "Correct-Horse-Battery-91"


class Command(BaseCommand):
    help = "Regression checks for the platform (company, users, documents, help desk, CRM) fixes."

    def add_arguments(self, parser):
        parser.add_argument("--keep", action="store_true",
                            help="Commit the test data instead of rolling it back.")

    def handle(self, *args, **opts):
        self.passed = 0
        self.failures = []
        media = tempfile.mkdtemp(prefix="merp-fix-platform-")
        setup_test_environment()
        try:
            with override_settings(MEDIA_ROOT=media, SITE_URL="https://erp.example.test"):
                with transaction.atomic():
                    self.run()
                    if not opts["keep"]:
                        transaction.set_rollback(True)
        finally:
            teardown_test_environment()
            shutil.rmtree(media, ignore_errors=True)
        self.stdout.write("")
        self.stdout.write(f"Checks passed: {self.passed}")
        if self.failures:
            self.stdout.write(self.style.ERROR(f"Failures: {len(self.failures)}"))
            for line in self.failures:
                self.stderr.write(self.style.ERROR(f"  FAIL {line}"))
        else:
            self.stdout.write(self.style.SUCCESS("Failures: 0"))
        if not opts["keep"]:
            self.stdout.write("Test data rolled back (use --keep to retain it).")
        if self.failures:
            raise SystemExit(1)

    # ────────────────────────────── reporting ────────────────────────────────

    def section(self, title):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"-- {title}"))

    def expect(self, label, condition, detail=""):
        if condition:
            self.passed += 1
            self.stdout.write(f"  ok   {label}")
            return True
        self.failures.append(f"{label} — {detail}" if detail else label)
        self.stdout.write(self.style.ERROR(f"  FAIL {label} {detail}"))
        return False

    def flash(self, response):
        ctx = getattr(response, "context", None)
        if not ctx or "messages" not in ctx:
            return []
        return [str(m.message) for m in ctx["messages"]]

    def flashes(self, client, response):
        location = response.get("Location")
        if not location:
            return self.flash(response)
        return self.flash(client.get(location))

    def form_errors(self, response):
        ctx = getattr(response, "context", None)
        form = ctx.get("form") if ctx else None
        return dict(form.errors) if form is not None else {}

    def text(self, response):
        return response.content.decode("utf-8", "replace")

    def refused(self, label, fn, field):
        """`fn()` raises a ValidationError naming `field`."""
        try:
            fn()
        except ValidationError as exc:
            names = set(getattr(exc, "error_dict", {}) or {})
            return self.expect(label, field in names, str(exc))
        return self.expect(label, False, "no ValidationError")

    # ────────────────────────────── fixtures ─────────────────────────────────

    def make_user(self, tag, password=STRONG_PW):
        from apps.accounts.models import User
        self.seq += 1
        name = f"{tag}.{self.stamp}.{self.seq}".lower()
        user = User.objects.create(username=name, email=f"{name}@platform.local",
                                   first_name=tag.title(), last_name="Fix")
        user.set_password(password)
        user.save()
        return user

    def login(self, user, company=None):
        client = Client(raise_request_exception=False)
        client.force_login(user, backend="django.contrib.auth.backends.ModelBackend")
        if company is not None:
            session = client.session
            session["active_company_id"] = str(company.id)
            session.save()
        return client

    def company_data(self, name, **extra):
        data = {
            "name": name, "legal_name": f"{name} Ltd", "registration_number": "",
            "tax_id": "", "vat_number": "", "currency": "KES", "timezone": "Africa/Nairobi",
            "fiscal_year_start_month": "1", "industry": "", "phone": "", "email": "",
            "website": "", "address_line1": "", "address_line2": "", "city": "",
            "state_province": "", "postal_code": "", "country": "Kenya",
        }
        data.update(extra)
        return data

    def make_company(self, label):
        from apps.organizations.models import Company
        admin = self.make_user(f"{label}.admin")
        client = self.login(admin)
        name = f"Fix Platform {label} {self.stamp}"
        r = client.post(reverse("organizations:company_create"), self.company_data(name))
        assert r.status_code in (302, 303), (r.status_code, self.form_errors(r))
        company = Company.objects.get(name=name)
        return company, admin, self.login(admin, company)

    def member(self, company, role, tag=None):
        from apps.organizations.models import CompanyMembership
        user = self.make_user(tag or role)
        CompanyMembership.objects.create(company=company, user=user, role=role, is_default=True)
        return user, self.login(user, company)

    def run(self):
        self.stamp = datetime.datetime.now().strftime("%H%M%S%f")
        self.seq = 0
        self.a, self.a_admin, self.A = self.make_company("A")
        self.b, self.b_admin, self.B = self.make_company("B")
        self.check_company_record()
        self.check_downloads()
        self.check_upload_rules()
        self.check_emailed_links()
        self.check_reset_and_login()
        self.check_unique_email()
        self.check_members()
        self.check_no_500s()
        self.check_helpdesk()
        self.check_organisation_rules()
        self.check_ranges()
        self.check_notification_links()
        self.check_crm_leak()
        self.check_hidden_links()

    # ───────────────────────────── 1. company ────────────────────────────────

    def check_company_record(self):
        from apps.organizations.models import CompanyMembership
        self.section("1. company record: only its own admin edits it")
        CompanyMembership.objects.create(company=self.b, user=self.a_admin, role="VIEWER")
        url = reverse("organizations:company_edit", args=[self.b.pk])
        r = self.A.post(url, self.company_data(self.b.name, legal_name="PWNED"))
        self.b.refresh_from_db()
        self.expect("A's admin (VIEWER in B) posting B's edit page gets 404",
                    r.status_code == 404, f"HTTP {r.status_code}")
        self.expect("B's record is unchanged", self.b.legal_name != "PWNED", self.b.legal_name)
        r = self.A.get(url)
        self.expect("...and cannot open B's edit page either", r.status_code == 404, f"HTTP {r.status_code}")
        r = self.A.get(reverse("organizations:company_list"))
        self.expect("the company list offers Edit for A but not for B",
                    reverse("organizations:company_edit", args=[self.a.pk]) in self.text(r)
                    and url not in self.text(r))
        r = self.B.post(url, self.company_data(self.b.name, legal_name="B Holdings Ltd"))
        self.b.refresh_from_db()
        self.expect("B's own admin can still edit B", r.status_code == 302
                    and self.b.legal_name == "B Holdings Ltd", f"HTTP {r.status_code} {self.form_errors(r)}")
        r = self.A.post(reverse("organizations:company_edit", args=[self.a.pk]),
                        self.company_data(self.a.name, legal_name="A Group Ltd"))
        self.expect("A's admin can edit A", r.status_code == 302, f"HTTP {r.status_code} {self.form_errors(r)}")
        r = self.A.get(reverse("organizations:company_edit", args=[self.a.pk]))
        self.expect("a company admin is not offered the platform-wide 'default company' flag",
                    "is_default" not in (r.context["form"].fields if r.context else {"is_default": 1}))

    # ───────────────────────────── 2. downloads ──────────────────────────────

    def upload(self, client, name, content, filename, ctype="application/pdf"):
        return client.post(reverse("documents:document_upload"), {
            "name": name, "category": "", "description": "", "expiry_date": "",
            "file": SimpleUploadedFile(filename, content, content_type=ctype)})

    def check_downloads(self):
        from apps.documents.models import Document
        self.section("2. downloads go through protected views")
        self.emp, self.E = self.member(self.a, "EMPLOYEE")
        _, viewer = self.member(self.a, "VIEWER")
        r = self.upload(self.E, "NDA", PDF, "nda.pdf")
        doc = Document.objects.filter(company=self.a, name="NDA").first()
        self.expect("an employee uploads a PDF", r.status_code == 302 and doc is not None,
                    f"HTTP {r.status_code} {self.form_errors(r)}")
        if doc is None:
            return
        self.expect("the recorded type comes from the file name, not the browser",
                    doc.mime_type == "application/pdf", doc.mime_type)
        url = reverse("documents:document_download", args=[doc.pk])
        self.expect("download URL is /documents/files/<pk>/download/",
                    url == f"/documents/files/{doc.pk}/download/", url)
        for label, client in (("the uploader (EMPLOYEE)", self.E), ("a VIEWER", viewer),
                              ("the company admin", self.A)):
            r = client.get(url)
            disp = r.get("Content-Disposition", "")
            self.expect(f"{label} downloads it as an attachment",
                        r.status_code == 200 and disp.startswith("attachment"),
                        f"HTTP {r.status_code} {disp}")
        r = self.E.get(url)
        body = b"".join(r.streaming_content) if r.status_code == 200 else b""
        self.expect("the bytes are the uploaded file", body == PDF)
        self.expect("content type is application/pdf with nosniff",
                    r.get("Content-Type") == "application/pdf"
                    and r.get("X-Content-Type-Options") == "nosniff",
                    f"{r.get('Content-Type')} {r.get('X-Content-Type-Options')}")
        r = self.B.get(url)
        self.expect("another company's admin gets 404", r.status_code == 404, f"HTTP {r.status_code}")
        r = Client().get(url)
        self.expect("signed out: sent to sign in", r.status_code == 302 and "/auth/login/" in r["Location"],
                    f"HTTP {r.status_code}")
        r = self.E.get(reverse("documents:document_detail", args=[doc.pk]))
        self.expect("the detail page links to the protected view, not the file",
                    url in self.text(r) and doc.file.url not in self.text(r))
        r = self.E.get(reverse("documents:document_list"))
        self.expect("the list links to the protected view, not the file",
                    url in self.text(r) and doc.file.url not in self.text(r))
        for label, client in (("its own admin", self.A), ("another company's admin", self.B)):
            r = client.get(doc.file.url)
            self.expect(f"/media/ does not serve the document to {label}", r.status_code == 404,
                        f"HTTP {r.status_code}")

        # A CV: recruitment is HR's, scoped to the company's own openings.
        from apps.recruitment.models import Candidate, JobOpening
        opening = JobOpening.objects.create(company=self.a, title="Clerk")
        cand = Candidate.objects.create(opening=opening, first_name="Ann", last_name="Doe",
                                        email="ann@example.test")
        cand.resume.save("ann.pdf", SimpleUploadedFile("ann.pdf", PDF), save=True)
        _, hr = self.member(self.a, "HR_MANAGER")
        _, hr_b = self.member(self.b, "HR_MANAGER")
        url = reverse("recruitment:candidate_resume", args=[cand.pk])
        r = hr.get(url)
        self.expect("HR downloads a candidate's CV as an attachment",
                    r.status_code == 200 and r.get("Content-Disposition", "").startswith("attachment"),
                    f"HTTP {r.status_code}")
        self.expect("another company's HR gets 404", hr_b.get(url).status_code == 404)
        r = hr.get(reverse("recruitment:candidate_detail", args=[cand.pk]))
        self.expect("the candidate page links to the protected CV view", url in self.text(r)
                    and cand.resume.url not in self.text(r))

        # A receipt: visible where its claim is.
        from apps.expenses.models import ExpenseCategory, ExpenseClaim, ExpenseClaimLine
        from apps.hr.models import Employee
        staff = Employee.objects.create(company=self.a, employee_number=f"FX-{self.stamp}",
                                        first_name="Emp", last_name="Loyee", user=self.emp,
                                        date_hired=datetime.date.today())
        cat = ExpenseCategory.objects.create(company=self.a, name=f"Travel {self.stamp}")
        claim = ExpenseClaim.objects.create(company=self.a, reference=f"EXP-FX{self.stamp[-5:]}",
                                            employee=staff, claim_date=datetime.date.today())
        line = ExpenseClaimLine.objects.create(claim=claim, category=cat, description="Taxi",
                                               expense_date=datetime.date.today(), amount=D("500"))
        line.receipt.save("taxi.pdf", SimpleUploadedFile("taxi.pdf", PDF), save=True)
        url = reverse("documents:receipt_download", args=[line.pk])
        r = self.E.get(url)
        self.expect("the claimant downloads their receipt as an attachment",
                    r.status_code == 200 and r.get("Content-Disposition", "").startswith("attachment"),
                    f"HTTP {r.status_code}")
        self.expect("a colleague who can't see the claim gets 404", viewer.get(url).status_code == 404)
        self.expect("another company's admin gets 404", self.B.get(url).status_code == 404)
        r = self.E.get(reverse("expenses:claim_detail", args=[claim.pk]))
        self.expect("the claim page links to the protected receipt view",
                    r.status_code != 200 or (url in self.text(r) and line.receipt.url not in self.text(r)),
                    f"HTTP {r.status_code}")

    # ───────────────────────────── 3. upload rules ───────────────────────────

    def check_upload_rules(self):
        from django.apps import apps
        from django.db import models
        from apps.documents.models import Document
        from apps.documents.validators import UploadValidator
        self.section("3. upload checks: type, content, size")
        cases = [
            ("an .html file", b"<html><script>alert(1)</script></html>", "x.html", "file"),
            ("an .svg file", b"<svg onload=alert(1)>", "x.svg", "file"),
            ("HTML renamed to .pdf", b"<!DOCTYPE html><html><body>hi</body></html>", "evil.pdf", "file"),
            ("script inside a .txt", b"notes\n<script>alert(document.cookie)</script>", "n.txt", "file"),
            ("an SVG renamed to .png", b"<?xml version='1.0'?><svg xmlns='x'/>", "logo.png", "file"),
            ("an .exe", b"MZ\x90\x00", "tool.exe", "file"),
        ]
        for label, content, filename, field in cases:
            before = Document.objects.filter(company=self.a).count()
            r = self.upload(self.E, f"bad {filename}", content, filename)
            self.expect(f"{label} is refused with a message on the file",
                        r.status_code == 200 and field in self.form_errors(r)
                        and Document.objects.filter(company=self.a).count() == before,
                        f"HTTP {r.status_code} {self.form_errors(r)}")
        r = self.upload(self.E, "sheet", b"a,b\n1,2\n", "figures.csv", "text/csv")
        self.expect("a plain CSV is accepted", r.status_code == 302, str(self.form_errors(r)))
        with override_settings(MAX_UPLOAD_SIZE=1024):
            r = self.upload(self.E, "big", PDF + b"0" * 2048, "big.pdf")
            self.expect("a file over MAX_UPLOAD_SIZE is refused", r.status_code == 200
                        and "file" in self.form_errors(r), str(self.form_errors(r)))
        from django.conf import settings
        self.expect("the default cap is 10 MB", settings.MAX_UPLOAD_SIZE == 10 * 1024 * 1024,
                    str(settings.MAX_UPLOAD_SIZE))
        uncovered = []
        for model in apps.get_models():
            for f in model._meta.get_fields():
                if isinstance(f, models.FileField) and not any(
                        isinstance(v, UploadValidator) for v in f.validators):
                    uncovered.append(f"{model.__name__}.{f.name}")
        self.expect("every FileField/ImageField in the project carries the check", not uncovered,
                    ", ".join(uncovered))
        # The résumé goes through the same rule on the candidate form.
        from apps.recruitment.forms import CandidateForm
        from apps.recruitment.models import JobOpening
        opening = JobOpening.objects.create(company=self.a, title="Driver")
        form = CandidateForm({"opening": opening.pk, "first_name": "X", "last_name": "Y",
                              "email": "x@example.test", "years_experience": 0, "stage": "APPLIED",
                              "rating": 0},
                             {"resume": SimpleUploadedFile("cv.pdf", b"<html><script>x</script>")},
                             company=self.a)
        self.expect("an HTML résumé is refused", not form.is_valid() and "resume" in form.errors,
                    str(form.errors))

    # ───────────────────────────── 4. emailed links ──────────────────────────

    def check_emailed_links(self):
        self.section("4. reset links come from SITE_URL, not the Host header")
        user = self.make_user("reset.host")
        mail.outbox = []
        Client().post(reverse("accounts:password_reset"), {"email": user.email},
                      HTTP_HOST="evil.example")
        body = mail.outbox[-1].body if mail.outbox else ""
        self.expect("the reset email links to SITE_URL",
                    "https://erp.example.test/auth/password-reset/" in body, body[:200])
        self.expect("the forged Host is nowhere in it", "evil.example" not in body)
        mail.outbox = []
        with override_settings(SITE_URL="", DEBUG=False, ALLOWED_HOSTS=["*"]):
            Client().post(reverse("accounts:password_reset"), {"email": user.email},
                          HTTP_HOST="evil.example")
        self.expect("with no SITE_URL in production no link is mailed at all", not mail.outbox,
                    mail.outbox[-1].body[:120] if mail.outbox else "")

    # ───────────────────────────── 5. reset & login ──────────────────────────

    def check_reset_and_login(self):
        from apps.accounts.models import LoginAttempt, PasswordResetToken, User
        self.section("5. password reset, validators and sign-in lockout")
        user = self.make_user("reset.user")
        mail.outbox = []
        Client().post(reverse("accounts:password_reset"), {"email": user.email.upper()})
        self.expect("a reset for the email in capitals still finds the account", len(mail.outbox) == 1)
        first = PasswordResetToken.objects.filter(user=user).order_by("created_at").last()
        Client().post(reverse("accounts:password_reset"), {"email": user.email})
        tokens = PasswordResetToken.objects.filter(user=user).order_by("created_at")
        first.refresh_from_db()
        latest = tokens.last()
        self.expect("asking again spends the earlier link", first.used and not first.is_valid())
        self.expect("the newest link still works", latest.pk != first.pk and latest.is_valid())
        c = Client()
        r = c.get(reverse("accounts:password_reset_confirm", args=[first.token]))
        self.expect("the old link is refused", r.status_code == 302, f"HTTP {r.status_code}")
        url = reverse("accounts:password_reset_confirm", args=[latest.token])
        r = c.post(url, {"password": "password", "password_confirm": "password"})
        self.expect("a common password is refused on reset", r.status_code == 200
                    and "password" in self.form_errors(r), str(self.form_errors(r)))
        r = c.post(url, {"password": "12345678901", "password_confirm": "12345678901"})
        self.expect("an all-digit password is refused on reset", r.status_code == 200
                    and "password" in self.form_errors(r))
        r = c.post(url, {"password": STRONG_PW + "x", "password_confirm": STRONG_PW + "x"})
        latest.refresh_from_db()
        user.refresh_from_db()
        self.expect("a strong password is accepted and the link is spent",
                    r.status_code == 302 and latest.used and user.check_password(STRONG_PW + "x"),
                    f"HTTP {r.status_code} {self.form_errors(r)}")

        email = f"signup.{self.stamp}@platform.local"
        r = Client().post(reverse("accounts:register"), {
            "first_name": "Sign", "last_name": "Up", "email": email, "phone": "",
            "password": "password1", "password_confirm": "password1", "terms_accepted": "on"})
        self.expect("sign-up refuses a common password", r.status_code == 200
                    and not User.objects.filter(email=email).exists(), f"HTTP {r.status_code}")
        r = self.A.post(reverse("accounts:user_create"), {
            "first_name": "New", "last_name": "Hire", "email": f"hire.{self.stamp}@platform.local",
            "username": f"hire.{self.stamp}", "phone": "", "job_title": "", "password": "qwertyuiop",
            "company_role": "EMPLOYEE"})
        self.expect("admin user-create refuses a common password", r.status_code == 200
                    and "password" in self.form_errors(r), str(self.form_errors(r)))

        # Lockout: five failures for one account.
        target = self.make_user("lock.target")
        c = Client()
        for _ in range(5):
            c.post(reverse("accounts:login"), {"email": target.email, "password": "wrong-one"},
                   REMOTE_ADDR="10.9.0.1")
        r = c.post(reverse("accounts:login"), {"email": target.email, "password": STRONG_PW},
                   REMOTE_ADDR="10.9.0.2")
        self.expect("after five failures even the right password is refused, with a message",
                    r.status_code == 429 and "Too many failed sign-in attempts" in self.text(r)
                    and "_auth_user_id" not in c.session, f"HTTP {r.status_code}")
        # ...and five failures from one address lock that address for everyone.
        other = self.make_user("lock.other")
        for i in range(5):
            c.post(reverse("accounts:login"), {"email": f"nobody{i}.{self.stamp}@platform.local",
                                               "password": "x"}, REMOTE_ADDR="10.9.0.3")
        r = c.post(reverse("accounts:login"), {"email": other.email, "password": STRONG_PW},
                   REMOTE_ADDR="10.9.0.3")
        self.expect("five failures from one address lock that address", r.status_code == 429)
        r = Client().post(reverse("accounts:login"), {"email": other.email, "password": STRONG_PW},
                          REMOTE_ADDR="10.9.0.4")
        self.expect("the same account signs in from another address", r.status_code == 302,
                    f"HTTP {r.status_code}")
        # The lock lapses after the window.
        LoginAttempt.objects.filter(email__iexact=target.email).update(
            created_at=timezone.now() - datetime.timedelta(minutes=16))
        r = Client().post(reverse("accounts:login"), {"email": target.email, "password": STRONG_PW},
                          REMOTE_ADDR="10.9.0.5")
        self.expect("fifteen minutes later the owner signs in", r.status_code == 302,
                    f"HTTP {r.status_code}")
        c = self.login(target)
        self.expect("force_login (test logins) is untouched by the lockout",
                    c.get(reverse("accounts:profile")).status_code == 200)

    # ───────────────────────────── 6. unique email ───────────────────────────

    def check_unique_email(self):
        from apps.accounts.models import User
        self.section("6. one email, one account, whatever the case")
        taken = self.make_user("taken")
        r = self.A.post(reverse("accounts:user_create"), {
            "first_name": "Dup", "last_name": "Case", "email": taken.email.upper(),
            "username": f"dup.{self.stamp}", "phone": "", "job_title": "", "password": STRONG_PW,
            "company_role": "EMPLOYEE"})
        self.expect("create refuses a case variant of a taken email",
                    r.status_code == 200 and "email" in self.form_errors(r), str(self.form_errors(r)))
        me = self.make_user("profile.me")
        c = self.login(me)
        r = c.post(reverse("accounts:profile_edit"), {
            "first_name": "Me", "last_name": "Fix", "email": taken.email.title(), "username": me.username,
            "phone": "", "job_title": "", "bio": "", "timezone": "Africa/Nairobi", "theme_preference": "light"})
        self.expect("profile edit refuses a case variant of another account's email",
                    r.status_code == 200 and "email" in self.form_errors(r), str(self.form_errors(r)))
        u = User.objects.create(username=f"mixed.{self.stamp}", email=f"Mixed.Case.{self.stamp}@Platform.Local")
        u.refresh_from_db()
        self.expect("emails are stored lower-case", u.email == u.email.lower(), u.email)

    # ───────────────────────────── 7. members ────────────────────────────────

    def check_members(self):
        from apps.organizations.models import CompanyMembership
        self.section("7. adding existing users and changing roles")
        outsider = self.make_user("outsider")
        r = self.A.post(reverse("accounts:user_add_existing"),
                        {"email": outsider.email.upper(), "company_role": "SALES_REP"})
        m = CompanyMembership.objects.filter(company=self.a, user=outsider).first()
        self.expect("an admin adds an existing login by email, with a role",
                    r.status_code == 302 and m is not None and m.role == "SALES_REP",
                    f"HTTP {r.status_code} {self.form_errors(r)}")
        r = self.A.post(reverse("accounts:user_add_existing"),
                        {"email": outsider.email, "company_role": "VIEWER"})
        self.expect("adding them twice is refused", r.status_code == 200 and "email" in self.form_errors(r))
        r = self.A.post(reverse("accounts:user_add_existing"),
                        {"email": f"ghost.{self.stamp}@platform.local", "company_role": "VIEWER"})
        self.expect("an unknown email is refused", r.status_code == 200 and "email" in self.form_errors(r))
        _, rep_client = self.member(self.a, "SALES_REP")
        r = rep_client.post(reverse("accounts:user_add_existing"),
                            {"email": self.make_user("x").email, "company_role": "ADMIN"})
        self.expect("a non-admin cannot add people", r.status_code == 403, f"HTTP {r.status_code}")

        def edit(client, user, role):
            return client.post(reverse("accounts:user_edit", args=[user.pk]), {
                "first_name": user.first_name, "last_name": user.last_name, "email": user.email,
                "username": user.username, "phone": "", "job_title": "", "bio": "",
                "timezone": "Africa/Nairobi", "theme_preference": "light", "company_role": role})

        r = edit(self.A, outsider, "ACCOUNTANT")
        m.refresh_from_db()
        self.expect("an admin changes a member's role", r.status_code == 302 and m.role == "ACCOUNTANT",
                    f"HTTP {r.status_code} {self.form_errors(r)}")
        r = edit(self.A, self.a_admin, "VIEWER")
        own = CompanyMembership.objects.get(company=self.a, user=self.a_admin)
        self.expect("an admin cannot change their own role", own.role == "ADMIN", own.role)
        from apps.accounts.models import User
        root = User.objects.create(username=f"root.{self.stamp}", email=f"root.{self.stamp}@platform.local",
                                   is_superuser=True)
        r = edit(self.login(root, self.a), self.a_admin, "VIEWER")
        own.refresh_from_db()
        self.expect("the last admin cannot be demoted, with a message on the role",
                    own.role == "ADMIN" and "company_role" in self.form_errors(r),
                    f"{own.role} {self.form_errors(r)}")
        edit(self.A, outsider, "ADMIN")
        r = edit(self.login(root, self.a), self.a_admin, "FINANCE_MANAGER")
        own.refresh_from_db()
        self.expect("with a second admin in place the first may be demoted",
                    own.role == "FINANCE_MANAGER", f"{own.role} {self.form_errors(r)}")
        own.role = "ADMIN"
        own.save()

    # ───────────────────────────── 8. no 500s ────────────────────────────────

    def check_no_500s(self):
        from apps.projects.models import Project, Task
        self.section("8. bad input is a message, not a 500")
        self.A.post(reverse("documents:category_create"), {"name": "Contracts", "description": ""})
        r = self.A.post(reverse("documents:category_create"), {"name": "contracts", "description": ""})
        self.expect("a duplicate document category is a form error", r.status_code == 200
                    and "name" in self.form_errors(r), f"HTTP {r.status_code}")
        _, pm = self.member(self.a, "PROJECT_MANAGER")
        p = Project.objects.create(company=self.a, reference=f"PRJ-FX{self.stamp[-4:]}", name="Fit-out",
                                   start_date=datetime.date.today())
        r = pm.post(reverse("projects:task_create"), {"project": str(p.pk), "title": "Wire up",
                                                      "status": "TODO", "priority": "MEDIUM"})
        self.expect("the standalone task form saves the task on the chosen project",
                    r.status_code == 302 and Task.objects.filter(project=p, title="Wire up").exists(),
                    f"HTTP {r.status_code} {self.form_errors(r)}")
        founder = self.login(self.make_user("founder"))
        for month in ("13", "0"):
            r = founder.post(reverse("organizations:company_create"),
                             self.company_data(f"FY{month} {self.stamp}", fiscal_year_start_month=month))
            self.expect(f"fiscal year start month {month} is a form error", r.status_code == 200
                        and "fiscal_year_start_month" in self.form_errors(r), f"HTTP {r.status_code}")
        r = self.A.get(reverse("documents:document_list"), {"category": "abc"})
        self.expect("?category=abc is ignored", r.status_code == 200, f"HTTP {r.status_code}")

    # ───────────────────────────── 9. help desk ──────────────────────────────

    def check_helpdesk(self):
        from apps.helpdesk.models import Ticket
        self.section("9. help desk state machine and numbering")
        agent, agent_c = self.member(self.a, "SALES_REP", "agent")
        r = self.E.get(reverse("helpdesk:ticket_create"))
        preview = r.context["form"].fields["reference"].initial if r.context else ""
        self.expect("the new-ticket form previews a TKT- number", str(preview).startswith("TKT-"), str(preview))

        def open_ticket(client, reference, subject):
            return client.post(reverse("helpdesk:ticket_create"), {
                "reference": reference, "subject": subject, "description": "", "customer": "",
                "requester_name": "", "requester_email": "", "channel": "WEB", "priority": "NORMAL",
                "assigned_to": str(agent.pk)})

        r1 = open_ticket(self.E, preview, "Printer jam")
        r2 = open_ticket(self.E, preview, "Screen flicker")
        t1 = Ticket.objects.filter(company=self.a, subject="Printer jam").first()
        t2 = Ticket.objects.filter(company=self.a, subject="Screen flicker").first()
        self.expect("the first ticket takes the previewed number", t1 is not None and t1.reference == preview,
                    f"{r1.status_code} {self.form_errors(r1)}")
        self.expect("a second form saved with the same preview gets the next number",
                    t2 is not None and t1 is not None and t2.reference != t1.reference,
                    f"{r2.status_code} {t2 and t2.reference}")
        open_ticket(self.E, f"MANUAL-{self.stamp}", "Typed number")
        r = open_ticket(self.E, f"manual-{self.stamp}", "Typed again")
        self.expect("a hand-typed duplicate reference is refused", r.status_code == 200
                    and "reference" in self.form_errors(r), str(self.form_errors(r)))
        r = open_ticket(self.E, "", "Blank ref")
        t3 = Ticket.objects.filter(company=self.a, subject="Blank ref").first()
        self.expect("a blank reference is numbered", t3 is not None and t3.reference.startswith("TKT-"),
                    str(self.form_errors(r)))
        if t1 is None:
            return
        agent_c.post(reverse("helpdesk:ticket_resolve", args=[t1.pk]))
        t1.refresh_from_db()
        resolved_at = t1.resolved_at
        self.E.post(reverse("helpdesk:ticket_close", args=[t1.pk]))
        t1.refresh_from_db()
        closed_at = t1.closed_at
        r = agent_c.post(reverse("helpdesk:ticket_resolve", args=[t1.pk]))
        t1.refresh_from_db()
        self.expect("resolve after close is refused: still CLOSED, resolved_at kept",
                    t1.status == "CLOSED" and t1.resolved_at == resolved_at, t1.status)
        self.expect("...with a message", any("already closed" in m for m in self.flashes(agent_c, r)))
        r = self.E.post(reverse("helpdesk:ticket_close", args=[t1.pk]))
        t1.refresh_from_db()
        self.expect("closing a closed ticket is refused", t1.closed_at == closed_at
                    and any("already closed" in m for m in self.flashes(self.E, r)))
        before = t1.comments.count()
        r = self.E.post(reverse("helpdesk:ticket_comment", args=[t1.pk]), {"body": "Still broken"})
        self.expect("a reply on a closed ticket is refused", t1.comments.count() == before
                    and any("closed" in m for m in self.flashes(self.E, r)))
        if t2 is not None:
            r = self.E.post(reverse("helpdesk:ticket_comment", args=[t2.pk]), {"body": "   "})
            self.expect("a blank reply shows an error and posts nothing",
                        t2.comments.count() == 0 and any("Write something" in m for m in self.flashes(self.E, r)))

    # ───────────────────────────── 10. organisations ─────────────────────────

    def check_organisation_rules(self):
        from apps.organizations.models import Branch, Department, Warehouse
        self.section("10. organisation rules")

        def branch(name, code, hq=False):
            data = {"company": str(self.a.pk), "name": name, "code": code, "manager_name": "",
                    "phone": "", "email": "", "address_line1": "", "city": "", "country": "Kenya"}
            if hq:
                data["is_headquarters"] = "on"
            return self.A.post(reverse("organizations:branch_create"), data)

        old_hq = Branch.objects.get(company=self.a, is_headquarters=True)
        r = branch("Mombasa", "MSA", hq=True)
        hqs = list(Branch.objects.filter(company=self.a, is_headquarters=True).values_list("name", flat=True))
        self.expect("a new HQ takes the title from the old one", r.status_code == 302 and hqs == ["Mombasa"],
                    f"{hqs} {self.form_errors(r)}")
        r = branch("Kisumu", "msa")
        self.expect("a duplicate branch code is refused", r.status_code == 200 and "code" in self.form_errors(r),
                    str(self.form_errors(r)))
        old_hq.is_headquarters = True
        old_hq.save()
        self.expect("saving the old HQ again moves the title back",
                    Branch.objects.filter(company=self.a, is_headquarters=True).count() == 1)

        r = self.A.post(reverse("organizations:warehouse_create"), {
            "company": str(self.a.pk), "branch": "", "name": "Depot 2", "code": f"D2{self.stamp[-3:]}",
            "is_default": "on", "address_line1": "", "city": "", "country": "Kenya"})
        defaults = list(Warehouse.objects.filter(company=self.a, is_default=True).values_list("name", flat=True))
        self.expect("a new default warehouse replaces the old default", defaults == ["Depot 2"],
                    f"{defaults} {self.form_errors(r)}")

        top = Department.objects.create(company=self.a, name=f"Ops {self.stamp}")
        mid = Department.objects.create(company=self.a, name=f"Logistics {self.stamp}", parent=top)
        low = Department.objects.create(company=self.a, name=f"Fleet {self.stamp}", parent=mid)
        r = self.A.post(reverse("organizations:department_edit", args=[top.pk]), {
            "company": str(self.a.pk), "branch": "", "name": top.name, "code": "", "parent": str(low.pk)})
        top.refresh_from_db()
        self.expect("a department loop (A under its own grandchild) is refused",
                    r.status_code == 200 and "parent" in self.form_errors(r) and top.parent_id is None,
                    str(self.form_errors(r)))

        r = self.A.post(reverse("organizations:company_edit", args=[self.a.pk]),
                        self.company_data(self.a.name, timezone="Africa/Nairobbi"))
        self.expect("an unknown company timezone is refused", r.status_code == 200
                    and "timezone" in self.form_errors(r), str(self.form_errors(r)))
        me = self.make_user("tz")
        r = self.login(me).post(reverse("accounts:profile_edit"), {
            "first_name": "Tz", "last_name": "Fix", "email": me.email, "username": me.username,
            "phone": "", "job_title": "", "bio": "", "timezone": "Mars/Olympus", "theme_preference": "light"})
        self.expect("an unknown user timezone is refused", r.status_code == 200
                    and "timezone" in self.form_errors(r), str(self.form_errors(r)))

    # ───────────────────────────── 11. ranges ────────────────────────────────

    def check_ranges(self):
        from apps.crm.models import Customer, Lead
        from apps.projects.models import Project, Task
        from apps.recruitment.models import Candidate, Interview, JobOpening
        self.section("11. range checks")
        today = datetime.date.today()
        self.refused("negative customer credit limit",
                     Customer(company=self.a, name="C", credit_limit=D("-1")).clean, "credit_limit")
        self.refused("negative lead value", Lead(company=self.a, name="L", estimated_value=D("-5")).clean,
                     "estimated_value")
        self.refused("lead close date before 2000",
                     Lead(company=self.a, name="L", expected_close_date=datetime.date(1999, 12, 31)).clean,
                     "expected_close_date")
        self.refused("negative project budget",
                     Project(company=self.a, name="P", start_date=today, budget=D("-1")).clean, "budget")
        self.refused("project ending before it starts",
                     Project(company=self.a, name="P", start_date=today,
                             end_date=today - datetime.timedelta(days=1)).clean, "end_date")
        self.refused("negative task estimate", Task(title="T", estimated_hours=D("-2")).clean, "estimated_hours")
        self.refused("opening salary min above max",
                     JobOpening(company=self.a, title="J", salary_range_min=D("900"),
                                salary_range_max=D("100")).clean, "salary_range_max")
        self.refused("negative opening salary",
                     JobOpening(company=self.a, title="J", salary_range_min=D("-1")).clean, "salary_range_min")
        self.refused("opening closing before it was posted",
                     JobOpening(company=self.a, title="J", posted_date=today,
                                close_date=today - datetime.timedelta(days=3)).clean, "close_date")
        self.refused("opening headcount 0", JobOpening(company=self.a, title="J", headcount=0).clean, "headcount")
        self.refused("candidate rating 6", Candidate(first_name="a", last_name="b", rating=6).clean, "rating")
        self.refused("negative expected salary",
                     Candidate(first_name="a", last_name="b", expected_salary=D("-1")).clean, "expected_salary")
        self.refused("a 0-minute interview", Interview(duration_minutes=0).clean, "duration_minutes")
        r = self.A.post(reverse("crm:customer_create"), {
            "name": "Neg", "customer_type": "BUSINESS", "tax_id": "", "phone": "", "email": "", "website": "",
            "credit_limit": "-100", "payment_terms_days": "30", "currency": "KES", "address_line1": "",
            "address_line2": "", "city": "", "country": "", "notes": ""})
        self.expect("the customer screen shows the credit-limit error",
                    r.status_code == 200 and "credit_limit" in self.form_errors(r), str(self.form_errors(r)))

    # ───────────────────────────── 12. notifications ─────────────────────────

    def check_notification_links(self):
        from apps.notifications.models import Notification
        self.section("12. notification links")
        cases = [("javascript:alert(1)", ""), ("JaVaScRiPt:alert(1)", ""), ("java\tscript:alert(1)", ""),
                 ("data:text/html,<script>x</script>", ""), ("//evil.example/x", ""),
                 ("/\\evil.example", ""), ("/sales/invoices/", "/sales/invoices/"),
                 ("https://docs.example.test/a", "https://docs.example.test/a"), ("invoices/1/", "invoices/1/")]
        for raw, want in cases:
            n = Notification.send(self.a_admin, "Link", action_url=raw)
            n.refresh_from_db()
            self.expect(f"{raw!r} is stored as {want!r}", n.action_url == want, repr(n.action_url))
        n = Notification.send(self.a_admin, "Old row", action_url="/ok/")
        Notification.objects.filter(pk=n.pk).update(action_url="javascript:alert(document.cookie)")
        again = Notification.objects.get(pk=n.pk)
        self.expect("an unsafe link already in the table is dropped when read", again.action_url == "")
        r = self.A.get(reverse("notifications:list"))
        self.expect("the pages never render a javascript: link", "javascript:" not in self.text(r).lower())
        self.refused("clean() refuses a javascript: link",
                     Notification(user=self.a_admin, title="x", action_url="javascript:x").clean, "action_url")

    # ───────────────────────────── 13. CRM ───────────────────────────────────

    def check_crm_leak(self):
        from apps.crm.models import Customer
        self.section("13. a cashier sees the customer, not the ledger")
        cust = Customer.objects.create(company=self.a, name=f"Walk-in {self.stamp}")
        _, cashier = self.member(self.a, "POS_CASHIER")
        _, finance = self.member(self.a, "FINANCE_MANAGER")
        url = reverse("crm:customer_detail", args=[cust.pk])
        r = cashier.get(url)
        page = self.text(r)
        self.expect("POS_CASHIER opens the customer", r.status_code == 200, f"HTTP {r.status_code}")
        self.expect("...but sees no outstanding balance or invoice list",
                    "Outstanding" not in page and "Recent Invoices" not in page)
        self.expect("...and no New Invoice / Edit buttons",
                    reverse("sales:invoice_create") not in page
                    and reverse("crm:customer_edit", args=[cust.pk]) not in page)
        r = finance.get(url)
        self.expect("FINANCE_MANAGER sees the balance and the invoices",
                    "Outstanding" in self.text(r) and "Recent Invoices" in self.text(r))

    # ───────────────────────────── 14. hidden links ──────────────────────────

    def check_hidden_links(self):
        from apps.core import access
        self.section("14. links a role can't open are not shown")
        users_url = reverse("accounts:user_list")
        _, rep = self.member(self.a, "SALES_REP")
        for ns, client in (("documents", self.E), ("helpdesk", self.E), ("crm", rep)):
            page = self.text(client.get(reverse(f"{ns}:home" if ns != "crm" else "crm:index")))
            self.expect(f"{ns} home does not link user management for a non-admin", users_url not in page)
        self.expect("...but an admin still sees it",
                    users_url in self.text(self.A.get(reverse("documents:home"))))

        pm_user, pm = self.member(self.a, "PROJECT_MANAGER", "pm.tiles")
        page = self.text(pm.get(reverse("workshop:index")))
        from django.urls import resolve
        hrefs = re.findall(r'<a href="([^"?]+)[^"]*" class="launcher-tile"', page)
        blocked = []
        for href in hrefs:
            match = resolve(href)
            if not access.can(pm_user, "PROJECT_MANAGER", match.namespace, match.url_name, "GET"):
                blocked.append(href)
        self.expect("every workshop tile shown to PROJECT_MANAGER opens for them",
                    hrefs and not blocked, f"{len(hrefs)} shown, refused: {blocked}")
        admin_tiles = len(re.findall(r'class="launcher-tile"', self.text(self.A.get(reverse("workshop:index")))))
        self.expect("the admin still sees all 14 workshop tiles", admin_tiles == 14, str(admin_tiles))

        hr_user, hr = self.member(self.a, "HR_MANAGER", "hr.depts")
        page = self.text(hr.get(reverse("organizations:department_list")))
        # HR keeps the department tree (access.py department_*), so the links show
        # for HR; the point of the check is that they follow the role table.
        may_change = access.can(hr_user, "HR_MANAGER", "organizations", "department_create", "POST")
        self.expect("department New/Edit links follow the role table for HR_MANAGER",
                    (reverse("organizations:department_create") in page) == may_change)
