from django.contrib import admin
from .models import TimesheetEntry


@admin.register(TimesheetEntry)
class TimesheetEntryAdmin(admin.ModelAdmin):
    list_display = ("user", "project", "entry_date", "hours", "status", "billable")
    list_filter = ("status", "billable", "entry_date")
    search_fields = ("description",)
