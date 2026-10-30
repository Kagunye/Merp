"""Authentication and user management forms."""
from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import PasswordChangeForm

from .models import User


def check_password_strength(form, field, user):
    """Run AUTH_PASSWORD_VALIDATORS on `field` and attach any failure to it.

    The settings list (minimum length, not too like the name or email, not a
    common password, not all digits) is only enforced where a form asks for
    it; Django's own admin forms do, these hand-built ones did not, so
    "password" or "12345678" was accepted on sign-up, on a reset and when an
    admin created a colleague.
    """
    password = form.cleaned_data.get(field)
    if not password:
        return
    try:
        password_validation.validate_password(password, user)
    except forms.ValidationError as exc:
        form.add_error(field, exc)


def clean_unique_email(form, field="email"):
    """The email, lower-cased, if no *other* account uses it in any case.

    Sign-in matches emails case-insensitively (apps.accounts.backends), so
    Jane@Acme.com and jane@acme.com as two accounts would make one of them
    impossible to sign in to -- and the database's own unique check is
    case-sensitive on Postgres, so it would let both through.
    """
    email = (form.cleaned_data.get(field) or "").strip().lower()
    if not email:
        return email
    twin = User.objects.filter(email__iexact=email).exclude(pk=form.instance.pk).first()
    if twin:
        raise forms.ValidationError(
            "Another account already uses this email address. Each person signs "
            "in with their own email; use a different one.")
    return email


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
    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

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
        check_password_strength(self, "password", self.user)
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

    def clean_email(self):
        return clean_unique_email(self)

    def clean(self):
        cd = super().clean()
        # Validated against the details typed in, so a password that is just
        # the new colleague's name or email is refused too.
        probe = User(email=cd.get("email") or "", username=cd.get("username") or "",
                     first_name=cd.get("first_name") or "", last_name=cd.get("last_name") or "")
        check_password_strength(self, "password", probe)
        return cd

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

    def clean_email(self):
        if self.fields["email"].disabled:
            return self.instance.email
        return clean_unique_email(self)

    def clean_timezone(self):
        from apps.organizations.validators import validate_timezone
        return validate_timezone((self.cleaned_data.get("timezone") or "").strip())


class MemberRoleMixin:
    """Adds "Role in this company" to the member edit form.

    The role is the membership's, not the account's, so it is read from and
    written to the active company's CompanyMembership only. Two guards:

    * nobody changes their own role (the field is shown, locked) -- an admin
      demoting themselves by accident could leave the company unmanaged, and
      anyone promoting themselves is the thing the role table exists to stop;
    * the company's last active ADMIN can't be demoted: someone has to be
      left who can manage users, branches and the company record.
    """

    def add_role_field(self, membership, editing_self):
        from apps.organizations.models import CompanyMembership
        self.membership = membership
        self.fields["company_role"] = forms.ChoiceField(
            label="Role in this company", choices=CompanyMembership.ROLE_CHOICES,
            initial=membership.role)
        if editing_self:
            self.fields["company_role"].disabled = True
            self.fields["company_role"].help_text = (
                "You can't change your own role. Ask another admin of this company.")

    def clean_company_role(self):
        from apps.organizations.models import CompanyMembership
        role = self.cleaned_data.get("company_role")
        membership = self.membership
        if self.fields["company_role"].disabled:
            return membership.role
        if membership.role == "ADMIN" and role != "ADMIN":
            others = CompanyMembership.objects.filter(
                company=membership.company, role="ADMIN", is_active=True,
                user__is_active=True).exclude(pk=membership.pk)
            if not others.exists():
                raise forms.ValidationError(
                    f"{membership.user.full_name} is the only admin of "
                    f"{membership.company.name}. Make someone else an admin first.")
        return role


class MemberEditForm(MemberRoleMixin, UserEditForm):
    """UserEditForm plus the member's role in the active company."""


class AddExistingUserForm(forms.Form):
    """A company admin adds somebody who already has a login."""
    email = forms.EmailField(
        label="Their email address",
        help_text="The email they already sign in with. To add someone new, use New user instead.")
    company_role = forms.ChoiceField(label="Role in this company", initial="EMPLOYEE")

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.organizations.models import CompanyMembership
        self.company = company
        self.fields["company_role"].choices = CompanyMembership.ROLE_CHOICES
        self.user = None

    def clean_email(self):
        from apps.organizations.models import CompanyMembership
        email = (self.cleaned_data.get("email") or "").strip()
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is None:
            raise forms.ValidationError(
                "No active account uses this email. Check the spelling, or create the "
                "account with New user.")
        existing = CompanyMembership.objects.filter(user=user, company=self.company).first()
        if existing is not None:
            if existing.is_active:
                raise forms.ValidationError(
                    f"{user.full_name} is already a member of {self.company.name} "
                    f"({existing.get_role_display()}). Change their role from Edit instead.")
            raise forms.ValidationError(
                f"{user.full_name} was a member of {self.company.name} and is disabled. "
                "Re-enable them from the user list instead.")
        self.user = user
        return email


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
        probe = User(email=cd.get("email") or "", username=cd.get("email") or "",
                     first_name=cd.get("first_name") or "", last_name=cd.get("last_name") or "")
        check_password_strength(self, "password", probe)
        return cd

    def save(self, commit=True):
        user = super().save(commit=False)
        user.username = self.cleaned_data["email"]
        user.set_password(self.cleaned_data["password"])
        if commit:
            user.save()
        return user
