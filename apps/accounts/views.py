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
    ChangePasswordForm, LoginForm, PasswordResetConfirmForm,
    PasswordResetRequestForm, RegisterForm, UserCreateForm, UserEditForm,
)
from .models import LoginAttempt, PasswordResetToken, User


def _get_client_ip(request):
    x_forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


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
        return ctx


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
            email = form.cleaned_data["email"]
            try:
                user = User.objects.get(email=email, is_active=True)
                token = PasswordResetToken.objects.create(
                    user=user,
                    expires_at=timezone.now() + timedelta(hours=24),
                )
                reset_url = request.build_absolute_uri(f"/auth/password-reset/{token.token}/")
                # Send email (using Django's built-in mechanism)
                from django.core.mail import send_mail
                send_mail(
                    subject="Password Reset Request — Mofisi",
                    message=f"Click to reset your password: {reset_url}\n\nThis link expires in 24 hours.",
                    from_email=None,
                    recipient_list=[email],
                    fail_silently=True,
                )
            except User.DoesNotExist:
                pass  # Don't reveal whether email exists
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
            "form": PasswordResetConfirmForm(),
            "token": token,
        })

    def post(self, request, token):
        reset_token = get_object_or_404(PasswordResetToken, token=token)
        if not reset_token.is_valid():
            messages.error(request, "This reset link has expired.")
            return redirect("accounts:password_reset")
        form = PasswordResetConfirmForm(request.POST)
        if form.is_valid():
            reset_token.user.set_password(form.cleaned_data["password"])
            reset_token.user.save()
            reset_token.used = True
            reset_token.save()
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
        ctx["memberships"] = self.object.memberships.select_related("company", "branch").all()
        company = self.request.active_company
        ctx["member_active"] = bool(company) and self.object.memberships.filter(
            company=company, is_active=True).exists()
        return ctx


class UserEditView(LoginRequiredMixin, View):
    """Edit a colleague's details. Sign-in identity (email, username) is locked
    when they also belong to another company: changing it there would let
    one company take over an account another company relies on."""
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
        form = UserEditForm(data, files, instance=user)
        company = getattr(request, "active_company", None)
        shared = (company is not None and not is_platform_admin(request.user)
                  and user.pk != request.user.pk
                  and user.memberships.filter(is_active=True).exclude(company=company).exists())
        if shared:
            for name in ("email", "username"):
                form.fields[name].disabled = True
                form.fields[name].help_text = "This person also belongs to another company, so only they can change it."
        return form

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
            form.save()
            messages.success(request, "User updated successfully.")
            return redirect("accounts:user_detail", pk=pk)
        return render(request, self.template_name, {"form": form, "profile_user": user})


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
    membership.is_active = not membership.is_active
    membership.save(update_fields=["is_active"])
    status = "enabled" if membership.is_active else "disabled"
    messages.success(request, f"{user.full_name}'s access to {request.active_company.name} has been {status}.")
    return redirect("accounts:user_list")
