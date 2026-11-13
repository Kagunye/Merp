"""Forms for the KRA eTIMS screens."""
import re

from django import forms

from apps.core.forms import style_field

from .models import EtimsDevice, EtimsItem, EtimsSettings

SECRET_FIELDS = ("oauth_consumer_secret", "pergamon_api_key")


class EtimsSettingsForm(forms.ModelForm):
    """The company's eTIMS connection. Secrets are never sent back to the
    browser: a blank secret keeps the saved one."""
    sales_type = forms.ChoiceField(
        choices=[("N", "N — Normal (real receipts)"), ("T", "T — Training (practice receipts)")],
        help_text="Use Training while practising in the sandbox.")

    class Meta:
        model = EtimsSettings
        fields = ["enabled", "backend", "environment", "tin", "base_url", "oauth_consumer_key",
                  "oauth_consumer_secret", "pergamon_endpoint", "pergamon_api_key", "auto_send",
                  "sales_type", "timeout_seconds"]
        labels = {"oauth_consumer_key": "GavaConnect consumer key",
                  "oauth_consumer_secret": "GavaConnect consumer secret",
                  "pergamon_endpoint": "Pergamon fiscal endpoint",
                  "pergamon_api_key": "Pergamon API key",
                  "timeout_seconds": "Timeout (seconds)",
                  "enabled": "Send documents to KRA"}
        help_texts = {"oauth_consumer_key": "Only if KRA puts OAuth in front of OSCU for you.",
                      "pergamon_endpoint": "The URL Pergamon gives you for fiscalising documents."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in SECRET_FIELDS:
            saved = bool(getattr(self.instance, name, ""))
            self.fields[name].widget = forms.PasswordInput(render_value=False, attrs={
                "autocomplete": "new-password",
                "placeholder": "Saved — leave blank to keep" if saved else ""})
            self.fields[name].required = False
            if saved:
                self.fields[name].help_text = "A value is saved. Leave blank to keep it."
        for field in self.fields.values():
            style_field(field)

    def _keep(self, name):
        value = (self.cleaned_data.get(name) or "").strip()
        return value or getattr(self.instance, name, "") or ""

    def clean_oauth_consumer_secret(self):
        return self._keep("oauth_consumer_secret")

    def clean_pergamon_api_key(self):
        return self._keep("pergamon_api_key")

    def clean_tin(self):
        pin = (self.cleaned_data.get("tin") or "").strip().upper()
        if pin and not re.fullmatch(r"[A-Z]\d{9}[A-Z]", pin):
            raise forms.ValidationError("A KRA PIN is a letter, nine digits and a letter, e.g. P051234567X.")
        return pin

    def clean(self):
        data = super().clean()
        if data.get("enabled") and data.get("backend") == "PERGAMON":
            if not data.get("pergamon_endpoint"):
                self.add_error("pergamon_endpoint", "Pergamon needs the endpoint they issued you.")
            if not data.get("pergamon_api_key"):
                self.add_error("pergamon_api_key", "Pergamon needs the API key they issued you.")
        if data.get("enabled") and data.get("backend") == "VSCU" and not data.get("base_url"):
            self.add_error("base_url", "VSCU runs on your own server: enter its address, "
                                       "e.g. http://localhost:8088")
        return data


class EtimsDeviceForm(forms.ModelForm):
    class Meta:
        model = EtimsDevice
        fields = ["branch", "bhf_id", "device_serial", "is_active"]
        labels = {"is_active": "In use"}

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        from apps.organizations.models import Branch
        self.fields["branch"].queryset = Branch.objects.filter(company=company, is_active=True).order_by("name")
        if self.instance.pk and self.instance.initialized_at:
            # KRA bound the key to this bhfId and serial: changing them needs a new device.
            for name in ("bhf_id", "device_serial"):
                self.fields[name].disabled = True
                self.fields[name].help_text = "Fixed once KRA has initialised the device."
        for field in self.fields.values():
            style_field(field)

    def clean_bhf_id(self):
        bhf = (self.cleaned_data.get("bhf_id") or "").strip()
        if not re.fullmatch(r"\d{2}", bhf):
            raise forms.ValidationError("Two digits: 00 for head office, 01, 02 … for branches.")
        clash = EtimsDevice.objects.filter(company=self.company, bhf_id=bhf).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(f"Another device already uses bhfId {bhf}.")
        return bhf

    def clean_device_serial(self):
        serial = (self.cleaned_data.get("device_serial") or "").strip()
        if not serial:
            raise forms.ValidationError("Enter the device serial KRA registered for this branch.")
        return serial

    def clean_branch(self):
        branch = self.cleaned_data.get("branch")
        if branch and EtimsDevice.objects.filter(company=self.company, branch=branch).exclude(
                pk=self.instance.pk).exists():
            raise forms.ValidationError("This branch already has a device.")
        return branch


class EtimsItemForm(forms.ModelForm):
    """KRA classes and units of one product, edited inline on the items list."""
    class Meta:
        model = EtimsItem
        fields = ["item_cls_cd", "item_ty_cd", "pkg_unit_cd", "qty_unit_cd"]

    def clean_item_cls_cd(self):
        code = (self.cleaned_data.get("item_cls_cd") or "").strip()
        if not re.fullmatch(r"\d{8,10}", code):
            raise forms.ValidationError("The item class is KRA's 8–10 digit code.")
        return code

    def clean_pkg_unit_cd(self):
        return (self.cleaned_data.get("pkg_unit_cd") or "").strip().upper()

    def clean_qty_unit_cd(self):
        return (self.cleaned_data.get("qty_unit_cd") or "").strip().upper()


class PinCheckForm(forms.Form):
    pin = forms.CharField(label="KRA PIN", max_length=11, min_length=11,
                          help_text="A letter, nine digits and a letter, e.g. P051234567X.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_field(self.fields["pin"])
        self.fields["pin"].widget.attrs.update({"autocomplete": "off", "class": "input mono uppercase"})

    def clean_pin(self):
        pin = self.cleaned_data["pin"].strip().upper()
        if not re.fullmatch(r"[A-Z]\d{9}[A-Z]", pin):
            raise forms.ValidationError("That is not the shape of a KRA PIN.")
        return pin
