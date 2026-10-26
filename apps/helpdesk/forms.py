from django import forms
from .models import Ticket, TicketComment


class TicketForm(forms.ModelForm):
    class Meta:
        model = Ticket
        fields = ["reference", "subject", "description", "customer",
                  "requester_name", "requester_email", "channel",
                  "priority", "assigned_to"]
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.core.forms import company_users
        from apps.crm.models import Customer
        self.fields["customer"].queryset = Customer.objects.filter(company=company) if company else Customer.objects.none()
        self.fields["assigned_to"].queryset = company_users(company)


class TicketCommentForm(forms.ModelForm):
    class Meta:
        model = TicketComment
        fields = ["body", "is_internal"]
        widgets = {"body": forms.Textarea(attrs={"rows": 3, "placeholder": "Write a reply..."})}
