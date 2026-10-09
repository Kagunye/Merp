"""Authentication and user management views."""
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
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
        form = LoginForm(request.POST)
        ip = _get_client_ip(request)
        ua = request.META.get("HTTP_USER_AGENT", "")
        features = ["Finance", "Inventory", "HR & Payroll", "Reports & BI", "Point of Sale", "Procurement"]

        if form.is_valid():
            email = form.cleaned_data["email"]
            password = form.cleaned_data["password"]
            remember_me = form.cleaned_data.get("remember_me", False)

            user = authenticate(request, username=email, password=password)
            if user is not None:
                login(request, user)
                LoginAttempt.objects.create(
                    user=user, email=email, ip_address=ip,
                    user_agent=ua, success=True,
                )
                if not remember_me:
                    request.session.set_expiry(0)
                messages.success(request, f"Welcome back, {user.first_name or user.email}!")
                next_url = request.GET.get("next", "/dashboard/")
                return redirect(next_url)
            else:
                LoginAttempt.objects.create(
                    email=form.cleaned_data.get("email", ""),
                    ip_address=ip, user_agent=ua, success=False,
                    failure_reason="Invalid credentials",
                )
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
            login(request, user)
            messages.success(
                request,
                f"Welcome to MERP, {user.first_name or user.email}! "
                "Finish setting up your company to get started.",
            )
            return redirect("dashboard:index")
        return render(request, self.template_name, {"form": form})


class LogoutView(View):
    def post(self, request):
        logout(request)
        messages.info(request, "You have been logged out.")
        return redirect("accounts:login")

    def get(self, request):
        logout(request)
        return redirect("accounts:login")


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
                    subject="Password Reset Request — MERP",
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


class UserListView(LoginRequiredMixin, ListView):
    model = User
    template_name = "accounts/user_list.html"
    context_object_name = "users"
    paginate_by = 25

    def get_queryset(self):
        qs = User.objects.all().order_by("first_name", "last_name")
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

    def get(self, request):
        return render(request, self.template_name, {
            "form": UserCreateForm(),
            "page_title": "Create User",
        })

    def post(self, request):
        form = UserCreateForm(request.POST)
        if form.is_valid():
            user = form.save()
            messages.success(request, f"User {user.full_name} created successfully.")
            return redirect("accounts:user_detail", pk=user.pk)
        return render(request, self.template_name, {"form": form})


class UserDetailView(LoginRequiredMixin, DetailView):
    model = User
    template_name = "accounts/user_detail.html"
    context_object_name = "profile_user"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["page_title"] = f"User — {self.object.full_name}"
        ctx["memberships"] = self.object.memberships.select_related("company", "branch").all()
        return ctx


class UserEditView(LoginRequiredMixin, View):
    template_name = "accounts/user_form.html"

    def get(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        return render(request, self.template_name, {
            "form": UserEditForm(instance=user),
            "profile_user": user,
            "page_title": f"Edit User — {user.full_name}",
        })

    def post(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        form = UserEditForm(request.POST, request.FILES, instance=user)
        if form.is_valid():
            form.save()
            messages.success(request, "User updated successfully.")
            return redirect("accounts:user_detail", pk=pk)
        return render(request, self.template_name, {"form": form, "profile_user": user})


@login_required
def toggle_user_active(request, pk):
    user = get_object_or_404(User, pk=pk)
    if user == request.user:
        messages.error(request, "You cannot deactivate your own account.")
        return redirect("accounts:user_detail", pk=pk)
    user.is_active = not user.is_active
    user.save(update_fields=["is_active"])
    status = "activated" if user.is_active else "deactivated"
    messages.success(request, f"User {user.full_name} has been {status}.")
    return redirect("accounts:user_list")
