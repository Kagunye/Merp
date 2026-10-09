from django.contrib import admin
from .models import Ticket, TicketComment


class TicketCommentInline(admin.TabularInline):
    model = TicketComment
    extra = 0


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("reference", "subject", "status", "priority", "customer", "assigned_to", "opened_at")
    list_filter = ("status", "priority", "channel")
    search_fields = ("reference", "subject", "requester_email")
    inlines = [TicketCommentInline]
