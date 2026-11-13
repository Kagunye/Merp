"""Forms for the restaurant back office (head-office configuration of the till).

Every form takes the active company and limits each choice to that company's
own rows, so a tampered id from another company is refused as "not one of the
available choices" rather than saved.
"""
from decimal import Decimal

from django import forms
from django.db.models import Q

from apps.core.forms import date_input, style_field

from . import services
from .models import (POSMenu, POSMenuItem, POSMenuSection, POSPriceRule, POSSettings, POSStaff,
                     POSStation, POSTable)


def _style(form):
    for field in form.fields.values():
        style_field(field)


def _branches(company):
    return company.branches.filter(is_active=True).order_by("name")


class CompanyForm(forms.ModelForm):
    """A model form bound to one company: the instance is stamped with it."""

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if hasattr(self.instance, "company_id") and not self.instance.company_id:
            self.instance.company = company
        self.limit()
        _style(self)

    def limit(self):
        pass

    def unique_code(self, model, label):
        code = (self.cleaned_data.get("code") or "").strip()
        if not code:
            return code
        clash = model.objects.filter(company=self.company, code__iexact=code)
        if self.instance.pk and not self.instance._state.adding:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(f"Another {label} already uses the code {code}.")
        return code


class SettingsForm(CompanyForm):
    class Meta:
        model = POSSettings
        fields = ["receipt_header", "receipt_footer", "default_menu", "waiter_can_take_payment",
                  "require_table", "day_end_requires_closed_bills", "pin_idle_minutes"]
        widgets = {"receipt_header": forms.Textarea(attrs={"rows": 3}),
                   "receipt_footer": forms.Textarea(attrs={"rows": 3})}

    def limit(self):
        menus = POSMenu.objects.filter(company=self.company, is_active=True)
        if self.instance.default_menu_id:
            menus = POSMenu.objects.filter(company=self.company).filter(
                Q(is_active=True) | Q(pk=self.instance.default_menu_id))
        self.fields["default_menu"].queryset = menus
        self.fields["default_menu"].empty_label = "No default menu"

    def clean_pin_idle_minutes(self):
        value = self.cleaned_data.get("pin_idle_minutes")
        if value is not None and not 1 <= value <= 120:
            raise forms.ValidationError("Choose between 1 and 120 minutes.")
        return value


class StationForm(CompanyForm):
    class Meta:
        model = POSStation
        fields = ["name", "code", "kind", "branch", "warehouse", "menu", "printer_name", "is_active"]
        labels = {"is_active": "Switched on"}

    def limit(self):
        from apps.organizations.models import Warehouse
        self.fields["branch"].queryset = _branches(self.company)
        self.fields["warehouse"].queryset = Warehouse.objects.filter(
            company=self.company, is_active=True).select_related("branch").order_by("name")
        self.fields["warehouse"].label_from_instance = (
            lambda w: f"{w.name} ({w.branch.name if w.branch_id else 'shared'})")
        self.fields["warehouse"].empty_label = "The till's own choice"
        self.fields["menu"].queryset = POSMenu.objects.filter(company=self.company)
        self.fields["menu"].empty_label = "Head office's default menu"

    def clean_code(self):
        return self.unique_code(POSStation, "station")

    def clean(self):
        data = super().clean()
        branch, warehouse = data.get("branch"), data.get("warehouse")
        if branch and warehouse and warehouse.branch_id and warehouse.branch_id != branch.pk:
            self.add_error("warehouse", f"{warehouse.name} belongs to {warehouse.branch.name}; "
                                        f"choose a store at {branch.name} or a shared one.")
        menu = data.get("menu")
        if branch and menu and menu.pk and menu.branches.exists() and \
                not menu.branches.filter(pk=branch.pk).exists():
            self.add_error("menu", f"{menu.name} is not offered at {branch.name}.")
        return data


class TableForm(CompanyForm):
    class Meta:
        model = POSTable
        fields = ["name", "branch", "station", "area", "seats", "sort_order", "is_active"]
        labels = {"is_active": "In use", "sort_order": "Order on the floor plan"}

    def limit(self):
        self.fields["branch"].queryset = _branches(self.company)
        self.fields["station"].queryset = POSStation.objects.filter(
            company=self.company).select_related("branch")
        self.fields["station"].label_from_instance = lambda s: f"{s.name} ({s.branch.name})"
        self.fields["station"].empty_label = "Any station at the branch"

    def clean_seats(self):
        seats = self.cleaned_data.get("seats")
        if seats is not None and not 1 <= seats <= 100:
            raise forms.ValidationError("Seats has to be between 1 and 100.")
        return seats

    def clean(self):
        data = super().clean()
        branch, station = data.get("branch"), data.get("station")
        if branch and station and station.branch_id != branch.pk:
            self.add_error("station", f"{station.name} is at {station.branch.name}, "
                                      f"not {branch.name}.")
        name = (data.get("name") or "").strip()
        if branch and name:
            clash = POSTable.objects.filter(company=self.company, branch=branch,
                                            name__iexact=name, is_active=True)
            if self.instance.pk and not self.instance._state.adding:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                self.add_error("name", f"{branch.name} already has a table called {name}.")
        return data


class MenuForm(CompanyForm):
    class Meta:
        model = POSMenu
        fields = ["name", "description", "branches", "is_active"]
        widgets = {"branches": forms.CheckboxSelectMultiple}
        labels = {"is_active": "In use"}

    def limit(self):
        self.fields["branches"].queryset = _branches(self.company)


class ItemForm(forms.ModelForm):
    class Meta:
        model = POSMenuItem
        fields = ["product", "display_name", "price", "sort_order"]

    def __init__(self, *args, company, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.inventory.models import Product
        live = Q(is_active=True)
        if self.instance.pk and self.instance.product_id:
            live |= Q(pk=self.instance.product_id)     # keep a retired product editable
        self.fields["product"].queryset = Product.objects.filter(company=company).filter(
            live).order_by("name")
        self.fields["product"].label_from_instance = lambda p: f"{p.name} ({p.sku})"
        self.fields["product"].empty_label = "Choose a product"
        self.fields["sort_order"].required = False
        self.fields["sort_order"].label = "Position"
        self.fields["display_name"].label = "Name on the menu"
        _style(self)

    def clean_sort_order(self):
        value = self.cleaned_data.get("sort_order")
        return 0 if value is None else value

    def clean_price(self):
        price = self.cleaned_data.get("price")
        if price is not None and price < 0:
            raise forms.ValidationError("A price cannot be below nothing.")
        return price

    def clean(self):
        data = super().clean()
        product, price = data.get("product"), data.get("price")
        if product and price is None and not (product.selling_price or 0) > 0:
            self.add_error("price", f"{product.name} has no selling price on file: "
                                    "give this item a price.")
        return data


class RuleForm(CompanyForm):
    days = forms.TypedMultipleChoiceField(
        choices=list(enumerate(POSPriceRule.DAY_NAMES)), coerce=int,
        widget=forms.CheckboxSelectMultiple, label="Days it runs")

    class Meta:
        model = POSPriceRule
        fields = ["name", "discount_percent", "days", "start_time", "end_time", "valid_from",
                  "valid_to", "section", "category", "product", "branches", "is_active"]
        widgets = {"start_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
                   "end_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
                   "valid_from": date_input(), "valid_to": date_input(),
                   "branches": forms.CheckboxSelectMultiple}
        labels = {"discount_percent": "Discount (%)", "is_active": "In use",
                  "section": "Only this menu section", "category": "Only this category",
                  "product": "Only this product"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["days"] = self.instance.day_list if self.instance.days else list(range(7))

    def limit(self):
        from apps.inventory.models import Product, ProductCategory
        self.fields["section"].queryset = POSMenuSection.objects.filter(
            menu__company=self.company).select_related("menu").order_by("menu__name", "sort_order")
        self.fields["section"].label_from_instance = lambda s: f"{s.menu.name} > {s.name}"
        self.fields["category"].queryset = ProductCategory.objects.filter(company=self.company)
        self.fields["product"].queryset = Product.objects.filter(
            company=self.company, is_active=True).order_by("name")
        self.fields["product"].label_from_instance = lambda p: f"{p.name} ({p.sku})"
        self.fields["branches"].queryset = _branches(self.company)
        for name in ("section", "category", "product"):
            self.fields[name].empty_label = "Any"

    def clean_days(self):
        days = sorted(set(d for d in self.cleaned_data.get("days") or [] if 0 <= d <= 6))
        if not days:
            raise forms.ValidationError("Tick at least one day.")
        return ",".join(str(d) for d in days)

    def clean_discount_percent(self):
        pct = self.cleaned_data.get("discount_percent")
        if pct is not None and not Decimal("0") < pct < Decimal("100"):
            raise forms.ValidationError("The discount has to be more than 0% and less than 100%.")
        return pct

    def clean(self):
        data = super().clean()
        start, end = data.get("start_time"), data.get("end_time")
        if start and end and start == end:
            self.add_error("end_time", "The end has to differ from the start. A window that "
                                       "ends after midnight (22:00 to 01:00) is fine.")
        since, until = data.get("valid_from"), data.get("valid_to")
        if since and until and until < since:
            self.add_error("valid_to", "The last day comes before the first.")
        scopes = [n for n in ("section", "category", "product") if data.get(n)]
        if len(scopes) > 1:
            raise forms.ValidationError("Choose one of menu section, category or product (or none "
                                        "for everything on the menu).")
        return data


class StaffForm(CompanyForm):
    class Meta:
        model = POSStaff
        fields = ["name", "code", "role", "branch", "user", "is_active"]
        labels = {"user": "ERP login", "is_active": "Working here"}

    def limit(self):
        self.fields["branch"].queryset = _branches(self.company)
        self.fields["branch"].empty_label = "Any branch"
        self.fields["user"].queryset = services.company_people(self.company)
        self.fields["user"].label_from_instance = (
            lambda u: u.get_full_name() or u.username)
        self.fields["user"].empty_label = "No ERP login"

    def clean_code(self):
        return self.unique_code(POSStaff, "member of staff")


class PinForm(forms.Form):
    pin = forms.CharField(label="New PIN", max_length=8, strip=True,
                          widget=forms.PasswordInput(attrs={"inputmode": "numeric",
                                                            "autocomplete": "new-password"}))
    confirm = forms.CharField(label="Type it again", max_length=8, strip=True,
                              widget=forms.PasswordInput(attrs={"inputmode": "numeric",
                                                                "autocomplete": "new-password"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _style(self)

    def clean(self):
        data = super().clean()
        if data.get("pin") and data.get("confirm") and data["pin"] != data["confirm"]:
            raise forms.ValidationError("The two PINs do not match.")
        return data
