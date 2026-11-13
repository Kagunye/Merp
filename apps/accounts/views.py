"""Authentication and user management views."""
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.generic import DetailView, ListView, TemplateView, UpdateView, View

from .forms import (
    AddExistingUserForm, ChangePasswordForm, LoginForm, MemberEditForm, MembershipEditForm,
    PasswordResetConfirmForm, PasswordResetRequestForm, RegisterForm,
    UserCreateForm, UserEditForm,
)
from .links import absolute_link
from .models import LoginAttempt, PasswordResetToken, User

# Sign-in throttling: this many failed attempts for one account, or from one
# address, within the window locks further attempts out until the window has
# passed. Guessing a password then takes years instead of minutes.
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_WINDOW = timedelta(minutes=15)
LOCKED_OUT_REASON = "Locked out"


def _get_client_ip(request):
    x_forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def login_locked_out(email, ip):
    """Whether sign-in for `email`, or from `ip`, is locked out right now.

    Counts the real failures (not the refusals the lockout itself logs) in
    the last LOGIN_LOCKOUT_WINDOW. For the account, only failures since its
    last successful sign-in count, so the owner who finally remembers their
    password starts again from zero. The address count is not reset by a
    success: one person signing in to their own account must not clear the
    way for guessing at everyone else's from the same machine.

    The address comes from X-Forwarded-For where a proxy sets it, which a
    client can forge; the per-account limit is the one that can't be
    dodged, and it alone caps guessing at one account.
    """
    since = timezone.now() - LOGIN_LOCKOUT_WINDOW
    failures = LoginAttempt.objects.filter(success=False, created_at__gte=since).exclude(
        failure_reason=LOCKED_OUT_REASON)
    if email:
        last_ok = LoginAttempt.objects.filter(
            email__iexact=email, success=True, created_at__gte=since).values_list(
            "created_at", flat=True).first()
        account = failures.filter(email__iexact=email)
        if last_ok:
            account = account.filter(created_at__gt=last_ok)
        if account.count() >= LOGIN_MAX_FAILURES:
            return True
    if ip and failures.filter(ip_address=ip).count() >= LOGIN_MAX_FAILURES:
        return True
    return False


def throttled_admin_login(admin_login):
    """`admin_login` (AdminSite.login) behind the same lockout as the sign-in page.

    /admin/login/ is a second door to the same accounts. It took any number
    of guesses and recorded none of them, so the five-failure lockout on
    /auth/login/ could be walked around by guessing at the admin form
    instead -- and the accounts worth guessing (platform admins) are exactly
    the ones it serves. Every POST here is now checked against
    login_locked_out() first and recorded as a LoginAttempt afterwards, so
    the two forms share one count.
    """
    import functools
    import logging
    log = logging.getLogger(__name__)

    @functools.wraps(admin_login)
    def login_view(request, extra_context=None):
        if request.method != "POST":
            return admin_login(request, extra_context)
        email = (request.POST.get("username") or "").strip()
        ip = _get_client_ip(request)
        ua = request.META.get("HTTP_USER_AGENT", "")
        try:
            locked = login_locked_out(email, ip)
        except Exception as exc:  # never let the throttle itself block sign-in
            log.warning("Admin login throttle check failed: %s", exc)
            locked = False
        if locked:
            try:
                LoginAttempt.objects.create(email=email, ip_address=ip, user_agent=ua,
                                            success=False, failure_reason=LOCKED_OUT_REASON)
            except Exception as exc:
                log.warning("LoginAttempt (admin lockout) audit write failed: %s", exc)
            from django.http import HttpResponse
            minutes = int(LOGIN_LOCKOUT_WINDOW.total_seconds() // 60)
            return HttpResponse(
                f"Too many failed sign-in attempts. Sign-in is paused for {minutes} minutes "
                "for this account and this device. Wait and try again, or reset your password.",
                status=429, content_type="text/plain; charset=utf-8")
        response = admin_login(request, extra_context)
        ok = response.status_code in (301, 302) and request.user.is_authenticated
        try:
            LoginAttempt.objects.create(
                user=request.user if ok else None, email=email, ip_address=ip, user_agent=ua,
                success=ok, failure_reason="" if ok else "Invalid credentials (admin)")
        except Exception as exc:
            log.warning("LoginAttempt (admin) audit write failed: %s", exc)
        return response

    return login_view


class LoginView(View):
    template_name = "accounts/login.html"

    @staticmethod
    def _demo_context():
        """Expose seeded admin creds on the sign-in page only when the
        operator explicitly opts in via SHOW_DEMO_CREDENTIALS=1."""
        import os
        if os.environ.get("SHOW_DEMO_CREDENTIALS") != "1":
            return {}
        email = os.environ.get("ADMIN_EMAIL") or ""
        password = os.environ.get("ADMIN_PASSWORD") or ""
        if not email or not password:
            return {}
        return {"demo_email": email, "demo_password": password}

    def get(self, request):
        if request.user.is_authenticated:
            return redirect("dashboard:index")
        features = ["Finance", "Inventory", "HR & Payroll", "Reports & BI", "Point of Sale", "Procurement"]
        ctx = {"form": LoginForm(), "features": features, **self._demo_context()}
        return render(request, self.template_name, ctx)

    def post(self, request):
        import logging
        import os
        from django.db import OperationalError, ProgrammingError
        log = logging.getLogger(__name__)
        form = LoginForm(request.POST)
        ip = _get_client_ip(request)
        ua = request.META.get("HTTP_USER_AGENT", "")
        features = ["Finance", "Inventory", "HR & Payroll", "Reports & BI", "Point of Sale", "Procurement"]

        if form.is_valid():
            email = form.cleaned_data["email"]
            password = form.cleaned_data["password"]
            remember_me = form.cleaned_data.get("remember_me", False)

            try:
                locked = login_locked_out(email, ip)
            except Exception as exc:  # never let the throttle itself block sign-in
                log.warning("Login throttle check failed: %s", exc)
                locked = False
            if locked:
                try:
                    LoginAttempt.objects.create(
                        email=email, ip_address=ip, user_agent=ua, success=False,
                        failure_reason=LOCKED_OUT_REASON)
                except Exception as exc:
                    log.warning("LoginAttempt (lockout) audit write failed: %s", exc)
                minutes = int(LOGIN_LOCKOUT_WINDOW.total_seconds() // 60)
                form.add_error(None, (
                    f"Too many failed sign-in attempts. Sign-in is paused for {minutes} "
                    "minutes for this account and this device. Wait and try again, or "
                    "reset your password."))
                ctx = {"form": form, "features": features, **self._demo_context()}
                return render(request, self.template_name, ctx, status=429)

            def _try_auth():
                return authenticate(request, username=email, password=password)

            try:
                user = _try_auth()
            except (OperationalError, ProgrammingError) as exc:
                # Likely cause: migrations haven't actually completed yet.
                # Lazily migrate and try authenticating one more time.
                log.warning("Auth hit %s — running lazy migrate + retry", type(exc).__name__)
                try:
                    from django.core.management import call_command
                    call_command("migrate", "--noinput", verbosity=0)
                    if os.environ.get("ADMIN_EMAIL") and os.environ.get("ADMIN_PASSWORD"):
                        call_command("create_default_admin")
                    user = _try_auth()
                except Exception as inner:
                    log.exception("Lazy-migrate retry failed: %s", inner)
                    form.add_error(None,
                        "The database is still initializing. Please refresh in a few seconds.")
                    ctx = {"form": form, "features": features, **self._demo_context()}
                    return render(request, self.template_name, ctx)
            except Exception as exc:
                log.exception("Authentication backend crashed: %s", exc)
                form.add_error(None, "Login is temporarily unavailable. Please try again shortly.")
                ctx = {"form": form, "features": features, **self._demo_context()}
                return render(request, self.template_name, ctx)

            if user is not None:
                try:
                    login(request, user)
                except Exception as exc:
                    log.exception("login() failed (likely a session backend issue): %s", exc)
                    form.add_error(None, "Could not start a session. Please try again.")
                    ctx = {"form": form, "features": features, **self._demo_context()}
                    return render(request, self.template_name, ctx)

                # Audit — never block sign-in on an audit write.
                try:
                    LoginAttempt.objects.create(
                        user=user, email=email, ip_address=ip,
                        user_agent=ua, success=True,
                    )
                except Exception as exc:
                    log.warning("LoginAttempt audit write failed: %s", exc)

                try:
                    if not remember_me:
                        request.session.set_expiry(0)
                except Exception as exc:
                    log.warning("session.set_expiry failed: %s", exc)

                try:
                    messages.success(request, f"Welcome back, {user.first_name or user.email}!")
                except Exception:
                    pass
                return redirect(safe_next(request, "/dashboard/"))
            else:
                try:
                    LoginAttempt.objects.create(
                        email=form.cleaned_data.get("email", ""),
                        ip_address=ip, user_agent=ua, success=False,
                        failure_reason="Invalid credentials",
                    )
                except Exception as exc:
                    log.warning("LoginAttempt (failure) audit write failed: %s", exc)
                form.add_error(None, "Invalid email or password. Please try again.")

        ctx = {"form": form, "features": features, **self._demo_context()}
        return render(request, self.template_name, ctx)


class RegisterView(View):
    """Public self-service registration page.

    Creates an inactive-pending account that still logs in; a real
    deployment should gate it behind an admin-approval flow or send an
    email confirmation. Here we log the user straight in and route to the
    dashboard, which will show the no-company onboarding card.
    """
    template_name = "accounts/register.html"

    def get(self, request):
        if request.user.is_authenticated:
            return redirect("dashboard:index")
        return render(request, self.template_name, {"form": RegisterForm()})

    def post(self, request):
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save()
            # Several auth backends are configured, so login() must be told which one.
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            messages.success(
                request,
                f"Welcome to Mofisi, {user.first_name or user.email}! "
                "Finish setting up your company to get started.",
            )
            return redirect("dashboard:index")
        return render(request, self.template_name, {"form": form})


def safe_next(request, default):
    """The ?next= target only if it stays on this site; otherwise `default`."""
    from django.utils.http import url_has_allowed_host_and_scheme
    target = request.GET.get("next") or request.POST.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
            target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return target
    return default


class LogoutView(View):
    def post(self, request):
        logout(request)
        messages.info(request, "You have been logged out.")
        return redirect("accounts:login")

    def get(self, request):
        # Signing out changes state, so it takes a button press (POST with a
        # CSRF token); a link or an <img> pointing here can't sign anyone out.
        if not request.user.is_authenticated:
            return redirect("accounts:login")
        return render(request, "accounts/logout_confirm.html", {"page_title": "Sign out"})


class ProfileView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/profile.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "My Profile"
        from .models import CompanyInvitation
        ctx["pending_invitations"] = CompanyInvitation.pending_for(self.request.user)
        return ctx

    def get(self, request, *args, **kwargs):
        # The profile page is where a person looks for what is waiting on
        # them; a short note sends them to the invitations screen.
        from .models import CompanyInvitation
        count = CompanyInvitation.pending_for(request.user).count()
        if count:
            messages.info(request, f"You have {count} pending company invitation"
                                   f"{'s' if count != 1 else ''}. Open Invitations "
                                   f"(/auth/invitations/) to accept or decline.")
        return super().get(request, *args, **kwargs)


class ProfileEditView(LoginRequiredMixin, View):
    template_name = "accounts/profile_edit.html"

    def get(self, request):
        form = UserEditForm(instance=request.user)
        return render(request, self.template_name, {"form": form, "page_title": "Edit Profile"})

    def post(self, request):
        form = UserEditForm(request.POST, request.FILES, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile updated successfully.")
            return redirect("accounts:profile")
        return render(request, self.template_name, {"form": form})


class ChangePasswordView(LoginRequiredMixin, View):
    template_name = "accounts/change_password.html"

    def get(self, request):
        return render(request, self.template_name, {
            "form": ChangePasswordForm(request.user),
            "page_title": "Change Password",
        })

    def post(self, request):
        form = ChangePasswordForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            messages.success(request, "Password changed successfully.")
            return redirect("accounts:profile")
        return render(request, self.template_name, {"form": form})


class PasswordResetRequestView(View):
    template_name = "accounts/password_reset.html"

    def get(self, request):
        return render(request, self.template_name, {"form": PasswordResetRequestForm()})

    def post(self, request):
        form = PasswordResetRequestForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data["email"].strip()
            # Emails are matched without regard to case, exactly as sign-in
            # matches them: "Jane@Acme.com" asking for a reset is jane@acme.com.
            user = User.objects.filter(email__iexact=email, is_active=True).first()
            if user is not None:
                from django.core.mail import send_mail
                from django.db import transaction
                with transaction.atomic():
                    # Only the newest link works. Every earlier unused link is
                    # spent now, so a link sitting in an old email (or one an
                    # attacker asked for) stops working the moment the owner
                    # asks again.
                    PasswordResetToken.objects.filter(user=user, used=False).update(used=True)
                    token = PasswordResetToken.objects.create(
                        user=user,
                        expires_at=timezone.now() + timedelta(hours=24),
                    )
                # Built from SITE_URL, never from the request's Host header.
                reset_url = absolute_link(f"/auth/password-reset/{token.token}/", request)
                if reset_url:
                    send_mail(
                        subject="Password Reset Request — Mofisi",
                        message=f"Click to reset your password: {reset_url}\n\nThis link expires in 24 hours.",
                        from_email=None,
                        recipient_list=[user.email],
                        fail_silently=True,
                    )
            # The same answer either way: don't reveal whether the email exists.
            messages.success(request, "If an account exists with that email, you will receive a reset link.")
            return redirect("accounts:login")
        return render(request, self.template_name, {"form": form})


class PasswordResetConfirmView(View):
    template_name = "accounts/password_reset_confirm.html"

    def get(self, request, token):
        reset_token = get_object_or_404(PasswordResetToken, token=token)
        if not reset_token.is_valid():
            messages.error(request, "This reset link has expired or already been used.")
            return redirect("accounts:password_reset")
        return render(request, self.template_name, {
            "form": PasswordResetConfirmForm(user=reset_token.user),
            "token": token,
        })

    def post(self, request, token):
        reset_token = get_object_or_404(PasswordResetToken, token=token)
        if not reset_token.is_valid():
            messages.error(request, "This reset link has expired.")
            return redirect("accounts:password_reset")
        form = PasswordResetConfirmForm(request.POST, user=reset_token.user)
        if form.is_valid():
            reset_token.user.set_password(form.cleaned_data["password"])
            reset_token.user.save()
            # Spend this link and any other still outstanding for the account.
            PasswordResetToken.objects.filter(user=reset_token.user, used=False).update(used=True)
            messages.success(request, "Password reset successfully. Please log in.")
            return redirect("accounts:login")
        return render(request, self.template_name, {"form": form, "token": token})


def _company_members(request):
    """Users this person may manage: members of the active company (everyone,
    for a platform admin). Never another company's users."""
    from apps.core.access import is_platform_admin
    if is_platform_admin(request.user):
        return User.objects.all()
    company = getattr(request, "active_company", None)
    if company is None:
        return User.objects.none()
    return User.objects.filter(memberships__company=company).distinct()


def _membership_active(company):
    from django.db.models import Exists, OuterRef
    from apps.organizations.models import CompanyMembership
    return Exists(CompanyMembership.objects.filter(user=OuterRef("pk"), company=company, is_active=True))


class UserListView(LoginRequiredMixin, ListView):
    model = User
    template_name = "accounts/user_list.html"
    context_object_name = "users"
    paginate_by = 25

    def get_queryset(self):
        company = self.request.active_company
        if not company:
            return User.objects.none()
        # Everyone who has ever been in this company, so a disabled member can be re-enabled.
        qs = (
            User.objects.filter(memberships__company=company)
            .distinct().annotate(member_active=_membership_active(company))
            .order_by("first_name", "last_name")
        )
        q = self.request.GET.get("q", "").strip()
        if q:
            from django.db.models import Q
            qs = qs.filter(
                Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(email__icontains=q)
            )
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = "Users"
        ctx["search_query"] = self.request.GET.get("q", "")
        return ctx


class UserCreateView(LoginRequiredMixin, View):
    template_name = "accounts/user_form.html"

    def _form(self, request, data=None):
        from apps.core.access import is_platform_admin
        return UserCreateForm(data, platform_admin=is_platform_admin(request.user))

    def get(self, request):
        return render(request, self.template_name, {
            "form": self._form(request),
            "page_title": "Create User",
        })

    def post(self, request):
        from django.db import transaction
        from apps.organizations.models import CompanyMembership
        company = request.active_company
        if company is None:
            messages.error(request, "Switch to a company before adding users to it.")
            return redirect("accounts:user_list")
        form = self._form(request, request.POST)
        if form.is_valid():
            with transaction.atomic():
                user = form.save()
                CompanyMembership.objects.create(
                    user=user, company=company, role=form.cleaned_data["company_role"], is_active=True)
            messages.success(request, f"User {user.full_name} created and added to {company.name}.")
            return redirect("accounts:user_detail", pk=user.pk)
        return render(request, self.template_name, {"form": form})


class UserDetailView(LoginRequiredMixin, DetailView):
    model = User

    def get_queryset(self):
        from django.db.models import Q
        company = self.request.active_company
        qs = User.objects.all()
        if company:
            qs = qs.filter(
                Q(memberships__company=company, memberships__is_active=True)
                | Q(pk=self.request.user.pk)
            ).distinct()
        return qs
    template_name = "accounts/user_detail.html"
    context_object_name = "profile_user"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"User — {self.object.full_name}"
        # Only this company's seat: what the person does in other companies is
        # none of this company's business.
        ctx["memberships"] = self.object.memberships.select_related("company", "branch").filter(
            company=self.request.active_company)
        company = self.request.active_company
        ctx["member_active"] = bool(company) and self.object.memberships.filter(
            company=company, is_active=True).exists()
        return ctx


class UserEditView(LoginRequiredMixin, View):
    """Edit a colleague's seat in the active company.

    A company admin changes the membership only: role, branch, department
    (access is switched on and off from the user list). The person's own
    details -- name, email, username, phone, profile -- are theirs: only they
    (on their profile) or a platform administrator change them. Letting any
    company admin edit them made "add the victim to my company, change their
    email to mine, reset the password" an account takeover, and let one
    company rewrite what every other company the person works for sees.
    """
    template_name = "accounts/user_form.html"

    def _target(self, request, pk):
        from apps.core.access import is_platform_admin
        user = get_object_or_404(_company_members(request), pk=pk)
        if is_platform_admin(user) and not is_platform_admin(request.user):
            from django.http import Http404
            raise Http404
        return user

    def _form(self, request, user, data=None, files=None):
        from apps.core.access import is_platform_admin
        from apps.organizations.models import CompanyMembership
        company = getattr(request, "active_company", None)
        membership = (CompanyMembership.objects.filter(user=user, company=company).select_related(
            "company", "user").first() if company is not None else None)
        editing_self = user.pk == request.user.pk
        if editing_self or is_platform_admin(request.user):
            # The person themself, or a platform admin: the whole record.
            if membership is not None:
                form = MemberEditForm(data, files, instance=user)
                form.add_role_field(membership, editing_self=editing_self)
            else:
                form = UserEditForm(data, files, instance=user)
            return form
        if membership is None:
            from django.http import Http404
            raise Http404
        return MembershipEditForm(data, membership=membership, editing_self=False)

    def get(self, request, pk):
        user = self._target(request, pk)
        return render(request, self.template_name, {
            "form": self._form(request, user),
            "profile_user": user,
            "page_title": f"Edit User — {user.full_name}",
        })

    def post(self, request, pk):
        user = self._target(request, pk)
        form = self._form(request, user, request.POST, request.FILES)
        if form.is_valid():
            from django.db import transaction
            last_admin = False
            with transaction.atomic():
                membership = getattr(form, "membership", None)
                role = form.cleaned_data.get("company_role")
                if membership is not None and membership.role == "ADMIN" and role and role != "ADMIN":
                    # Two admins demoting each other at once would each pass the
                    # form's check. Claim the company and count the admins again.
                    from apps.organizations.models import Company, CompanyMembership
                    Company.objects.filter(pk=membership.company_id).update(
                        updated_at=timezone.now())
                    others = CompanyMembership.objects.filter(
                        company=membership.company_id, role="ADMIN", is_active=True,
                        user__is_active=True).exclude(pk=membership.pk)
                    last_admin = not others.exists()
                if last_admin:
                    # Leave the transaction before rendering: the page runs
                    # queries, which a rolled-back block would refuse.
                    transaction.set_rollback(True)
                else:
                    self._save_member(request, form, membership, user)
            if last_admin:
                form.add_error("company_role",
                               f"{user.full_name} is the only admin of "
                               f"{membership.company.name}. Make someone else an admin first.")
                return render(request, self.template_name, {"form": form, "profile_user": user})
            messages.success(request, "User updated successfully.")
            return redirect("accounts:user_detail", pk=pk)
        return render(request, self.template_name, {"form": form, "profile_user": user})

    def _save_member(self, request, form, membership, user):
        old_role = membership.role if membership is not None else None
        old_display = membership.get_role_display() if membership is not None else ""
        form.save()
        role = form.cleaned_data.get("company_role")
        if membership is not None and role and role != old_role:
            membership.role = role
            membership.save(update_fields=["role"])
            messages.success(request, f"{user.full_name}'s role in {membership.company.name} "
                                      f"changed from {old_display} to {membership.get_role_display()}.")


class UserAddExistingView(LoginRequiredMixin, View):
    """Invite somebody who may already have a login to the active company.

    People work for more than one company; creating a second account for
    them is impossible (the email is taken) and would split their history in
    two anyway. But a company must not be able to take a person in without
    asking: anybody can register and set up a company, and the "admin" of
    that company could otherwise pull a stranger in by email and manage them.
    So this only records an invitation. The person accepts it, signed in as
    themselves, from the emailed link or from Invitations on their profile;
    only then do they become a member.

    The answer here is the same whatever email is typed -- existing or not,
    already a member or not -- so the screen can't be used to find out who
    has an account, or what their name is.
    """
    template_name = "accounts/user_form.html"
    SAME_ANSWER = "If that person has a Mofisi account, they've been invited."

    def get(self, request):
        return render(request, self.template_name, {
            "form": AddExistingUserForm(company=request.active_company),
            "page_title": "Invite an existing user",
        })

    def post(self, request):
        from django.db import transaction
        from apps.organizations.models import CompanyMembership
        from .models import CompanyInvitation
        company = request.active_company
        if company is None:
            messages.error(request, "Switch to a company before adding people to it.")
            return redirect("accounts:user_list")
        form = AddExistingUserForm(request.POST, company=company)
        if not form.is_valid():
            return render(request, self.template_name, {
                "form": form, "page_title": "Invite an existing user"})
        email = form.cleaned_data["email"]
        role = form.cleaned_data["company_role"]
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        already = user is not None and CompanyMembership.objects.filter(
            user=user, company=company).exists()
        if not already:
            with transaction.atomic():
                # One open invitation per person per company: asking again
                # replaces the old one (new role, new link, new 14 days).
                CompanyInvitation.objects.filter(
                    company=company, email__iexact=email, accepted_at__isnull=True,
                    declined_at__isnull=True).update(declined_at=timezone.now())
                invitation = CompanyInvitation.objects.create(
                    company=company, email=email, role=role, invited_by=request.user)
            if user is not None:
                link = absolute_link(f"/auth/invitations/{invitation.token}/", request)
                if link:
                    from django.core.mail import send_mail
                    send_mail(
                        subject=f"Invitation to join {company.name} — Mofisi",
                        message=(f"{request.user.full_name} has invited you to join {company.name} "
                                 f"as {invitation.get_role_display()}.\n\nSign in and accept or "
                                 f"decline here: {link}\n\nNothing changes unless you accept. "
                                 "The invitation expires in 14 days."),
                        from_email=None, recipient_list=[user.email], fail_silently=True,
                    )
        messages.success(request, self.SAME_ANSWER)
        return redirect("accounts:user_list")


class InvitationListView(LoginRequiredMixin, View):
    """The signed-in person's open invitations, to accept or decline."""
    template_name = "accounts/invitations.html"

    def get(self, request):
        from .models import CompanyInvitation
        return render(request, self.template_name, {
            "invitations": CompanyInvitation.pending_for(request.user),
            "page_title": "Invitations",
        })


class InvitationRespondView(LoginRequiredMixin, View):
    """Accept or decline one invitation.

    The link alone proves nothing: it is matched against the email of the
    person signed in, so only the person it was addressed to can act on it.
    Somebody else who opens the link is told it isn't theirs.
    """
    template_name = "accounts/invitations.html"
    NOT_YOURS = ("This invitation isn't for the account you are signed in with, or it has "
                 "expired or been answered already. Sign in with the email it was sent to, "
                 "or ask the company for a new one.")

    def _invitation(self, request, token):
        from .models import CompanyInvitation
        return CompanyInvitation.pending_for(request.user).filter(token=token).first()

    def get(self, request, token):
        invitation = self._invitation(request, token)
        if invitation is None:
            messages.error(request, self.NOT_YOURS)
            return redirect("accounts:invitations")
        return render(request, self.template_name, {
            "invitations": [invitation], "page_title": "Invitation"})

    def post(self, request, token):
        from django.db import transaction
        from apps.organizations.models import CompanyMembership
        from .models import CompanyInvitation
        invitation = self._invitation(request, token)
        if invitation is None:
            messages.error(request, self.NOT_YOURS)
            return redirect("accounts:invitations")
        if request.POST.get("decision") == "decline":
            invitation.declined_at = timezone.now()
            invitation.save(update_fields=["declined_at"])
            messages.info(request, f"You declined the invitation to {invitation.company.name}.")
            return redirect("accounts:invitations")
        existing = CompanyMembership.objects.filter(user=request.user, company=invitation.company).first()
        if existing is not None and not existing.is_active:
            # A disabled seat is not re-opened by an invitation: the company
            # turned it off, and turns it back on from its user list.
            messages.error(request, f"Your access to {invitation.company.name} was disabled by its "
                                    "administrators. Ask them to re-enable it.")
            return redirect("accounts:invitations")
        with transaction.atomic():
            membership = existing or CompanyMembership.objects.create(
                user=request.user, company=invitation.company, role=invitation.role, is_active=True)
            now = timezone.now()
            invitation.accepted_at = now
            invitation.save(update_fields=["accepted_at"])
            CompanyInvitation.objects.filter(
                company=invitation.company, email__iexact=invitation.email,
                accepted_at__isnull=True, declined_at__isnull=True).exclude(
                pk=invitation.pk).update(declined_at=now)
        request.session["active_company_id"] = str(invitation.company.id)
        request.session.pop("active_branch_id", None)
        messages.success(request, f"You joined {invitation.company.name} as "
                                  f"{membership.get_role_display()}.")
        return redirect("dashboard:index")


def _last_active_admin(membership):
    """Whether `membership` is the company's only active ADMIN seat."""
    from apps.organizations.models import CompanyMembership
    if membership.role != "ADMIN" or not membership.is_active:
        return False
    return not CompanyMembership.objects.filter(
        company=membership.company, role="ADMIN", is_active=True,
        user__is_active=True).exclude(pk=membership.pk).exists()


@login_required
@require_POST
def toggle_user_active(request, pk):
    """Disable or re-enable someone's access to the active company.

    It changes their membership here, not their account, so one company can't
    lock a person out of another company they also work for.
    """
    from apps.core.access import is_platform_admin
    from apps.organizations.models import CompanyMembership
    user = get_object_or_404(_company_members(request), pk=pk)
    if user == request.user:
        messages.error(request, "You cannot disable your own access.")
        return redirect("accounts:user_detail", pk=pk)
    if is_platform_admin(user) and not is_platform_admin(request.user):
        messages.error(request, "Platform administrators can't be disabled from a company.")
        return redirect("accounts:user_list")
    membership = CompanyMembership.objects.filter(user=user, company=request.active_company).first()
    if membership is None:
        messages.error(request, f"{user.full_name} isn't a member of {request.active_company}.")
        return redirect("accounts:user_list")
    from django.db import transaction
    from apps.organizations.models import Company
    with transaction.atomic():
        # Two admins disabling each other at the same moment would each see
        # the other still active. Claim the company row first, so the second
        # request waits and then finds it would leave nobody in charge.
        Company.objects.filter(pk=membership.company_id).update(updated_at=timezone.now())
        membership.refresh_from_db()
        if _last_active_admin(membership):
            # Applies to platform admins too: a company with no active admin has
            # nobody left who can manage its users, branches or company record.
            messages.error(request, f"{user.full_name} is the only active admin of "
                                    f"{membership.company.name}. Make someone else an admin first.")
            return redirect("accounts:user_list")
        membership.is_active = not membership.is_active
        membership.save(update_fields=["is_active"])
    status = "enabled" if membership.is_active else "disabled"
    messages.success(request, f"{user.full_name}'s access to {request.active_company.name} has been {status}.")
    return redirect("accounts:user_list")
