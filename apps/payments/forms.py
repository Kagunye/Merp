from django import forms

from .models import PaymentGateway

FIELDS = {
    "MPESA": ["is_active", "environment", "consumer_key", "consumer_secret", "shortcode_type", "shortcode",
              "till_number", "passkey", "settlement_account", "callback_base"],
    "PESAPAL": ["is_active", "environment", "consumer_key", "consumer_secret", "settlement_account", "callback_base"],
    "DPO": ["is_active", "environment", "company_token", "service_type", "settlement_account", "callback_base"],
}
SECRETS = ("consumer_secret", "passkey", "company_token")


class GatewayForm(forms.ModelForm):
    class Meta:
        model = PaymentGateway
        fields = sorted({f for fs in FIELDS.values() for f in fs})

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        provider = self.instance.provider
        for name in list(self.fields):
            if name not in FIELDS[provider]:
                del self.fields[name]
        # Switch and environment first, then the keys, as the provider lists them.
        self.order_fields(FIELDS[provider])
        for name in SECRETS:
            if name in self.fields:
                # Never sent back to the browser; blank keeps what is stored.
                self.fields[name].widget = forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"})
                self.fields[name].help_text = "Stored. Leave blank to keep it." if getattr(self.instance, name) else ""
        if "settlement_account" in self.fields:
            from apps.banking.models import BankAccount
            self.fields["settlement_account"].queryset = BankAccount.objects.filter(company=company)
            self.fields["settlement_account"].empty_label = "Choose the bank account (M-Pesa: the till's M-Pesa account)"
        if "callback_base" in self.fields:
            self.fields["callback_base"].label = "Public address of this site"

    def clean(self):
        data = super().clean()
        for name in SECRETS:
            if name in self.fields and not data.get(name):
                data[name] = getattr(self.instance, name)
        live = data.get("environment") in ("SANDBOX", "LIVE") and data.get("is_active")
        provider = self.instance.provider
        if live:
            need = {"MPESA": ["consumer_key", "consumer_secret", "shortcode", "passkey"],
                    "PESAPAL": ["consumer_key", "consumer_secret"],
                    "DPO": ["company_token", "service_type"]}[provider]
            for name in need:
                if not data.get(name):
                    self.add_error(name, "Needed to switch this gateway on outside the simulator.")
            if provider == "MPESA" and data.get("shortcode_type") == "TILL" and not data.get("till_number"):
                self.add_error("till_number", "A Buy Goods gateway needs the till number.")
            base = data.get("callback_base") or ""
            if data.get("environment") == "LIVE" and base and not base.startswith("https://"):
                self.add_error("callback_base", "The providers only call back to an https:// address.")
        return data
