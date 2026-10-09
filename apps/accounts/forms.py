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
    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "username", "phone", "job_title", "bio", "avatar", "timezone", "theme_preference"]


class ChangePasswordForm(PasswordChangeForm):
    pass
