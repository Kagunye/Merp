from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User, LoginAttempt, PasswordResetToken


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ["email", "full_name", "is_active", "is_system_admin", "date_joined"]
    list_filter = ["is_active", "is_system_admin", "is_staff"]
    search_fields = ["email", "first_name", "last_name", "username"]
    ordering = ["email"]
    fieldsets = BaseUserAdmin.fieldsets + (
        ("Profile", {"fields": ("phone", "avatar", "job_title", "bio", "theme_preference", "timezone")}),
        ("Permissions", {"fields": ("is_system_admin",)}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ("Profile", {"fields": ("email", "first_name", "last_name", "phone")}),
    )


@admin.register(LoginAttempt)
class LoginAttemptAdmin(admin.ModelAdmin):
    list_display = ["email", "success", "ip_address", "created_at"]
    list_filter = ["success"]
    readonly_fields = ["email", "user", "ip_address", "user_agent", "success", "created_at"]
