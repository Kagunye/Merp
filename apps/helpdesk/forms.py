from django import forms
from .models import TICKET_PREFIX, Ticket, TicketComment


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
        self.company = company
        self.fields["customer"].queryset = Customer.objects.filter(company=company) if company else Customer.objects.none()
        self.fields["assigned_to"].queryset = company_users(company)
        # Left as shown, the reference is the next number in the TKT- series,
        # issued when the ticket is saved (see TicketCreateView.form_valid).
        self.fields["reference"].required = False
        self.fields["reference"].help_text = (
            "Leave as shown to take the next ticket number, or type your own.")

    def clean_reference(self):
        """Two tickets may not share a reference.

        A number from the company's own TKT- series is only the preview the
        form showed: if somebody saved with it meanwhile, the save takes the
        next one (SequenceCounter.claim_number). Only a reference typed by hand
        can clash, and that is refused here.
        """
        reference = (self.cleaned_data.get("reference") or "").strip()
        prefix = TICKET_PREFIX
        series = reference[len(prefix):] if reference.upper().startswith(prefix) else ""
        if self.instance._state.adding and series.isascii() and series.isdigit():
            return reference
        if reference and self.company is not None:
            twin = Ticket.objects.filter(company=self.company, reference__iexact=reference).exclude(
                pk=self.instance.pk).first()
            if twin:
                raise forms.ValidationError(
                    f"Ticket {twin.reference} already exists. Leave the reference as "
                    "suggested, or use another one.")
        return reference


class TicketCommentForm(forms.ModelForm):
    class Meta:
        model = TicketComment
        fields = ["body", "is_internal"]
        widgets = {"body": forms.Textarea(attrs={"rows": 3, "placeholder": "Write a reply..."})}
        # A reply of only spaces is stripped to nothing and lands here too.
        error_messages = {"body": {"required": "Write something before posting a reply."}}

    def clean_body(self):
        body = (self.cleaned_data.get("body") or "").strip()
        if not body:
            raise forms.ValidationError("Write something before posting a reply.")
        return body
