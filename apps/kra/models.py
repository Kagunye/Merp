"""KRA eTIMS integration (OSCU / VSCU field model, KRA spec v2.0).

One canonical KRA payload is built for every document; a backend (direct
OSCU, a local VSCU, or an integrator such as Pergamon) only carries it. Every
call goes through an outbox (EtimsSubmission) so nothing is lost when KRA or
the internet is down, and documents leave strictly in order per branch device:
KRA refuses invoice n+1 before invoice n.
"""
from django.db import models

from apps.core.models import BaseModel


class EtimsSettings(BaseModel):
    """One row per company: how this company talks to KRA."""
    BACKENDS = [
        ("OSCU", "KRA eTIMS OSCU (direct, online)"),
        ("VSCU", "KRA VSCU (local JAR on your server)"),
        ("PERGAMON", "Pergamon (Easy Tax, KRA-approved integrator)"),
        ("SIMULATOR", "Built-in KRA simulator (testing only)"),
    ]
    ENVIRONMENTS = [("SANDBOX", "Sandbox (testing)"), ("PRODUCTION", "Production (live)")]
    company = models.OneToOneField("organizations.Company", on_delete=models.CASCADE,
                                   related_name="etims_settings")
    enabled = models.BooleanField(default=False,
                                  help_text="Off: nothing is sent to KRA and nothing is queued.")
    backend = models.CharField(max_length=10, choices=BACKENDS, default="SIMULATOR")
    environment = models.CharField(max_length=10, choices=ENVIRONMENTS, default="SANDBOX")
    tin = models.CharField("KRA PIN", max_length=11, blank=True, default="",
                           help_text="Leave blank to use the company's Tax ID / KRA PIN.")
    base_url = models.URLField(blank=True, default="",
                               help_text="Leave blank for KRA's default for the backend and "
                                         "environment. VSCU: e.g. http://localhost:8088")
    # GavaConnect OAuth, if KRA asks for it in front of OSCU (optional).
    oauth_consumer_key = models.CharField(max_length=200, blank=True, default="")
    oauth_consumer_secret = models.CharField(max_length=200, blank=True, default="")
    # Pergamon: the fiscal endpoint and key they issue (their API is not public).
    pergamon_endpoint = models.URLField(blank=True, default="")
    pergamon_api_key = models.CharField(max_length=200, blank=True, default="")
    auto_send = models.BooleanField(default=True,
                                    help_text="Send to KRA straight away when a document posts; "
                                              "the queue retries anything that fails.")
    sales_type = models.CharField(max_length=1, default="N",
                                  help_text="N normal; T training (sandbox practice receipts).")
    timeout_seconds = models.PositiveSmallIntegerField(default=20)

    class Meta:
        verbose_name = "eTIMS settings"

    def __str__(self):
        return f"eTIMS for {self.company}"

    @property
    def kra_pin(self):
        return (self.tin or self.company.tax_id or "").strip().upper()


class EtimsDevice(BaseModel):
    """A branch's KRA control unit: tin + bhfId + device serial, and what
    KRA hands back at initialisation (cmcKey, SCU id, MRC number)."""
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="etims_devices")
    branch = models.ForeignKey("organizations.Branch", on_delete=models.CASCADE,
                               related_name="etims_devices")
    bhf_id = models.CharField("KRA branch id (bhfId)", max_length=2, default="00",
                              help_text="00 is head office; 01, 02 … are branches.")
    device_serial = models.CharField("Device serial (dvcSrlNo)", max_length=100)
    cmc_key = models.CharField("Communication key (cmcKey)", max_length=255, blank=True, default="")
    sdc_id = models.CharField("SCU / CU ID (sdcId)", max_length=40, blank=True, default="")
    mrc_no = models.CharField("MRC number", max_length=40, blank=True, default="")
    dvc_id = models.CharField("Device id", max_length=40, blank=True, default="")
    taxpayer_name = models.CharField(max_length=200, blank=True, default="")
    branch_name = models.CharField(max_length=200, blank=True, default="")
    initialized_at = models.DateTimeField(null=True, blank=True)
    # Sequences KRA checks: invoices and stock movements run 1, 2, 3 … per device.
    next_invoice_no = models.PositiveIntegerField(default=1)
    next_stock_no = models.PositiveIntegerField(default=1)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["bhf_id"]
        constraints = [models.UniqueConstraint(fields=["company", "bhf_id"],
                                               name="etims_device_bhf_unique"),
                       models.UniqueConstraint(fields=["company", "branch"],
                                               name="etims_device_branch_unique")]

    def __str__(self):
        return f"{self.branch} (bhfId {self.bhf_id})"

    @property
    def ready(self):
        return bool(self.initialized_at and self.cmc_key)


class EtimsCode(models.Model):
    """KRA's standard codes (selectCodeList): tax types, units, payment types …"""
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="etims_codes")
    cd_cls = models.CharField(max_length=5)
    cd_cls_name = models.CharField(max_length=100, blank=True, default="")
    cd = models.CharField(max_length=20)
    name = models.CharField(max_length=200)
    user_dfn_cd1 = models.CharField(max_length=50, blank=True, default="")
    sort_order = models.IntegerField(default=0)
    use_yn = models.BooleanField(default=True)

    class Meta:
        ordering = ["cd_cls", "sort_order", "cd"]
        constraints = [models.UniqueConstraint(fields=["company", "cd_cls", "cd"],
                                               name="etims_code_unique")]

    def __str__(self):
        return f"{self.cd_cls}/{self.cd} {self.name}"


class EtimsItemClass(models.Model):
    """KRA item classification (selectItemClsList)."""
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="etims_item_classes")
    code = models.CharField(max_length=10)
    name = models.CharField(max_length=255)
    level = models.PositiveSmallIntegerField(default=1)
    tax_type = models.CharField(max_length=1, blank=True, default="")
    use_yn = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]
        constraints = [models.UniqueConstraint(fields=["company", "code"],
                                               name="etims_itemclass_unique")]

    def __str__(self):
        return f"{self.code} {self.name}"


class EtimsTaxMap(models.Model):
    """Which KRA tax type (A exempt, B 16%, C zero-rated, D non-VAT, E 8%) a
    tax rate of this company is. 0% is ambiguous (exempt or zero-rated), so
    it is the company's choice."""
    TAX_TYPES = [("A", "A — Exempt"), ("B", "B — Standard 16%"), ("C", "C — Zero-rated 0%"),
                 ("D", "D — Non-VAT"), ("E", "E — Reduced 8%")]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="etims_tax_maps")
    tax_rate = models.OneToOneField("accounting.TaxRate", on_delete=models.CASCADE,
                                    related_name="etims_map")
    tax_type = models.CharField(max_length=1, choices=TAX_TYPES)

    def __str__(self):
        return f"{self.tax_rate} → {self.tax_type}"


class EtimsItem(BaseModel):
    """A product as KRA knows it (saveItem): its KRA item code and classes."""
    ITEM_TYPES = [("1", "1 — Raw material"), ("2", "2 — Finished product"), ("3", "3 — Service")]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="etims_items")
    product = models.OneToOneField("inventory.Product", on_delete=models.CASCADE,
                                   related_name="etims_item")
    item_cd = models.CharField("KRA item code", max_length=20)
    item_cls_cd = models.CharField("Item class", max_length=10, default="5059690800",
                                   help_text="KRA item classification code (8–10 digits).")
    item_ty_cd = models.CharField(max_length=1, choices=ITEM_TYPES, default="2")
    origin_country = models.CharField(max_length=2, default="KE")
    pkg_unit_cd = models.CharField("Packaging unit", max_length=5, default="NT")
    qty_unit_cd = models.CharField("Quantity unit", max_length=5, default="U")
    registered_at = models.DateTimeField(null=True, blank=True)
    registered_hash = models.CharField(max_length=64, blank=True, default="",
                                       help_text="What was last sent, so a change re-registers it.")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company", "item_cd"],
                                               name="etims_item_code_unique")]

    def __str__(self):
        return self.item_cd


class EtimsSubmission(BaseModel):
    """The outbox: one call to KRA, kept until KRA has accepted it."""
    KINDS = [
        ("INIT", "Device initialisation"), ("CODES", "Code list"), ("ITEM_CLASSES", "Item classes"),
        ("NOTICES", "Notices"), ("BRANCHES", "Branches"), ("CUSTOMER_LOOKUP", "Customer PIN lookup"),
        ("CUSTOMER", "Branch customer"), ("USER", "Branch user"), ("ITEM", "Item registration"),
        ("SALE", "Sale"), ("CREDIT_NOTE", "Credit note"), ("PURCHASE", "Purchase"),
        ("PURCHASE_LIST", "Supplier invoices"), ("IMPORT_LIST", "Imports"),
        ("IMPORT_UPDATE", "Import decision"), ("STOCK_IO", "Stock movement"),
        ("STOCK_MASTER", "Stock on hand"), ("STOCK_MOVES", "Transfers in"),
    ]
    STATUSES = [("PENDING", "Waiting to send"), ("SENT", "Accepted by KRA"),
                ("FAILED", "Refused — needs attention"), ("HELD", "Waiting for an earlier one"),
                ("CANCELLED", "Cancelled")]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="etims_submissions")
    device = models.ForeignKey(EtimsDevice, on_delete=models.CASCADE, null=True, blank=True,
                               related_name="submissions")
    kind = models.CharField(max_length=20, choices=KINDS)
    status = models.CharField(max_length=10, choices=STATUSES, default="PENDING", db_index=True)
    # What it is about, for people: "INV-00012", "POS-00031", a product's SKU.
    source_type = models.CharField(max_length=40, blank=True, default="")
    source_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    source_label = models.CharField(max_length=100, blank=True, default="")
    sequence = models.PositiveIntegerField(null=True, blank=True,
                                           help_text="invcNo or sarNo this call carries.")
    payload = models.JSONField(default=dict)
    response = models.JSONField(default=dict, blank=True)
    result_code = models.CharField(max_length=10, blank=True, default="")
    result_message = models.CharField(max_length=500, blank=True, default="")
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    next_try_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    depends_on = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="dependants")

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["company", "status", "created_at"])]

    def __str__(self):
        return f"{self.get_kind_display()} {self.source_label or ''}".strip()


class EtimsReceipt(BaseModel):
    """What KRA signed for a sale or credit note: printed on the receipt."""
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="etims_receipts")
    device = models.ForeignKey(EtimsDevice, on_delete=models.PROTECT, related_name="receipts")
    submission = models.OneToOneField(EtimsSubmission, on_delete=models.CASCADE,
                                      related_name="receipt")
    source_type = models.CharField(max_length=40)
    source_id = models.CharField(max_length=64, db_index=True)
    receipt_type = models.CharField(max_length=1, default="S", help_text="S sale, R credit note.")
    invoice_no = models.PositiveIntegerField()
    original_invoice_no = models.PositiveIntegerField(default=0)
    receipt_no = models.CharField(max_length=40, blank=True, default="")
    total_receipt_no = models.CharField(max_length=40, blank=True, default="")
    internal_data = models.CharField(max_length=80, blank=True, default="")
    signature = models.CharField(max_length=80, blank=True, default="")
    sdc_id = models.CharField(max_length=40, blank=True, default="")
    mrc_no = models.CharField(max_length=40, blank=True, default="")
    signed_at = models.DateTimeField(null=True, blank=True)
    qr_url = models.URLField(max_length=500, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["device", "invoice_no"],
                                               name="etims_receipt_invno_unique")]

    def __str__(self):
        return f"{self.sdc_id}/{self.receipt_no}"

    @property
    def cu_invoice_no(self):
        return f"{self.sdc_id}/{self.receipt_no}" if self.sdc_id else str(self.receipt_no)

    @staticmethod
    def dashed(value):
        value = value or ""
        return "-".join(value[i:i + 4] for i in range(0, len(value), 4))


class EtimsSyncState(models.Model):
    """lastReqDt per data type, so each pull only fetches what changed."""
    device = models.ForeignKey(EtimsDevice, on_delete=models.CASCADE, related_name="sync_states")
    kind = models.CharField(max_length=20)
    last_req_dt = models.CharField(max_length=14, default="20200101000000")
    synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["device", "kind"],
                                               name="etims_sync_unique")]


class EtimsNotice(models.Model):
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="etims_notices")
    notice_no = models.CharField(max_length=40)
    title = models.CharField(max_length=255)
    content = models.TextField(blank=True, default="")
    detail_url = models.URLField(max_length=500, blank=True, default="")
    registered_at = models.CharField(max_length=20, blank=True, default="")

    class Meta:
        ordering = ["-registered_at"]
        constraints = [models.UniqueConstraint(fields=["company", "notice_no"],
                                               name="etims_notice_unique")]


class EtimsPurchase(BaseModel):
    """A supplier's invoice to us as KRA holds it (selectTrnsPurchaseSalesList),
    waiting for us to confirm (insertTrnsPurchase) or reject it."""
    STATUSES = [("FETCHED", "To review"), ("CONFIRMED", "Confirmed"), ("REJECTED", "Rejected")]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="etims_purchases")
    device = models.ForeignKey(EtimsDevice, on_delete=models.CASCADE, related_name="purchases")
    supplier_tin = models.CharField(max_length=20)
    supplier_name = models.CharField(max_length=200, blank=True, default="")
    supplier_bhf_id = models.CharField(max_length=2, blank=True, default="")
    supplier_invoice_no = models.CharField(max_length=40)
    sale_date = models.CharField(max_length=14, blank=True, default="")
    total_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    total_tax = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    data = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=STATUSES, default="FETCHED")
    bill = models.ForeignKey("procurement.Bill", on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="etims_purchases")

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(
            fields=["device", "supplier_tin", "supplier_bhf_id", "supplier_invoice_no"],
            name="etims_purchase_unique")]


class EtimsImport(BaseModel):
    """An item declared at customs for us (selectImportItemList), to approve
    (and map to one of our products) or reject (updateImportItem)."""
    STATUSES = [("2", "Waiting"), ("3", "Approved"), ("4", "Cancelled")]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="etims_imports")
    device = models.ForeignKey(EtimsDevice, on_delete=models.CASCADE, related_name="imports")
    task_cd = models.CharField(max_length=50)
    declaration_date = models.CharField(max_length=14)
    item_seq = models.PositiveIntegerField()
    declaration_no = models.CharField(max_length=50, blank=True, default="")
    hs_code = models.CharField(max_length=20, blank=True, default="")
    item_name = models.CharField(max_length=255, blank=True, default="")
    origin_country = models.CharField(max_length=2, blank=True, default="")
    quantity = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    qty_unit_cd = models.CharField(max_length=5, blank=True, default="")
    data = models.JSONField(default=dict)
    status = models.CharField(max_length=1, choices=STATUSES, default="2")
    product = models.ForeignKey("inventory.Product", on_delete=models.SET_NULL, null=True,
                                blank=True, related_name="+")

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["device", "task_cd", "declaration_date",
                                                       "item_seq"], name="etims_import_unique")]
