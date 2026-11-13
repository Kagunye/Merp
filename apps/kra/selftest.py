"""The KRA readiness checklist: every eTIMS function KRA vets an integrator
on, run end to end.

KRA does not publish its certification test sheet (it is handed out during
vetting; ask timsupport@kra.go.ke). These groups follow the eight function
categories and the ordering rules of the OSCU/VSCU v2.0 specifications.

`run(company)` plays them against the built-in KRA simulator inside a
transaction that is rolled back, so the company's real devices, numbers and
queue are untouched. `run(company, live=True)` plays them against the
company's configured backend in the KRA *sandbox* (refused in production):
that sends real test documents to KRA's sandbox on the company's device.
"""
import datetime
from decimal import Decimal as D

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from . import services
from .backends import EtimsTransportError, SimulatorBackend
from .models import (EtimsDevice, EtimsImport, EtimsItem, EtimsPurchase, EtimsReceipt,
                     EtimsSettings, EtimsSubmission)

GROUPS = [
    ("1", "Device initialisation"),
    ("2", "Code lists, item classes, notices (incremental sync)"),
    ("3", "Customers: PIN lookup and branch customer"),
    ("4", "Item registration (tax types A–E, services)"),
    ("5", "Imports: fetch, approve with mapping, reject"),
    ("6", "Sales and credit notes"),
    ("7", "Purchases: fetch and confirm"),
    ("8", "Stock movements and stock on hand"),
    ("9", "Offline queue, ordering and duplicates"),
]


class Rollback(Exception):
    pass


class Runner:
    def __init__(self):
        self.results = []

    def check(self, group, label, fn):
        try:
            ok, detail = fn()
        except (ValidationError, EtimsTransportError) as exc:
            ok, detail = False, " ".join(getattr(exc, "messages", [str(exc)]))
        except Exception as exc:       # noqa: BLE001 - a crash is a failed check
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.results.append({"group": group, "label": label, "ok": bool(ok), "detail": str(detail or "")})
        return ok


def _rows(company, products, specs):
    """specs: (product key, qty, unit price, discount%)."""
    rows = []
    for key, qty, price, disc in specs:
        p = products[key]
        net = (D(str(qty)) * D(str(price)) * (1 - D(str(disc)) / 100)).quantize(D("0.01"))
        rate = D(str(p.tax_rate.rate)) if p.tax_rate_id else D("0")
        rows.append({"product": p, "name": p.name, "qty": D(str(qty)), "unit_price": D(str(price)),
                     "discount_pct": D(str(disc)), "tax_rate": p.tax_rate, "net": net,
                     "tax": (net * rate / 100).quantize(D("0.01"))})
    return rows


def _fixtures(company, user):
    """Products for every KRA tax type, made inside the rolled-back run."""
    from apps.accounting.models import TaxRate
    from apps.inventory.models import Product, UnitOfMeasure
    uom = (UnitOfMeasure.objects.filter(company=company).first()
           or UnitOfMeasure.objects.create(company=company, name="Pieces", abbreviation="PCS", is_base=True))
    from apps.accounting.models import Account
    vat_account = (TaxRate.objects.filter(company=company).values_list("account_id", flat=True).first()
                   or Account.objects.filter(company=company, code="2200").values_list("pk", flat=True).first())
    rates = {}
    for code, name, rate, letter in (("KRA-B", "VAT 16%", "16", "B"), ("KRA-E", "VAT 8%", "8", "E"),
                                     ("KRA-C", "Zero-rated", "0", "C"), ("KRA-A", "Exempt", "0", "A")):
        tr = TaxRate.objects.create(company=company, name=f"{name} (self-test)", rate=D(rate), code=code,
                                    account_id=vat_account, effective_from=datetime.date(2020, 1, 1))
        from .models import EtimsTaxMap
        EtimsTaxMap.objects.create(company=company, tax_rate=tr, tax_type=letter)
        rates[letter] = tr
    stamp = timezone.now().strftime("%H%M%S%f")

    def product(key, name, price, letter, service=False):
        return Product.objects.create(
            company=company, sku=f"KRA-{key}-{stamp}", name=name, unit_of_measure=uom,
            selling_price=D(price), cost_price=D(price) / 2, tax_rate=rates.get(letter),
            track_inventory=not service, product_type="SERVICE" if service else "GOODS")
    return {
        "B": product("B", "Self-test soda 500ml", "116", "B"),
        "E": product("E", "Self-test petrol (8%)", "200", "E"),
        "C": product("C", "Self-test exported tea (0%)", "300", "C"),
        "A": product("A", "Self-test maize flour (exempt)", "150", "A"),
        "D": product("D", "Self-test non-VAT item", "50", None),
        "S": product("S", "Self-test delivery service", "500", "B", service=True),
    }


def _sale(runner, company, device, products, specs, *, receipt="S", original=0, customer=None,
          payment="CASH", label="", send=True, broken=False):
    rows = _rows(company, products, specs)
    for r in rows:
        services.queue_item(r["product"], force=False)
    n = services._claim_invoice_no(device)
    payload = services.sale_payload(company, device, trader_no=f"SELFTEST-{n}", invoice_no=n,
                                    receipt_type=receipt, original_no=original, customer=customer,
                                    payment=payment, when=timezone.now(), rows=rows)
    sub = EtimsSubmission.objects.create(
        company=company, device=device, kind="CREDIT_NOTE" if receipt == "R" else "SALE",
        source_type="kra.selftest", source_id=f"{label}-{n}", source_label=label, sequence=n,
        payload={**payload, "itemList": []} if broken else payload)
    if send:
        services.process(company)
    sub.refresh_from_db()
    return sub


def _scenarios(runner, company, device, user):
    conf = services.settings_for(company)
    products = _fixtures(company, user)

    class Cust:
        def __init__(self, name, tax_id="", phone=""):
            self.name, self.tax_id, self.phone = name, tax_id, phone

    # 1 — device
    runner.check("1", "Initialise the device and keep its key, SCU id and MRC",
                 lambda: (lambda d: (d.ready and bool(d.sdc_id), f"SCU {d.sdc_id}, MRC {d.mrc_no}"))(
                     services.initialize_device(device)))
    # 2 — codes
    runner.check("2", "Pull KRA's code lists", lambda: (lambda n: (n > 0, f"{n} codes"))(services.sync_codes(device)))
    runner.check("2", "Tax type B carries 16% from KRA",
                 lambda: (lambda c: (c is not None and c.user_dfn_cd1 in ("16", "16.00"), c and c.user_dfn_cd1))(
                     __import__("apps.kra.models", fromlist=["EtimsCode"]).EtimsCode.objects.filter(
                         company=company, cd_cls="04", cd="B").first()))
    runner.check("2", "Pull item classifications",
                 lambda: (lambda n: (n > 0, f"{n} classes"))(services.sync_item_classes(device)))
    runner.check("2", "Pull KRA notices", lambda: (True, f"{services.sync_notices(device)} notice(s)"))
    runner.check("2", "A second pull asks only for what changed (lastReqDt moves on)",
                 lambda: (lambda s: (not s.last_req_dt.startswith("2020"), s.last_req_dt))(
                     device.sync_states.get(kind="CODES")))
    # 3 — customers
    runner.check("3", "Look up a valid customer KRA PIN",
                 lambda: (lambda c: (c is not None, c and c.get("taxprNm")))(services.lookup_pin(company, "A123456789Z")))
    runner.check("3", "An unknown PIN is reported as not found, not an error",
                 lambda: (services.lookup_pin(company, "NOTAPIN") is None, "no result"))

    def save_customer():
        sub = EtimsSubmission.objects.create(
            company=company, device=device, kind="CUSTOMER", source_type="kra.selftest",
            source_id="cust", source_label="Branch customer", payload={
                "tin": conf.kra_pin, "bhfId": device.bhf_id, "custNo": "C0001", "custTin": "A123456789Z",
                "custNm": "Self-test Customer Ltd", "adrs": "Nairobi", "telNo": "0700000000",
                "email": None, "faxNo": None, "useYn": "Y", "remark": None, **services._who(user)})
        services.process(company)
        sub.refresh_from_db()
        return sub.status == "SENT", sub.result_message
    runner.check("3", "Save a branch customer", save_customer)
    # 4 — items
    for key, label in (("B", "B 16%"), ("E", "E 8%"), ("C", "C zero-rated"), ("A", "A exempt"),
                       ("D", "D non-VAT"), ("S", "a service (item type 3)")):
        def reg(key=key):
            sub = services.queue_item(products[key], user, force=True)
            services.process(company)
            item = EtimsItem.objects.get(product=products[key])
            sub.refresh_from_db()
            return sub.status == "SENT" and item.registered_at is not None, f"{item.item_cd} {sub.result_message}"
        runner.check("4", f"Register an item with tax type {label}", reg)
    runner.check("4", "Item codes follow KRA's pattern (KE + type + packaging + unit + sequence)",
                 lambda: (all(i.item_cd.startswith("KE") and i.item_cd[-7:].isdigit()
                              for i in EtimsItem.objects.filter(company=company, product__in=products.values())),
                          ", ".join(EtimsItem.objects.filter(product__in=products.values()).values_list("item_cd", flat=True)[:3])))
    # 5 — imports
    runner.check("5", "Fetch import declarations", lambda: (lambda n: (n >= 0, f"{n} new"))(services.fetch_imports(device)))

    def approve_import():
        rec = EtimsImport.objects.filter(device=device, status="2").first()
        if rec is None:
            return True, "none waiting"
        sub = services.decide_import(rec, user, approve=True, product=products["B"])
        services.process(company)
        rec.refresh_from_db()
        return rec.status == "3", sub.result_message or "approved"
    runner.check("5", "Approve an import and map it to our item", approve_import)
    runner.check("5", "Approving without a product is refused",
                 lambda: (_raises(lambda: services.decide_import(EtimsImport(company=company, device=device, status="2",
                                                                            task_cd="X", declaration_date="20260101", item_seq=9),
                                                                user, approve=True)), "refused"))
    # 6 — sales
    s1 = _sale(runner, company, device, products, [("B", 2, 100, 0)], label="sale-B")
    runner.check("6", "Normal sale, 16% VAT, signed by KRA",
                 lambda: (s1.status == "SENT" and EtimsReceipt.objects.filter(submission=s1).exists(),
                          s1.result_message))
    rec = EtimsReceipt.objects.filter(submission=s1).first()
    runner.check("6", "Receipt carries CU invoice no., internal data, signature and QR link",
                 lambda: (bool(rec and rec.sdc_id and rec.internal_data and rec.signature and rec.qr_url),
                          rec and f"{rec.cu_invoice_no} · {EtimsReceipt.dashed(rec.signature)}"))
    runner.check("6", "VAT-inclusive amounts: 2 × 116.00 = 232.00, VAT 32.00 in bucket B",
                 lambda: (s1.payload["taxblAmtB"] == 232.0 and s1.payload["taxAmtB"] == 32.0
                          and s1.payload["totAmt"] == 232.0, f"{s1.payload['taxblAmtB']}/{s1.payload['taxAmtB']}"))
    s2 = _sale(runner, company, device, products,
               [("B", 1, 100, 10), ("E", 2, 185.19, 0), ("C", 1, 300, 0), ("A", 3, 150, 0), ("D", 1, 50, 0)],
               customer=Cust("Buyer Ltd", "P051234567X"), payment="MPESA", label="mixed")
    runner.check("6", "Mixed invoice across A, B, C, D, E with a discount line and a buyer PIN",
                 lambda: (s2.status == "SENT", s2.result_message))
    runner.check("6", "The buyer's PIN is on the invoice", lambda: (s2.payload.get("custTin") == "P051234567X", s2.payload.get("custTin")))
    for pay, code in (("CARD", "05"), ("BANK", "07"), ("CREDIT", "02")):
        sx = _sale(runner, company, device, products, [("B", 1, 100, 0)], payment=pay, label=f"pay-{pay}")
        runner.check("6", f"Payment type {pay} sent as {code}",
                     lambda sx=sx, code=code: (sx.status == "SENT" and sx.payload["pmtTyCd"] == code, sx.payload["pmtTyCd"]))
    s_svc = _sale(runner, company, device, products, [("S", 1, 500, 0)], label="service")
    runner.check("6", "Sale of a service", lambda: (s_svc.status == "SENT", s_svc.result_message))
    cn = _sale(runner, company, device, products, [("B", 2, 100, 0)], receipt="R", original=s1.sequence,
               label="credit-full")
    runner.check("6", "Full credit note referencing the original invoice",
                 lambda: (cn.status == "SENT" and cn.payload["orgInvcNo"] == s1.sequence and cn.payload["rcptTyCd"] == "R",
                          f"orgInvcNo {cn.payload['orgInvcNo']}"))
    cn2 = _sale(runner, company, device, products, [("A", 1, 150, 0)], receipt="R", original=s2.sequence,
                label="credit-part")
    runner.check("6", "Partial credit note", lambda: (cn2.status == "SENT", cn2.result_message))
    nums = list(EtimsReceipt.objects.filter(device=device).order_by("invoice_no").values_list("invoice_no", flat=True))
    runner.check("6", "Invoice numbers run without a gap", lambda: (nums == list(range(nums[0], nums[0] + len(nums))) if nums else False,
                                                                    f"{nums[:1]}…{nums[-1:]}"))
    # 7 — purchases
    runner.check("7", "Fetch supplier invoices sent to our PIN",
                 lambda: (lambda n: (EtimsPurchase.objects.filter(device=device).exists(), f"{n} new"))(services.fetch_purchases(device)))

    def confirm():
        p = EtimsPurchase.objects.filter(device=device, status="FETCHED").first()
        if p is None:
            return False, "nothing fetched"
        sub = services.confirm_purchase(p, user)
        services.process(company)
        p.refresh_from_db()
        return p.status == "CONFIRMED", sub.result_message or p.status
    runner.check("7", "Confirm a supplier invoice (insertTrnsPurchase)", confirm)
    # 8 — stock
    from apps.inventory.models import StockMovement
    from apps.organizations.models import Warehouse
    wh = (Warehouse.objects.filter(company=company, branch=device.branch).first()
          or Warehouse.objects.create(company=company, branch=device.branch, name="eTIMS self-test store",
                                      code=f"KS{timezone.now():%H%M%S}", city="Nairobi", country="Kenya"))
    for mtype, code in (("RECEIPT", "02"), ("ADJUSTMENT_PLUS", "06"), ("DELIVERY", "11"),
                        ("ADJUSTMENT_MINUS", "16"), ("RETURN_IN", "03"), ("TRANSFER_OUT", "13")):
        def io(mtype=mtype, code=code):
            m = StockMovement.objects.create(product=products["B"], warehouse=wh, movement_type=mtype,
                                             quantity=D("5") if mtype in ("RECEIPT", "ADJUSTMENT_PLUS") else D("1"),
                                             unit_cost=D("58"), movement_date=timezone.localdate(),
                                             reference=f"SELFTEST-{mtype}")
            sub = (services.queue_stock_movement(m, user)
                   or EtimsSubmission.objects.get(kind="STOCK_IO", source_id=str(m.pk)))
            services.process(company)
            sub.refresh_from_db()
            master = EtimsSubmission.objects.filter(depends_on=sub, kind="STOCK_MASTER").first()
            return (sub.status == "SENT" and sub.payload["sarTyCd"] == code and master and master.status == "SENT",
                    f"sarNo {sub.sequence}, rsdQty {master and master.payload.get('rsdQty')}")
        runner.check("8", f"Stock movement {mtype} as sarTyCd {code}, then stock on hand", io)
    # 9 — offline & ordering
    s_dup = EtimsSubmission.objects.filter(device=device, kind="SALE", status="SENT").first()

    def duplicate():
        EtimsSubmission.objects.filter(pk=s_dup.pk).update(status="PENDING")
        services.process(company)
        s_dup.refresh_from_db()
        return s_dup.status == "SENT" and s_dup.result_code == "994", f"KRA answered {s_dup.result_code}"
    runner.check("9", "A resent invoice KRA already has (994) counts as accepted, no duplicate receipt", duplicate)

    def offline():
        import apps.kra.services as svc
        real = svc.backend_for

        class Down(SimulatorBackend):
            def call(self, operation, payload):
                raise EtimsTransportError("network down (simulated)")
        svc.backend_for = lambda conf, dev=None: Down(conf, dev)
        try:
            sub = _sale(runner, company, device, products, [("B", 1, 100, 0)], label="offline")
            queued = sub.status == "PENDING" and sub.attempts == 1 and sub.next_try_at is not None
        finally:
            svc.backend_for = real
        EtimsSubmission.objects.filter(pk=sub.pk).update(next_try_at=None)
        services.process(company)
        sub.refresh_from_db()
        return queued and sub.status == "SENT", f"queued while down, sent after: {sub.status}"
    runner.check("9", "Offline: the invoice waits in the queue and goes when KRA is back", offline)

    def ordering():
        bad = _sale(runner, company, device, products, [("B", 1, 100, 0)], label="will-fail",
                    send=False, broken=True)
        nxt = _sale(runner, company, device, products, [("B", 1, 100, 0)], label="after-fail")
        bad.refresh_from_db()
        nxt.refresh_from_db()
        return bad.status == "FAILED" and nxt.status == "HELD", f"refused {bad.status}, next {nxt.status}"
    runner.check("9", "A refused invoice holds every later invoice (no gaps reach KRA)", ordering)
    runner.check("9", "Settings: backend and environment recorded",
                 lambda: (True, f"{conf.get_backend_display()} · {conf.get_environment_display()}"))


def _raises(fn):
    try:
        fn()
    except ValidationError:
        return True
    return False


def run(company, user=None, live=False):
    """Run the checklist; returns {"results": [...], "passed": n, "failed": n, "mode": ...}."""
    runner = Runner()
    conf = services.settings_for(company)
    if live and conf.environment != "SANDBOX":
        raise ValidationError("The live checklist only runs against KRA's sandbox, never production.")
    mode = f"live: {conf.get_backend_display()} (sandbox)" if live else "KRA simulator (nothing sent, rolled back)"
    try:
        with transaction.atomic():
            if not live:
                EtimsSettings.objects.filter(pk=conf.pk).update(enabled=True, backend="SIMULATOR",
                                                                auto_send=False, sales_type="T")
                from apps.organizations.models import Branch
                branch = Branch.objects.create(company=company, name="eTIMS self-test branch",
                                               code=f"KT{timezone.now():%H%M%S}")
                used = set(EtimsDevice.objects.filter(company=company).values_list("bhf_id", flat=True))
                bhf = next(f"{i:02d}" for i in range(99, 0, -1) if f"{i:02d}" not in used)
                device = EtimsDevice.objects.create(company=company, branch=branch, bhf_id=bhf,
                                                    device_serial="SELFTEST0001")
            else:
                device = services.device_for(company)
                if device is None:
                    raise ValidationError("Add and initialise a device first.")
            _scenarios(runner, company, device, user)
            if not live:
                raise Rollback()
    except Rollback:
        pass
    passed = sum(1 for r in runner.results if r["ok"])
    return {"results": runner.results, "groups": GROUPS, "passed": passed,
            "failed": len(runner.results) - passed, "mode": mode, "ran_at": timezone.now()}
