"""Point of Sale models."""
from decimal import Decimal
from django.db import models
from django.db.models import Q
from apps.core.models import BaseModel, NotesMixin


class POSSession(BaseModel):
    STATUS_CHOICES = [("OPEN", "Open"), ("CLOSING", "Closing"), ("CLOSED", "Closed")]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE)
    branch = models.ForeignKey("organizations.Branch", on_delete=models.CASCADE)
    cashier = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="pos_sessions")
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="OPEN")
    opening_cash = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    closing_cash = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    expected_cash = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    total_sales = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    total_refunds = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    # Who actually shut the drawer. Usually the cashier; when an administrator
    # closes a till on someone's behalf the cash-up has to be able to say so,
    # because the count on it is then theirs and not the cashier's.
    closed_by = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True,
                                  blank=True, related_name="pos_sessions_closed")
    # The entry that wrote a short or over drawer off to cash over/short. Held on
    # the session so a second close or a late count can see the variance is
    # already in the book and never books it twice.
    variance_entry = models.ForeignKey("accounting.JournalEntry", on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name="pos_variances")

    def __str__(self):
        return f"POS Session — {self.cashier} ({self.opened_at.date()})"

    @property
    def variance(self):
        """What the counted drawer is away from the tickets. None until counted."""
        if self.closing_cash is None:
            return None
        return (self.closing_cash or Decimal("0")) - (self.expected_cash or Decimal("0"))


class POSSale(BaseModel, NotesMixin):
    PAYMENT_METHODS = [
        ("CASH", "Cash"),
        ("CARD", "Card"),
        ("MPESA", "M-Pesa"),
        ("BANK", "Bank Transfer"),
        ("SPLIT", "Split Payment"),
    ]
    STATUS_CHOICES = [("COMPLETED", "Completed"), ("REFUNDED", "Refunded"), ("PARTIAL_REFUND", "Partial Refund")]

    session = models.ForeignKey(POSSession, on_delete=models.CASCADE, related_name="sales")
    reference = models.CharField(max_length=50, db_index=True)
    customer = models.ForeignKey("crm.Customer", on_delete=models.SET_NULL, null=True, blank=True)
    # The shelf the goods came off, recorded on the ticket: a refund has to put
    # them back where they were taken from, and a cash-up has to be able to say
    # which warehouse the day's trading emptied.
    warehouse = models.ForeignKey("organizations.Warehouse", on_delete=models.SET_NULL,
                                  null=True, blank=True, related_name="pos_sales")
    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHODS, default="CASH")
    amount_tendered = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    change_given = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="COMPLETED")
    invoice = models.ForeignKey("sales.Invoice", on_delete=models.SET_NULL, null=True, blank=True)
    journal_entry = models.ForeignKey("accounting.JournalEntry", on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name="pos_sales")
    # A refund is its own ticket rather than an edit of the sale: the money that
    # went over the counter in the morning and came back in the afternoon are two
    # events, and the till has to be able to prove both.
    refund_of = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True,
                                  related_name="refunds")
    refunded_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    # The one-time token the terminal or refund form was rendered with. A
    # browser that submits the same form twice (a double click, a reload after a
    # slow response) sends the same token, and the till hands back the ticket
    # the first submission made instead of taking the money a second time.
    submission_token = models.CharField(max_length=64, blank=True, default="", db_index=True)
    # Who served the customer, picked on the till's top bar. Display only: the
    # drawer and the cash-up stay the cashier's, but a sales report can credit
    # the person on the floor who made the sale.
    salesperson = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name="pos_sales_served")
    # The customer's own reference (an LPO number, a table, a phone number),
    # typed on the top bar. Not the ticket number -- that is `reference`.
    customer_reference = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "submission_token"],
                condition=~Q(submission_token=""),
                name="pos_one_ticket_per_submission",
            ),
        ]

    def __str__(self):
        return self.reference

    @property
    def outstanding_refund(self):
        """What is still returnable on this ticket."""
        return (self.total_amount or Decimal("0")) - (self.refunded_amount or Decimal("0"))


class POSSaleLine(models.Model):
    sale = models.ForeignKey(POSSale, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT)
    tax_rate = models.ForeignKey("accounting.TaxRate", on_delete=models.SET_NULL,
                                 null=True, blank=True, related_name="pos_sale_lines")
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    unit_price = models.DecimalField(max_digits=15, decimal_places=4)
    # The line discount a supervisor gave, as a percentage of the shelf price,
    # and what it took off the line before tax (discount_amount).
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    line_total = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"

    @property
    def net_total(self):
        """The line before tax — what the revenue account is credited with."""
        return (self.line_total or Decimal("0")) - (self.tax_amount or Decimal("0"))


class POSCashMovement(BaseModel):
    """Money put into or taken out of a drawer that is not a sale or a refund.

    A payout (OUT) is notes handed over the counter for something the shop
    paid for -- a delivery boy's fare, milk for the office -- or a cash drop
    to the safe. A pay-in (IN) is a float top-up mid-shift. Either way the
    drawer's expected cash moves with it, and the ledger is told at once:

        payout   DR the counter account (an expense, or the bank/safe)   CR the till (1100)
        pay-in   DR the till (1100)   CR the counter account (float equity, the bank/safe)
    """
    DIRECTIONS = [("OUT", "Payout"), ("IN", "Pay-in")]

    session = models.ForeignKey(POSSession, on_delete=models.CASCADE, related_name="cash_movements")
    reference = models.CharField(max_length=50, db_index=True)
    direction = models.CharField(max_length=3, choices=DIRECTIONS)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=255)
    account = models.ForeignKey("accounting.Account", on_delete=models.PROTECT,
                                related_name="pos_cash_movements")
    journal_entry = models.ForeignKey("accounting.JournalEntry", on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name="pos_cash_movements")
    performed_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT,
                                     related_name="pos_cash_movements")
    # One-time form token, as on a ticket: the same payout form submitted
    # twice takes the notes out of the drawer once.
    submission_token = models.CharField(max_length=64, blank=True, default="", db_index=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "submission_token"],
                condition=~Q(submission_token=""),
                name="pos_one_cash_movement_per_submission",
            ),
        ]

    def __str__(self):
        return f"{self.reference} {self.get_direction_display()} {self.amount}"


# ───────────────────────────── Restaurant layer ─────────────────────────────
# Waiter stations, menus, happy hours and open bills sit on top of the till:
# a bill is closed by ringing it through an open till session (services.
# checkout_lines), so stock, VAT and the ledger work exactly as for a counter
# sale. Staff act at a station with a PIN (POSStaff), not an ERP login: a
# station is a device someone signed in to, and waiters identify themselves on
# it to take orders.

class POSSettings(BaseModel):
    """Head-office configuration: one row per company, read by every branch."""
    company = models.OneToOneField("organizations.Company", on_delete=models.CASCADE,
                                   related_name="pos_settings")
    receipt_header = models.TextField(blank=True, default="",
                                      help_text="Printed at the top of bills and receipts.")
    receipt_footer = models.TextField(blank=True, default="Thank you, karibu tena!",
                                      help_text="Printed at the bottom of bills and receipts.")
    default_menu = models.ForeignKey("pos.POSMenu", on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name="+",
                                     help_text="Used by any station without a menu of its own.")
    waiter_can_take_payment = models.BooleanField(
        default=False, help_text="Off: waiters print the bill and a cashier takes the money.")
    require_table = models.BooleanField(default=False,
                                        help_text="Every bill at a waiter station needs a table.")
    day_end_requires_closed_bills = models.BooleanField(
        default=True, help_text="Refuse the day end while any bill is still open.")
    pin_idle_minutes = models.PositiveSmallIntegerField(
        default=2, help_text="A waiter's PIN session ends after this many idle minutes.")

    class Meta:
        verbose_name = "POS settings"

    def __str__(self):
        return f"POS settings for {self.company}"


class POSMenu(BaseModel):
    """What a station sells, in the order it shows it. Items are products, so
    stock, tax and cost of sales come from the product."""
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="pos_menus")
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=255, blank=True, default="")
    branches = models.ManyToManyField("organizations.Branch", blank=True, related_name="pos_menus",
                                      help_text="Leave empty to offer it at every branch.")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class POSMenuSection(models.Model):
    menu = models.ForeignKey(POSMenu, on_delete=models.CASCADE, related_name="sections")
    name = models.CharField(max_length=80)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return f"{self.menu} > {self.name}"


class POSMenuItem(models.Model):
    section = models.ForeignKey(POSMenuSection, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT,
                                related_name="pos_menu_items")
    display_name = models.CharField(max_length=120, blank=True, default="",
                                    help_text="Leave blank to show the product name.")
    price = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True,
                                help_text="Leave blank to sell at the product's selling price.")
    is_available = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return self.label

    @property
    def label(self):
        return self.display_name or self.product.name

    @property
    def unit_price(self):
        return self.price if self.price is not None else self.product.selling_price


class POSStation(BaseModel):
    KINDS = [("WAITER", "Waiter station"), ("CASHIER", "Cashier point"), ("BAR", "Bar")]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="pos_stations")
    branch = models.ForeignKey("organizations.Branch", on_delete=models.CASCADE,
                               related_name="pos_stations")
    name = models.CharField(max_length=80)
    code = models.CharField(max_length=20)
    kind = models.CharField(max_length=10, choices=KINDS, default="WAITER")
    warehouse = models.ForeignKey("organizations.Warehouse", on_delete=models.SET_NULL, null=True,
                                  blank=True, help_text="Stock is issued from here when a bill closes.")
    menu = models.ForeignKey(POSMenu, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="stations",
                             help_text="Leave blank to use head office's default menu.")
    printer_name = models.CharField(max_length=80, blank=True, default="",
                                    help_text="Receipt printer at this station (for reference).")

    class Meta:
        ordering = ["branch__name", "name"]
        constraints = [models.UniqueConstraint(fields=["company", "code"],
                                               name="pos_station_code_unique")]

    def __str__(self):
        return f"{self.name} ({self.branch})"


class POSTable(BaseModel):
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="pos_tables")
    branch = models.ForeignKey("organizations.Branch", on_delete=models.CASCADE,
                               related_name="pos_tables")
    station = models.ForeignKey(POSStation, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="tables",
                                help_text="Leave blank to serve it from any station at the branch.")
    name = models.CharField(max_length=40)
    area = models.CharField(max_length=60, blank=True, default="",
                            help_text="e.g. Terrace, Bar, Main hall.")
    seats = models.PositiveSmallIntegerField(default=4)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["area", "sort_order", "name"]

    def __str__(self):
        return self.name


class POSPriceRule(BaseModel):
    """A timed discount, such as a happy hour: so much off, on these days,
    between these times, on these items. The server applies it when an item
    is ordered, so nobody types the discount and nobody can stretch it."""
    DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="pos_price_rules")
    name = models.CharField(max_length=80)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2)
    days = models.CharField(max_length=20, default="0,1,2,3,4,5,6",
                            help_text="Weekdays it runs, Monday = 0.")
    start_time = models.TimeField()
    end_time = models.TimeField(help_text="May be after midnight (e.g. 22:00 to 01:00).")
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)
    # PROTECT: deleting a section must not silently delete its happy hours
    # (and SET_NULL would widen the rule to everything).
    section = models.ForeignKey(POSMenuSection, on_delete=models.PROTECT, null=True, blank=True,
                                related_name="price_rules")
    category = models.ForeignKey("inventory.ProductCategory", on_delete=models.CASCADE, null=True,
                                 blank=True, related_name="pos_price_rules",
                                 help_text="Covers the category and everything under it.")
    product = models.ForeignKey("inventory.Product", on_delete=models.CASCADE, null=True,
                                blank=True, related_name="pos_price_rules")
    branches = models.ManyToManyField("organizations.Branch", blank=True,
                                      related_name="pos_price_rules",
                                      help_text="Leave empty to run it at every branch.")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def day_list(self):
        return [int(d) for d in (self.days or "").split(",") if d.strip().isdigit()]

    @property
    def days_label(self):
        days = self.day_list
        if sorted(days) == list(range(7)):
            return "Every day"
        return ", ".join(self.DAY_NAMES[d] for d in sorted(days) if 0 <= d < 7)


class POSStaff(BaseModel):
    """Someone who works the floor: identified at a station by a PIN."""
    ROLES = [("WAITER", "Waiter"), ("CASHIER", "Cashier"), ("MANAGER", "Manager")]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="pos_staff")
    branch = models.ForeignKey("organizations.Branch", on_delete=models.SET_NULL, null=True,
                               blank=True, help_text="Leave blank to work at any branch.")
    name = models.CharField(max_length=80)
    code = models.CharField(max_length=20, help_text="Staff number shown on bills.")
    role = models.CharField(max_length=10, choices=ROLES, default="WAITER")
    pin_hash = models.CharField(max_length=128, blank=True, default="")
    user = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="pos_staff", help_text="Their ERP login, if they have one.")
    failed_pins = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["company", "code"],
                                               name="pos_staff_code_unique")]

    def __str__(self):
        return f"{self.name} ({self.code})"

    @property
    def is_manager(self):
        return self.role == "MANAGER"


class POSOrder(BaseModel, NotesMixin):
    """An open bill: what a table (or a walk-in) has ordered so far."""
    STATUS_CHOICES = [("OPEN", "Open"), ("BILLED", "Bill printed"), ("CLOSED", "Closed"),
                      ("VOID", "Voided")]
    LIVE = ("OPEN", "BILLED")
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="pos_orders")
    branch = models.ForeignKey("organizations.Branch", on_delete=models.CASCADE)
    station = models.ForeignKey(POSStation, on_delete=models.PROTECT, related_name="orders")
    table = models.ForeignKey(POSTable, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="orders")
    waiter = models.ForeignKey(POSStaff, on_delete=models.PROTECT, related_name="orders")
    reference = models.CharField(max_length=30, db_index=True)
    covers = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="OPEN")
    opened_at = models.DateTimeField()
    billed_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True,
                                  blank=True, related_name="+")
    sale = models.ForeignKey(POSSale, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="orders")
    voided_by = models.ForeignKey(POSStaff, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="+")
    void_reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-opened_at"]

    def __str__(self):
        return self.reference


class POSOrderLine(models.Model):
    STATUS_CHOICES = [("ACTIVE", "Active"), ("VOID", "Voided")]
    order = models.ForeignKey(POSOrder, on_delete=models.CASCADE, related_name="lines")
    menu_item = models.ForeignKey(POSMenuItem, on_delete=models.SET_NULL, null=True, blank=True)
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT)
    name = models.CharField(max_length=120)
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    unit_price = models.DecimalField(max_digits=15, decimal_places=4)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    price_rule = models.ForeignKey(POSPriceRule, on_delete=models.SET_NULL, null=True, blank=True)
    note = models.CharField(max_length=120, blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="ACTIVE")
    added_by = models.ForeignKey(POSStaff, on_delete=models.PROTECT, related_name="+")
    added_at = models.DateTimeField(auto_now_add=True)
    voided_by = models.ForeignKey(POSStaff, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="+")
    void_requested_by = models.ForeignKey(POSStaff, on_delete=models.SET_NULL, null=True,
                                          blank=True, related_name="+")
    void_reason = models.CharField(max_length=255, blank=True, default="")
    voided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["added_at", "id"]

    def __str__(self):
        return f"{self.quantity} x {self.name}"

    @property
    def gross(self):
        return (self.quantity * self.unit_price).quantize(Decimal("0.01"))

    @property
    def discount(self):
        return (self.gross * self.discount_percent / Decimal("100")).quantize(Decimal("0.01"))

    @property
    def net(self):
        return self.gross - self.discount


class POSDayEnd(BaseModel):
    """The day closed at a branch: who closed it and the figures as they stood."""
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="pos_day_ends")
    branch = models.ForeignKey("organizations.Branch", on_delete=models.CASCADE)
    business_date = models.DateField()
    closed_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    closed_at = models.DateTimeField()
    summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-business_date"]
        constraints = [models.UniqueConstraint(fields=["company", "branch", "business_date"],
                                               name="pos_day_end_unique")]

    def __str__(self):
        return f"Day end {self.branch} {self.business_date}"


# ─────────────────────────── pictures and till hardware ──────────────────────────

class ProductPicture(models.Model):
    """The picture a till and a menu show for a product, kept in the database
    as a small compressed thumbnail: it survives a serverless host's wiped
    disk (Vercel), and the offline service worker can keep it."""
    product = models.OneToOneField("inventory.Product", on_delete=models.CASCADE,
                                   related_name="pos_picture")
    data = models.BinaryField()
    content_type = models.CharField(max_length=40, default="image/webp")
    width = models.PositiveSmallIntegerField(default=0)
    height = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Picture of {self.product}"


class POSPrinter(BaseModel):
    """A receipt printer and the cash drawer on it (most drawers open from the
    printer's drawer port), reached over the shop's network."""
    CONNECTIONS = [
        ("BRIDGE", "ESC/POS printer on the LAN, via the Mofisi bridge on the till PC"),
        ("EPSON", "Epson ePOS printer (direct from the browser)"),
        ("STAR", "Star WebPRNT printer (direct from the browser)"),
        ("SERVER", "ESC/POS printer the server itself can reach (server on the shop's LAN)"),
    ]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="pos_printers")
    branch = models.ForeignKey("organizations.Branch", on_delete=models.CASCADE, null=True,
                               blank=True, related_name="pos_printers")
    name = models.CharField(max_length=80)
    connection = models.CharField(max_length=10, choices=CONNECTIONS, default="BRIDGE")
    host = models.CharField("LAN IP address", max_length=100,
                            help_text="The printer's IP on the shop network, e.g. 192.168.1.50.")
    port = models.PositiveIntegerField(default=9100,
                                       help_text="9100 for raw ESC/POS; 80 or 8008 for Epson ePOS; 80 for Star.")
    bridge_url = models.CharField(max_length=120, default="http://127.0.0.1:9180",
                                  help_text="Where the Mofisi bridge listens on the till PC.")
    drawer_pin = models.PositiveSmallIntegerField(
        default=2, choices=[(2, "Pin 2 (most drawers)"), (5, "Pin 5")])
    paper_chars = models.PositiveSmallIntegerField(
        "Characters per line", default=48, choices=[(32, "58 mm paper (32)"), (42, "80 mm, small font (42)"),
                                                    (48, "80 mm paper (48)")])
    open_drawer_on_cash = models.BooleanField(default=True,
                                              help_text="Open the drawer when a cash sale or refund is rung.")
    print_receipts = models.BooleanField(default=True,
                                         help_text="Print the receipt on this printer when a sale is rung.")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.host})"


class POSDrawerEvent(models.Model):
    """Every time a cash drawer was opened, and why: a drawer opened with no
    sale is the classic way cash walks out, so each one is on the day end."""
    REASONS = [("SALE", "Cash sale"), ("REFUND", "Cash refund"), ("PAYOUT", "Payout / pay-in"),
               ("NO_SALE", "No sale"), ("TEST", "Test")]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="pos_drawer_events")
    branch = models.ForeignKey("organizations.Branch", on_delete=models.SET_NULL, null=True, blank=True)
    printer = models.ForeignKey(POSPrinter, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="events")
    session = models.ForeignKey(POSSession, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="drawer_events")
    sale = models.ForeignKey(POSSale, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="drawer_events")
    user = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=10, choices=REASONS)
    note = models.CharField(max_length=255, blank=True, default="")
    ok = models.BooleanField(default=True)
    error = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_reason_display()} by {self.user} at {self.created_at:%H:%M}"
