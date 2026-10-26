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


class UserCreateForm(forms.ModelForm):
    """A company admin adds a colleague to the company they're working in.

    `is_system_admin` (platform-wide access) is only offered to platform admins.
    """
    password = forms.CharField(
        min_length=8,
        widget=forms.PasswordInput(attrs={"placeholder": "Set a password"}),
    )
    company_role = forms.ChoiceField(label="Role in this company", initial="EMPLOYEE")

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "username", "phone", "job_title", "is_system_admin"]

    def __init__(self, *args, platform_admin=False, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.organizations.models import CompanyMembership
        self.fields["company_role"].choices = CompanyMembership.ROLE_CHOICES
        if not platform_admin:
            del self.fields["is_system_admin"]

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password"])
        if commit:
            user.save()
        return user


class UserEditForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "username", "phone", "job_title", "bio", "avatar", "timezone", "theme_preference"]


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
