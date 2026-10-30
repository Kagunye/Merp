"""Custom User model and authentication-related models."""
import uuid
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _


class User(AbstractUser):
    """Custom user model extending Django's AbstractUser."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(_("email address"), unique=True)
    phone = models.CharField(max_length=30, blank=True, default="")
    avatar = models.ImageField(upload_to="users/avatars/", null=True, blank=True)
    job_title = models.CharField(max_length=200, blank=True, default="")
    bio = models.TextField(blank=True, default="")
    is_system_admin = models.BooleanField(default=False)
    theme_preference = models.CharField(
        max_length=10,
        choices=[("light", "Light"), ("dark", "Dark"), ("system", "System")],
        default="light",
    )
    timezone = models.CharField(max_length=50, default="Africa/Nairobi")
    date_of_birth = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username", "first_name", "last_name"]

    class Meta:
        verbose_name = "User"
        verbose_name_plural = "Users"
        ordering = ["first_name", "last_name"]

    def __str__(self):
        return self.get_full_name() or self.email

    def save(self, *args, **kwargs):
        # Emails are stored lower-case. Sign-in already ignores case, and
        # keeping one spelling means the unique index (case-sensitive on
        # Postgres) actually stops Jane@ and jane@ becoming two accounts.
        if self.email:
            self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        return self.get_full_name() or self.email

    @property
    def initials(self):
        first = self.first_name[:1].upper() if self.first_name else ""
        last = self.last_name[:1].upper() if self.last_name else ""
        return first + last or self.email[:2].upper()

    @property
    def default_company(self):
        membership = self.memberships.filter(is_default=True, is_active=True).first()
        if membership:
            return membership.company
        membership = self.memberships.filter(is_active=True).first()
        return membership.company if membership else None

    def has_company_access(self, company):
        if self.is_system_admin or self.is_superuser:
            return True
        return self.memberships.filter(company=company, is_active=True).exists()

    def get_company_role(self, company):
        membership = self.memberships.filter(company=company, is_active=True).first()
        return membership.role if membership else None


class LoginAttempt(models.Model):
    """Track login attempts for security monitoring."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    email = models.EmailField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default="")
    success = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    failure_reason = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["email", "created_at"]),
            models.Index(fields=["ip_address", "created_at"]),
        ]

    def __str__(self):
        status = "✓" if self.success else "✗"
        return f"{status} {self.email} — {self.created_at:%Y-%m-%d %H:%M}"


class PasswordResetToken(models.Model):
    """Password reset tokens."""
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)

    def is_valid(self):
        from django.utils import timezone
        return not self.used and self.expires_at > timezone.now()

    def __str__(self):
        return f"Reset token for {self.user.email}"


INVITATION_LIFETIME_DAYS = 14


class CompanyInvitation(models.Model):
    """An offer of a seat in a company, waiting for the person to say yes.

    "Add an existing user" used to create the membership on the spot. That
    let anybody who registered and set up a company of their own pull a
    stranger in by email, without asking them, and then -- as that stranger's
    "company admin" -- change their email and reset their password: a full
    account takeover. Now the admin only records an invitation. Nothing
    changes for the invited person until they sign in, as themselves, and
    accept it; only then is the membership created.

    The invitation is addressed to an email, not to an account, so creating
    one says nothing about whether the email has a login (the admin gets the
    same answer either way). It is accepted by whoever is signed in with that
    email, and only by them -- holding the link is not enough.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="invitations")
    email = models.EmailField()
    role = models.CharField(max_length=30, default="EMPLOYEE")
    invited_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="invitations_sent")
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    declined_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["email", "expires_at"])]

    def __str__(self):
        return f"Invitation to {self.company} for {self.email}"

    def save(self, *args, **kwargs):
        self.email = (self.email or "").strip().lower()
        if not self.expires_at:
            from datetime import timedelta
            from django.utils import timezone
            self.expires_at = timezone.now() + timedelta(days=INVITATION_LIFETIME_DAYS)
        super().save(*args, **kwargs)

    @property
    def is_pending(self):
        from django.utils import timezone
        return (self.accepted_at is None and self.declined_at is None
                and self.expires_at > timezone.now())

    def get_role_display(self):
        from apps.organizations.models import CompanyMembership
        return dict(CompanyMembership.ROLE_CHOICES).get(self.role, self.role)

    @classmethod
    def pending_for(cls, user):
        """Open invitations addressed to `user`'s email, newest first."""
        from django.utils import timezone
        if not getattr(user, "is_authenticated", False) or not user.email:
            return cls.objects.none()
        return cls.objects.filter(
            email__iexact=user.email, accepted_at__isnull=True, declined_at__isnull=True,
            expires_at__gt=timezone.now(), company__is_active=True,
        ).select_related("company", "invited_by")


class UserPreference(models.Model):
    """Extended user preferences as key-value pairs."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="preferences")
    default_page_size = models.PositiveSmallIntegerField(default=25)
    notification_email = models.BooleanField(default=True)
    notification_sms = models.BooleanField(default=False)
    notification_in_app = models.BooleanField(default=True)
    dashboard_layout = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"Preferences for {self.user}"
