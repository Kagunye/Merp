"""Online payment gateways: M-Pesa (Safaricom Daraja), Pesapal and DPO Pay.

A company switches on the gateways it has a merchant account with. Each
payment asked for is a PaymentRequest: an M-Pesa prompt sent to a phone, or a
Pesapal / DPO checkout link. It becomes PAID only once the gateway itself
confirms it (a callback checked back with the gateway, never the callback's
word alone), and only then is the receipt booked against the invoice or the
till ticket is rung.
"""
import secrets
import uuid

from django.conf import settings
from django.db import models


def _key():
    return secrets.token_urlsafe(24)


class PaymentGateway(models.Model):
    PROVIDERS = [("MPESA", "M-Pesa (Safaricom Daraja)"), ("PESAPAL", "Pesapal"), ("DPO", "DPO Pay")]
    ENVIRONMENTS = [
        ("SIMULATOR", "Simulator (no money moves; for training and tests)"),
        ("SANDBOX", "Sandbox (the provider's test system)"),
        ("LIVE", "Live"),
    ]
    SHORTCODE_TYPES = [("PAYBILL", "Paybill"), ("TILL", "Buy Goods till")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="payment_gateways")
    provider = models.CharField(max_length=10, choices=PROVIDERS)
    is_active = models.BooleanField(default=False, verbose_name="Switched on")
    environment = models.CharField(max_length=10, choices=ENVIRONMENTS, default="SIMULATOR")

    # Daraja and Pesapal both issue a consumer key and secret.
    consumer_key = models.CharField(max_length=200, blank=True, default="")
    consumer_secret = models.CharField(max_length=200, blank=True, default="")
    # M-Pesa
    shortcode = models.CharField("Business shortcode", max_length=20, blank=True, default="",
                                 help_text="Paybill number, or for a till the store (head office) number.")
    shortcode_type = models.CharField(max_length=10, choices=SHORTCODE_TYPES, default="PAYBILL")
    till_number = models.CharField(max_length=20, blank=True, default="",
                                   help_text="Buy Goods only: the till number customers see.")
    passkey = models.CharField("Lipa na M-Pesa passkey", max_length=200, blank=True, default="")
    # Pesapal
    ipn_id = models.CharField("IPN id", max_length=100, blank=True, default="", editable=False)
    # DPO
    company_token = models.CharField("DPO company token", max_length=100, blank=True, default="")
    service_type = models.CharField("DPO service type", max_length=20, blank=True, default="")

    settlement_account = models.ForeignKey(
        "banking.BankAccount", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
        help_text="Where money from this gateway lands; receipts are booked to it.")
    callback_base = models.URLField(
        blank=True, default="",
        help_text="The public address of this Mofisi site (https://...). Blank uses the address it is opened on.")
    webhook_key = models.CharField(max_length=64, default=_key, editable=False, unique=True)
    last_check = models.DateTimeField(null=True, blank=True)
    last_check_ok = models.BooleanField(default=False)
    last_check_note = models.CharField(max_length=255, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["company", "provider"], name="payments_one_gateway_each")]
        ordering = ["provider"]

    def __str__(self):
        return f"{self.get_provider_display()} — {self.company}"


class PaymentRequest(models.Model):
    STATUSES = [("PENDING", "Waiting for the customer"), ("PAID", "Paid"), ("FAILED", "Failed"),
                ("CANCELLED", "Cancelled"), ("EXPIRED", "Expired")]
    FOR = [("INVOICE", "Invoice"), ("POS", "Till ticket"), ("OTHER", "Other")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="payment_requests")
    gateway = models.ForeignKey(PaymentGateway, on_delete=models.PROTECT, related_name="requests")
    purpose = models.CharField(max_length=10, choices=FOR, default="OTHER")
    invoice = models.ForeignKey("sales.Invoice", on_delete=models.SET_NULL, null=True, blank=True, related_name="payment_requests")
    pos_session = models.ForeignKey("pos.POSSession", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    reference = models.CharField(max_length=40, help_text="What the customer sees: the invoice number or till ticket.")
    description = models.CharField(max_length=100, blank=True, default="")
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    currency = models.CharField(max_length=3, default="KES")
    phone = models.CharField(max_length=20, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUSES, default="PENDING", db_index=True)
    provider_ref = models.CharField(max_length=120, blank=True, default="", db_index=True,
                                    help_text="CheckoutRequestID / OrderTrackingId / TransToken.")
    checkout_url = models.URLField(max_length=500, blank=True, default="")
    receipt = models.CharField("Confirmation code", max_length=60, blank=True, default="", db_index=True)
    payer = models.CharField(max_length=120, blank=True, default="")
    note = models.CharField(max_length=255, blank=True, default="")
    callback_key = models.CharField(max_length=64, default=_key, editable=False, unique=True)
    raw = models.JSONField(default=dict, blank=True)
    customer_payment = models.ForeignKey("sales.CustomerPayment", on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.gateway.get_provider_display()} {self.amount} {self.reference} ({self.status})"


class PaybillReceipt(models.Model):
    """Money paid straight to the paybill or till (not from a prompt we sent).
    Matched to an invoice by the account number the customer typed."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="paybill_receipts")
    gateway = models.ForeignKey(PaymentGateway, on_delete=models.CASCADE, related_name="paybill_receipts")
    trans_id = models.CharField(max_length=40)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    account = models.CharField("Account number typed", max_length=60, blank=True, default="")
    msisdn = models.CharField(max_length=80, blank=True, default="")
    payer = models.CharField(max_length=120, blank=True, default="")
    paid_at = models.DateTimeField(null=True, blank=True)
    invoice = models.ForeignKey("sales.Invoice", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    customer_payment = models.ForeignKey("sales.CustomerPayment", on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name="+")
    note = models.CharField(max_length=255, blank=True, default="")
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["gateway", "trans_id"], name="payments_paybill_once")]
