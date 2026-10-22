"""Authentication and user management forms."""
from django import forms
from django.contrib.auth.forms import PasswordChangeForm

from .models import User


class LoginForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={
            "placeholder": "Email address",
            "autofocus": True,
            "class": "w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent",
        })
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            "placeholder": "Password",
            "class": "w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent",
        })
    )
    remember_me = forms.BooleanField(required=False)


class PasswordResetRequestForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"placeholder": "Enter your email address"})
    )


class PasswordResetConfirmForm(forms.Form):
    password = forms.CharField(
        label="New Password",
        min_length=8,
        widget=forms.PasswordInput(attrs={"placeholder": "New password"}),
    )
    password_confirm = forms.CharField(
        label="Confirm Password",
        widget=forms.PasswordInput(attrs={"placeholder": "Confirm new password"}),
    )

    def clean(self):
        cd = super().clean()
        if cd.get("password") != cd.get("password_confirm"):
            raise forms.ValidationError("Passwords do not match.")
        return cd


def _role_choices():
    from apps.organizations.models import CompanyMembership
    return CompanyMembership.ROLE_CHOICES


class _CompanyRoleMixin(forms.Form):
    """Adds the colleague's role in the active company. `is_system_admin`
    (access to every company) is only offered to platform administrators."""

    company_role = forms.ChoiceField(label="Role in this company", choices=())

    def __init__(self, *args, allow_system_admin=False, initial_role=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["company_role"].choices = _role_choices()
        if initial_role:
            self.fields["company_role"].initial = initial_role
        if not allow_system_admin:
            self.fields.pop("is_system_admin", None)


class UserCreateForm(_CompanyRoleMixin, forms.ModelForm):
    password = forms.CharField(
        min_length=8,
        widget=forms.PasswordInput(attrs={"placeholder": "Set a password"}),
    )

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "username", "phone", "job_title", "is_system_admin"]

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        if commit:
            user.save()
        return user


class UserEditForm(forms.ModelForm):
    """Self-service profile edit — no role or platform fields."""
    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "username", "phone", "job_title", "bio", "avatar", "timezone", "theme_preference"]


class UserAdminEditForm(_CompanyRoleMixin, forms.ModelForm):
    """A company admin editing a colleague: details plus their role here."""
    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "username", "phone", "job_title", "is_system_admin"]


class ChangePasswordForm(PasswordChangeForm):
    pass


class RegisterForm(forms.ModelForm):
    """Public self-service registration form.

    Collects the minimum needed to create a login — the user later picks
    (or is assigned to) a company via Organizations.
    """
    password = forms.CharField(
        label="Password",
        min_length=8,
        widget=forms.PasswordInput(attrs={"placeholder": "At least 8 characters"}),
    )
    password_confirm = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(attrs={"placeholder": "Re-enter your password"}),
    )
    terms_accepted = forms.BooleanField(
        label="I agree to the Terms of Service and Privacy Policy.",
        required=True,
    )

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "phone"]

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean(self):
        cd = super().clean()
        if cd.get("password") and cd.get("password") != cd.get("password_confirm"):
            self.add_error("password_confirm", "Passwords do not match.")
        return cd

    def save(self, commit=True):
        user = super().save(commit=False)
        user.username = self.cleaned_data["email"]
        user.set_password(self.cleaned_data["password"])
        if commit:
            user.save()
        return user
