from django import forms
from .models import Ticket, TicketComment


class TicketForm(forms.ModelForm):
    class Meta:
        model = Ticket
        fields = ["reference", "subject", "description", "customer",
                  "requester_name", "requester_email", "channel",
                  "priority", "assigned_to"]
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}


class TicketCommentForm(forms.ModelForm):
    class Meta:
        model = TicketComment
        fields = ["body", "is_internal"]
        widgets = {"body": forms.Textarea(attrs={"rows": 3, "placeholder": "Write a reply..."})}
