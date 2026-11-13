"""KRA eTIMS: build the canonical payloads, queue them, send them in order,
and keep what KRA signs.

Rules from the KRA OSCU/VSCU v2.0 specifications:
* invcNo runs 1, 2, 3 … per device (tin + bhfId) and KRA refuses a gap; a
  credit note carries the original's invcNo in orgInvcNo, rcptTyCd "R".
* line amounts are VAT-inclusive: splyAmt = qty × prc, taxblAmt = splyAmt −
  dcAmt, taxAmt = the VAT inside taxblAmt; the A–E buckets and totAmt must
  equal the sum of the lines.
* items are registered (saveItem) before a sale uses them; a sale goes
  before its stock movement (insertStockIO), and that before the stock on
  hand (saveStockMaster).
* nothing is lost offline: every call waits in the outbox until KRA accepts
  it, 994 ("already have it") counts as accepted.
"""
import datetime
import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from .backends import DUPLICATE, NO_RESULT, OK, EtimsTransportError, backend_for, now_stamp
from .models import (EtimsCode, EtimsDevice, EtimsImport, EtimsItem, EtimsItemClass, EtimsNotice,
                     EtimsPurchase, EtimsReceipt, EtimsSettings, EtimsSubmission, EtimsSyncState,
                     EtimsTaxMap)

D = Decimal
Z = D("0")
CENT = D("0.01")
RETRY_MINUTES = [1, 2, 5, 15, 30, 60]

PAYMENT_CODES = {"CASH": "01", "CREDIT": "02", "CARD": "05", "MPESA": "06", "BANK": "07",
                 "CHEQUE": "04", "SPLIT": "03"}
STOCK_IO_CODES = {
    "RECEIPT": "02", "OPENING": "06", "ADJUSTMENT_PLUS": "06", "RETURN_IN": "03",
    "TRANSFER_IN": "04", "PRODUCTION_IN": "05",
    "DELIVERY": "11", "RETURN_OUT": "12", "TRANSFER_OUT": "13", "PRODUCTION_OUT": "14",
    "ADJUSTMENT_MINUS": "16",
}
INBOUND = {"RECEIPT", "OPENING", "ADJUSTMENT_PLUS", "RETURN_IN", "TRANSFER_IN", "PRODUCTION_IN"}
# Order inside one device's queue: what KRA needs first goes first.
PRIORITY = ["INIT", "CODES", "ITEM_CLASSES", "CUSTOMER", "USER", "ITEM", "SALE", "CREDIT_NOTE",
            "PURCHASE", "IMPORT_UPDATE", "STOCK_IO", "STOCK_MASTER"]
SEQUENTIAL = {"SALE", "CREDIT_NOTE"}
# Numbered families KRA checks for gaps: invoices (invcNo) and stock movements (sarNo).
FAMILIES = {"SALE": ("SALE", "CREDIT_NOTE"), "CREDIT_NOTE": ("SALE", "CREDIT_NOTE"),
            "STOCK_IO": ("STOCK_IO",)}


def cents(v):
    return D(str(v or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def amount(v):
    """KRA wants plain numbers in JSON."""
    return float(cents(v))


def ymd(d):
    return d.strftime("%Y%m%d")


def ymdhms(dt):
    return timezone.localtime(dt).strftime("%Y%m%d%H%M%S")


# ─────────────────────────────── settings & devices ──────────────────────────────

def settings_for(company):
    found, _ = EtimsSettings.objects.get_or_create(company=company)
    return found


def is_on(company):
    found = EtimsSettings.objects.filter(company=company).first()
    return bool(found and found.enabled)


def device_for(company, branch=None):
    """The device for a branch, else head office's (bhfId 00), else any."""
    qs = EtimsDevice.objects.filter(company=company, is_active=True)
    if branch is not None:
        found = qs.filter(branch=branch).first()
        if found:
            return found
    return qs.filter(bhf_id="00").first() or qs.first()


def initialize_device(device):
    """selectInitOsdcInfo: KRA hands back the cmcKey, SCU id and MRC number."""
    conf = settings_for(device.company)
    if not conf.kra_pin:
        raise ValidationError("Set the company's KRA PIN first (eTIMS settings or the company record).")
    payload = {"tin": conf.kra_pin, "bhfId": device.bhf_id, "dvcSrlNo": device.device_serial}
    try:
        resp = backend_for(conf, device).call("init", payload)
    except EtimsTransportError as exc:
        EtimsDevice.objects.filter(pk=device.pk).update(last_error=str(exc))
        raise ValidationError(str(exc))
    code = str(resp.get("resultCd", ""))
    info = ((resp.get("data") or {}).get("info") or {})
    if code != OK or not info.get("cmcKey"):
        msg = f"KRA refused initialisation ({code}): {resp.get('resultMsg', '')}"
        if code == "902":
            msg += " — the device is already installed; ask KRA (timsupport@kra.go.ke) for its key."
        EtimsDevice.objects.filter(pk=device.pk).update(last_error=msg)
        raise ValidationError(msg)
    for model_field, key in (("cmc_key", "cmcKey"), ("sdc_id", "sdcId"), ("mrc_no", "mrcNo"),
                             ("dvc_id", "dvcId"), ("taxpayer_name", "taxprNm"),
                             ("branch_name", "bhfNm")):
        setattr(device, model_field, str(info.get(key) or ""))
    device.initialized_at = timezone.now()
    device.last_error = ""
    device.save()
    return device


# ─────────────────────────────── tax types & items ───────────────────────────────

def tax_type(company, tax_rate):
    """KRA tax letter for one of our tax rates (A exempt, B 16%, C 0%, D non-VAT, E 8%)."""
    if tax_rate is None:
        return "D"
    mapped = EtimsTaxMap.objects.filter(tax_rate=tax_rate).values_list("tax_type", flat=True).first()
    if mapped:
        return mapped
    rate = D(str(tax_rate.rate))
    if rate == D("16"):
        return "B"
    if rate == D("8"):
        return "E"
    if rate == 0:
        return "C"
    raise ValidationError(f"Tax rate {tax_rate} ({rate}%) has no KRA tax type. Map it in eTIMS settings.")


def tax_percent(letter):
    return {"A": Z, "B": D("16"), "C": Z, "D": Z, "E": D("8")}[letter]


def _next_item_code(company, ty, pkg, qty):
    prefix = f"KE{ty}{pkg}{qty}"
    last = EtimsItem.objects.filter(company=company, item_cd__startswith=prefix).aggregate(
        m=Max("item_cd"))["m"]
    seq = int(last[len(prefix):]) + 1 if last and last[len(prefix):].isdigit() else 1
    return f"{prefix}{seq:07d}"


QTY_UNITS = {"PCS": "U", "PC": "U", "EA": "U", "KG": "KG", "G": "GRM", "GRM": "GRM", "L": "L",
             "LTR": "L", "ML": "ML", "M": "MTR", "MTR": "MTR", "BOX": "BX", "BX": "BX",
             "PKT": "PA", "DZ": "DZ", "SET": "SET", "PR": "PR"}


def item_for(product):
    """The product's KRA item record, created with sensible codes the first time."""
    found = EtimsItem.objects.filter(product=product).first()
    if found:
        return found
    ty = "3" if product.product_type == "SERVICE" or not product.track_inventory else "2"
    abbr = (getattr(product.unit_of_measure, "abbreviation", "") or "").upper()
    qty = QTY_UNITS.get(abbr, "U")
    pkg = "NT"
    for _ in range(3):
        try:
            with transaction.atomic():
                return EtimsItem.objects.create(
                    company=product.company, product=product, item_ty_cd=ty, pkg_unit_cd=pkg,
                    qty_unit_cd=qty, item_cd=_next_item_code(product.company, ty, pkg, qty))
        except Exception:      # a parallel save took the number: try the next one
            continue
    raise ValidationError(f"Could not give {product.name} a KRA item code.")


def item_payload(product, user=None):
    item = item_for(product)
    letter = tax_type(product.company, product.tax_rate)
    price = cents(D(str(product.selling_price or 0)) * (1 + tax_percent(letter) / 100))
    who = _who(user)
    return {
        "itemCd": item.item_cd, "itemClsCd": item.item_cls_cd, "itemTyCd": item.item_ty_cd,
        "itemNm": product.name[:200], "itemStdNm": None, "orgnNatCd": item.origin_country,
        "pkgUnitCd": item.pkg_unit_cd, "qtyUnitCd": item.qty_unit_cd, "taxTyCd": letter,
        "btchNo": None, "bcd": product.barcode or None, "dftPrc": amount(price),
        "grpPrcL1": amount(price), "grpPrcL2": None, "grpPrcL3": None, "grpPrcL4": None,
        "grpPrcL5": None, "addInfo": None, "sftyQty": None, "isrcAplcbYn": "N",
        "useYn": "Y" if product.is_active else "N", **who,
    }


def _who(user):
    uid = (getattr(user, "username", "") or "system")[:20]
    name = (user.get_full_name() if user and hasattr(user, "get_full_name") else "") or uid
    return {"regrId": uid, "regrNm": name[:60], "modrId": uid, "modrNm": name[:60]}


def _hash(payload):
    keep = {k: v for k, v in payload.items() if not k.startswith(("regr", "modr"))}
    return hashlib.sha256(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()


def queue_item(product, user=None, force=False):
    """saveItem when the product is new to KRA or what KRA holds has changed."""
    if not is_on(product.company):
        return None
    device = device_for(product.company)
    if device is None:
        return None
    payload = item_payload(product, user)
    item = item_for(product)
    digest = _hash(payload)
    if not force and item.registered_hash == digest:
        return None
    if EtimsSubmission.objects.filter(company=product.company, kind="ITEM", source_id=str(product.pk),
                                      status__in=("PENDING", "HELD")).exists():
        EtimsSubmission.objects.filter(company=product.company, kind="ITEM", source_id=str(product.pk),
                                       status__in=("PENDING", "HELD")).update(payload=payload)
        return None
    return EtimsSubmission.objects.create(
        company=product.company, device=device, kind="ITEM", source_type="inventory.Product",
        source_id=str(product.pk), source_label=product.sku, payload=payload)


# ─────────────────────────────────── sales ──────────────────────────────────────

def _sale_lines(company, rows):
    """rows: dicts {product, name, qty, unit_price (net), discount_pct, tax_rate, net, tax}.
    Returns KRA itemList and the A–E buckets, VAT-inclusive as KRA wants."""
    items, buckets, taxes = [], {k: Z for k in "ABCDE"}, {k: Z for k in "ABCDE"}
    for seq, r in enumerate(rows, 1):
        letter = tax_type(company, r["tax_rate"])
        rate = tax_percent(letter)
        item = item_for(r["product"]) if r["product"] is not None else None
        net, vat = cents(r["net"]), cents(r["tax"])
        taxbl = net + vat
        prc = cents(D(str(r["unit_price"])) * (1 + rate / 100))
        sply = cents(D(str(r["qty"])) * prc)
        dc = max(sply - taxbl, Z)
        items.append({
            "itemSeq": seq, "itemCd": item.item_cd if item else "KE3NTU0000000",
            "itemClsCd": item.item_cls_cd if item else "8111180000",
            "itemNm": (r["name"] or "")[:200], "bcd": (r["product"].barcode or None) if r["product"] else None,
            "pkgUnitCd": item.pkg_unit_cd if item else "NT", "pkg": float(r["qty"]),
            "qtyUnitCd": item.qty_unit_cd if item else "U", "qty": float(r["qty"]),
            "prc": amount(prc), "splyAmt": amount(sply), "dcRt": float(cents(r["discount_pct"])),
            "dcAmt": amount(dc), "isrccCd": None, "isrccNm": None, "isrcRt": None, "isrcAmt": None,
            "taxTyCd": letter, "taxblAmt": amount(taxbl), "taxAmt": amount(vat),
            "totAmt": amount(taxbl)})
        buckets[letter] += taxbl
        taxes[letter] += vat
    return items, buckets, taxes


def sale_payload(company, device, *, trader_no, invoice_no, receipt_type, original_no, customer,
                 payment, when, rows, user=None, remark="", refund_reason=None):
    conf = settings_for(company)
    items, buckets, taxes = _sale_lines(company, rows)
    tot = sum(buckets.values(), Z)
    cust_tin = (getattr(customer, "tax_id", "") or "").strip().upper()[:11] or None
    payload = {
        "tin": conf.kra_pin, "bhfId": device.bhf_id, "trdInvcNo": str(trader_no)[:50],
        "invcNo": invoice_no, "orgInvcNo": original_no or 0, "custTin": cust_tin,
        "custNm": (getattr(customer, "name", "") or "Walk-in customer")[:60],
        "salesTyCd": conf.sales_type or "N", "rcptTyCd": receipt_type,
        "pmtTyCd": PAYMENT_CODES.get(payment, "07"), "salesSttsCd": "02",
        "cfmDt": ymdhms(when), "salesDt": ymd(timezone.localtime(when).date()),
        "stockRlsDt": ymdhms(when), "cnclReqDt": None, "cnclDt": None,
        "rfdDt": ymdhms(when) if receipt_type == "R" else None,
        "rfdRsnCd": (refund_reason or "06") if receipt_type == "R" else None,
        "totItemCnt": len(items), "prchrAcptcYn": "N", "remark": (remark or "")[:400] or None,
        "totTaxblAmt": amount(tot), "totTaxAmt": amount(sum(taxes.values(), Z)), "totAmt": amount(tot),
        "receipt": {"custTin": cust_tin, "custMblNo": (getattr(customer, "phone", "") or None),
                    "rptNo": None, "rcptPbctDt": ymdhms(when), "trdeNm": None, "adrs": None,
                    "topMsg": company.name[:200], "btmMsg": "Thank you", "prchrAcptcYn": "N"},
        "itemList": items, **_who(user),
    }
    for k in "ABCDE":
        payload[f"taxblAmt{k}"] = amount(buckets[k])
        payload[f"taxRt{k}"] = float(tax_percent(k))
        payload[f"taxAmt{k}"] = amount(taxes[k])
    return payload


def _claim_invoice_no(device):
    """Take the next invcNo for this device, one at a time."""
    EtimsDevice.objects.filter(pk=device.pk).update(updated_at=timezone.now())
    device.refresh_from_db(fields=["next_invoice_no"])
    n = device.next_invoice_no
    EtimsDevice.objects.filter(pk=device.pk).update(next_invoice_no=n + 1)
    return n


def _claim_stock_no(device):
    EtimsDevice.objects.filter(pk=device.pk).update(updated_at=timezone.now())
    device.refresh_from_db(fields=["next_stock_no"])
    n = device.next_stock_no
    EtimsDevice.objects.filter(pk=device.pk).update(next_stock_no=n + 1)
    return n


def _already(company, kind, source_type, source_id):
    return EtimsSubmission.objects.filter(company=company, kind=kind, source_type=source_type,
                                          source_id=str(source_id)).exclude(status="CANCELLED").first()


def _original_no(company, source_type, source_id):
    rec = EtimsReceipt.objects.filter(company=company, source_type=source_type,
                                      source_id=str(source_id)).first()
    if rec:
        return rec.invoice_no
    sub = _already(company, "SALE", source_type, source_id)
    return sub.sequence if sub else 0


def _queue_items_for(company, rows, user):
    for r in rows:
        if r["product"] is not None:
            queue_item(r["product"], user)


@transaction.atomic
def queue_invoice(invoice, user=None):
    """A posted invoice or credit note, fiscalised on the device of its branch."""
    company = invoice.company
    if not is_on(company):
        return None
    credit = invoice.status == "CREDIT_NOTE" or invoice.original_invoice_id is not None
    kind = "CREDIT_NOTE" if credit else "SALE"
    found = _already(company, kind, "sales.Invoice", invoice.pk)
    if found:
        return found
    device = device_for(company, getattr(invoice, "branch", None))
    if device is None:
        return None
    rate = D(str(invoice.exchange_rate)) if (invoice.currency or "KES") != "KES" and invoice.exchange_rate else D("1")
    rows = []
    for line in invoice.lines.select_related("product__unit_of_measure", "tax_rate"):
        net = cents(D(str(line.line_total)) * rate)
        rows.append({"product": line.product, "name": line.description or (line.product.name if line.product else ""),
                     "qty": abs(D(str(line.quantity))), "unit_price": D(str(line.unit_price)) * rate,
                     "discount_pct": D(str(line.discount_percent or 0)), "tax_rate": line.tax_rate,
                     "net": abs(net), "tax": abs(cents(D(str(line.tax_amount)) * rate))})
    if not rows:
        return None
    _queue_items_for(company, rows, user)
    original = _original_no(company, "sales.Invoice", invoice.original_invoice_id) if credit else 0
    n = _claim_invoice_no(device)
    when = timezone.make_aware(datetime.datetime.combine(invoice.invoice_date, datetime.time(12))) \
        if invoice.invoice_date != timezone.localdate() else timezone.now()
    payload = sale_payload(company, device, trader_no=invoice.reference, invoice_no=n,
                           receipt_type="R" if credit else "S", original_no=original,
                           customer=invoice.customer, payment="CREDIT", when=when, rows=rows,
                           user=user, remark=invoice.notes if hasattr(invoice, "notes") else "")
    return EtimsSubmission.objects.create(
        company=company, device=device, kind=kind, source_type="sales.Invoice",
        source_id=str(invoice.pk), source_label=invoice.reference, sequence=n, payload=payload)


@transaction.atomic
def queue_pos_sale(sale, user=None):
    """A till ticket (or its refund) fiscalised on the till's branch device."""
    company = sale.session.company
    if not is_on(company):
        return None
    credit = sale.refund_of_id is not None
    kind = "CREDIT_NOTE" if credit else "SALE"
    found = _already(company, kind, "pos.POSSale", sale.pk)
    if found:
        return found
    device = device_for(company, sale.session.branch)
    if device is None:
        return None
    rows = []
    for line in sale.lines.select_related("product__unit_of_measure", "tax_rate"):
        net = cents(D(str(line.line_total)) - D(str(line.tax_amount)))
        rows.append({"product": line.product, "name": line.product.name, "qty": abs(D(str(line.quantity))),
                     "unit_price": D(str(line.unit_price)), "discount_pct": D(str(line.discount_percent or 0)),
                     "tax_rate": line.tax_rate, "net": abs(net), "tax": abs(cents(line.tax_amount))})
    if not rows:
        return None
    _queue_items_for(company, rows, user)
    original = _original_no(company, "pos.POSSale", sale.refund_of_id) if credit else 0
    n = _claim_invoice_no(device)
    payload = sale_payload(company, device, trader_no=sale.reference, invoice_no=n,
                           receipt_type="R" if credit else "S", original_no=original,
                           customer=sale.customer, payment=sale.payment_method, when=sale.created_at,
                           rows=rows, user=user or sale.session.cashier,
                           remark=sale.notes or "")
    return EtimsSubmission.objects.create(
        company=company, device=device, kind=kind, source_type="pos.POSSale",
        source_id=str(sale.pk), source_label=sale.reference, sequence=n, payload=payload)


# ─────────────────────────────────── stock ──────────────────────────────────────

def on_hand(product, branch):
    from apps.inventory.models import StockMovement
    qs = StockMovement.objects.filter(product=product)
    if branch is not None:
        qs = qs.filter(Q(warehouse__branch=branch) | Q(warehouse__branch__isnull=True))
    total = Z
    for mt, qty in qs.values_list("movement_type", "quantity"):
        total += qty if mt in INBOUND else -qty
    return total


@transaction.atomic
def queue_stock_movement(movement, user=None):
    product = movement.product
    company = product.company
    if not is_on(company) or not product.track_inventory:
        return None
    code = STOCK_IO_CODES.get(movement.movement_type)
    if code is None:
        return None
    if _already(company, "STOCK_IO", "inventory.StockMovement", movement.pk):
        return None
    branch = getattr(movement.warehouse, "branch", None)
    device = device_for(company, branch)
    if device is None:
        return None
    queue_item(product, user)
    item = item_for(product)
    letter = tax_type(company, product.tax_rate)
    qty = abs(D(str(movement.quantity)))
    prc = cents(movement.unit_cost or product.cost_price or 0)
    sply = cents(qty * prc)
    vat = cents(sply * tax_percent(letter) / (100 + tax_percent(letter))) if tax_percent(letter) else Z
    n = _claim_stock_no(device)
    payload = {
        "tin": settings_for(company).kra_pin, "bhfId": device.bhf_id, "sarNo": n, "orgSarNo": 0,
        "regTyCd": "A", "custTin": None, "custNm": None, "custBhfId": None, "sarTyCd": code,
        "ocrnDt": ymd(movement.movement_date), "totItemCnt": 1, "totTaxblAmt": amount(sply),
        "totTaxAmt": amount(vat), "totAmt": amount(sply), "remark": (movement.reference or "")[:400] or None,
        "itemList": [{
            "itemSeq": 1, "itemCd": item.item_cd, "itemClsCd": item.item_cls_cd,
            "itemNm": product.name[:200], "bcd": product.barcode or None, "pkgUnitCd": item.pkg_unit_cd,
            "pkg": float(qty), "qtyUnitCd": item.qty_unit_cd, "qty": float(qty), "itemExprDt": None,
            "prc": amount(prc), "splyAmt": amount(sply), "totDcAmt": 0, "taxblAmt": amount(sply),
            "taxTyCd": letter, "taxAmt": amount(vat), "totAmt": amount(sply)}],
        **_who(user or movement.performed_by),
    }
    io = EtimsSubmission.objects.create(
        company=company, device=device, kind="STOCK_IO", source_type="inventory.StockMovement",
        source_id=str(movement.pk), source_label=movement.reference or product.sku, sequence=n,
        payload=payload)
    master = {"tin": payload["tin"], "bhfId": device.bhf_id, "itemCd": item.item_cd,
              "rsdQty": float(on_hand(product, branch)), **_who(user or movement.performed_by)}
    EtimsSubmission.objects.create(
        company=company, device=device, kind="STOCK_MASTER", source_type="inventory.Product",
        source_id=str(product.pk), source_label=product.sku, payload=master, depends_on=io)
    return io


# ───────────────────────────── purchases & imports ───────────────────────────────

def confirm_purchase(purchase, user=None, accept=True):
    """insertTrnsPurchase: confirm (or reject) a supplier invoice KRA holds for us."""
    if purchase.status != "FETCHED":
        raise ValidationError("This supplier invoice has already been answered.")
    d = purchase.data
    device = purchase.device
    with transaction.atomic():
        # Our own purchase number, one after the last we sent on this device.
        n = (EtimsSubmission.objects.filter(device=device, kind="PURCHASE").aggregate(
            m=Max("sequence"))["m"] or 0) + 1
        payload = {
            "tin": settings_for(purchase.company).kra_pin, "bhfId": device.bhf_id, "invcNo": n,
            "orgInvcNo": 0, "spplrTin": purchase.supplier_tin, "spplrBhfId": purchase.supplier_bhf_id,
            "spplrNm": purchase.supplier_name, "spplrInvcNo": d.get("spplrInvcNo"),
            "regTyCd": "A", "pchsTyCd": "N", "rcptTyCd": "P", "pmtTyCd": d.get("pmtTyCd") or "01",
            "pchsSttsCd": "02" if accept else "04", "cfmDt": now_stamp(),
            "pchsDt": (d.get("salesDt") or ymd(timezone.localdate()))[:8], "wrhsDt": None,
            "cnclReqDt": None, "cnclDt": None, "rfdDt": None, "totItemCnt": d.get("totItemCnt") or 0,
            "totTaxblAmt": d.get("totTaxblAmt") or 0, "totTaxAmt": d.get("totTaxAmt") or 0,
            "totAmt": d.get("totAmt") or 0, "remark": None, "itemList": d.get("itemList") or [],
            **_who(user)}
        for k in "ABCDE":
            payload[f"taxblAmt{k}"] = d.get(f"taxblAmt{k}") or 0
            payload[f"taxRt{k}"] = float(tax_percent(k))
            payload[f"taxAmt{k}"] = d.get(f"taxAmt{k}") or 0
        sub = EtimsSubmission.objects.create(
            company=purchase.company, device=device, kind="PURCHASE", source_type="kra.EtimsPurchase",
            source_id=str(purchase.pk), source_label=f"{purchase.supplier_name} #{purchase.supplier_invoice_no}",
            sequence=n, payload=payload)
    return sub


def decide_import(record, user=None, approve=True, product=None):
    if record.status != "2":
        raise ValidationError("This import has already been decided.")
    if product is not None and product.company_id != record.company_id:
        raise ValidationError("That product is not one of this company's.")
    item = item_for(product) if product is not None else None
    if approve and item is None:
        raise ValidationError("Choose the product this import is, to approve it.")
    payload = {"tin": settings_for(record.company).kra_pin, "bhfId": record.device.bhf_id,
               "taskCd": record.task_cd, "dclDe": record.declaration_date, "itemSeq": record.item_seq,
               "hsCd": record.hs_code, "itemClsCd": item.item_cls_cd if item else None,
               "itemCd": item.item_cd if item else None, "imptItemSttsCd": "3" if approve else "4",
               "remark": None, "modrId": _who(user)["modrId"], "modrNm": _who(user)["modrNm"]}
    if product is not None:
        EtimsImport.objects.filter(pk=record.pk).update(product=product)
    return EtimsSubmission.objects.create(
        company=record.company, device=record.device, kind="IMPORT_UPDATE",
        source_type="kra.EtimsImport", source_id=str(record.pk),
        source_label=f"{record.item_name} ({record.declaration_no})", payload=payload)


# ──────────────────────────────── sending (outbox) ───────────────────────────────

OPERATION_FOR = {"ITEM": "item_save", "SALE": "sale_save", "CREDIT_NOTE": "sale_save",
                 "STOCK_IO": "stock_io", "STOCK_MASTER": "stock_master", "PURCHASE": "purchase_save",
                 "IMPORT_UPDATE": "import_update", "CUSTOMER": "customer_save", "USER": "user_save"}


def _due(company):
    now = timezone.now()
    return EtimsSubmission.objects.filter(company=company, status__in=("PENDING", "HELD")).filter(
        Q(next_try_at__isnull=True) | Q(next_try_at__lte=now))


def _sort_key(sub):
    return (PRIORITY.index(sub.kind) if sub.kind in PRIORITY else 99,
            sub.sequence or 0, sub.created_at)


def process(company, limit=200):
    """Send what is due, in KRA's order. Sales leave strictly by invcNo: a sale
    KRA refused holds every later sale of that device until it is fixed."""
    conf = settings_for(company)
    if not conf.enabled:
        return {"sent": 0, "failed": 0, "held": 0}
    stats = {"sent": 0, "failed": 0, "held": 0}
    blocked = set()       # (device, family) whose numbering is stuck
    for sub in sorted(_due(company).select_related("device")[:limit], key=_sort_key):
        dev = sub.device
        if dev is None or not dev.ready:
            stats["held"] += 1
            continue
        family = FAMILIES.get(sub.kind)
        if family:
            if (dev.pk, family) in blocked or EtimsSubmission.objects.filter(
                    device=dev, kind__in=family, sequence__lt=sub.sequence,
                    status__in=("PENDING", "HELD", "FAILED")).exists():
                EtimsSubmission.objects.filter(pk=sub.pk).update(status="HELD")
                stats["held"] += 1
                blocked.add((dev.pk, family))
                continue
        if sub.depends_on_id and sub.depends_on.status != "SENT":
            EtimsSubmission.objects.filter(pk=sub.pk).update(status="HELD")
            stats["held"] += 1
            continue
        result = send(sub, conf)
        stats[result] = stats.get(result, 0) + 1
        if result in ("failed", "transport") and family:
            blocked.add((dev.pk, family))
        if result == "transport":
            break       # KRA is unreachable: stop and try the whole queue later
    return stats


def send(sub, conf=None):
    conf = conf or settings_for(sub.company)
    backend = backend_for(conf, sub.device)
    attempts = sub.attempts + 1
    try:
        resp = backend.call(OPERATION_FOR[sub.kind], sub.payload)
    except EtimsTransportError as exc:
        wait = RETRY_MINUTES[min(attempts - 1, len(RETRY_MINUTES) - 1)]
        EtimsSubmission.objects.filter(pk=sub.pk).update(
            attempts=attempts, last_error=str(exc), status="PENDING",
            next_try_at=timezone.now() + datetime.timedelta(minutes=wait))
        return "transport"
    code = str(resp.get("resultCd", ""))
    fields = {"attempts": attempts, "response": resp, "result_code": code,
              "result_message": str(resp.get("resultMsg", ""))[:500], "next_try_at": None}
    if code in (OK, DUPLICATE):
        fields.update(status="SENT", sent_at=timezone.now(), last_error="")
        EtimsSubmission.objects.filter(pk=sub.pk).update(**fields)
        sub.refresh_from_db()
        _accepted(sub, resp, conf, duplicate=(code == DUPLICATE))
        # anything waiting on this one can go now
        EtimsSubmission.objects.filter(depends_on=sub, status="HELD").update(status="PENDING")
        if sub.kind in FAMILIES:
            EtimsSubmission.objects.filter(device=sub.device, kind__in=FAMILIES[sub.kind],
                                           status="HELD").update(status="PENDING")
        return "sent"
    fields.update(status="FAILED", last_error=f"KRA {code}: {resp.get('resultMsg', '')}")
    EtimsSubmission.objects.filter(pk=sub.pk).update(**fields)
    return "failed"


def _accepted(sub, resp, conf, duplicate=False):
    data = resp.get("data") or {}
    if sub.kind in SEQUENTIAL and not EtimsReceipt.objects.filter(submission=sub).exists():
        dev = sub.device
        sign = data.get("rcptSign") or ""
        signed = data.get("sdcDateTime") or data.get("vsdcRcptPbctDate") or ""
        try:
            signed_at = timezone.make_aware(datetime.datetime.strptime(signed[:14], "%Y%m%d%H%M%S"))
        except (ValueError, TypeError):
            signed_at = timezone.now()
        EtimsReceipt.objects.create(
            company=sub.company, device=dev, submission=sub, source_type=sub.source_type,
            source_id=sub.source_id, receipt_type=sub.payload.get("rcptTyCd", "S"),
            invoice_no=sub.sequence or 0, original_invoice_no=int(sub.payload.get("orgInvcNo") or 0),
            receipt_no=str(data.get("curRcptNo") or data.get("rcptNo") or sub.sequence or ""),
            total_receipt_no=str(data.get("totRcptNo") or ""), internal_data=data.get("intrlData") or "",
            signature=sign, sdc_id=data.get("sdcId") or dev.sdc_id, mrc_no=data.get("mrcNo") or dev.mrc_no,
            signed_at=signed_at,
            qr_url=backend_for(conf, dev).receipt_url(conf.kra_pin, dev.bhf_id, sign) if sign else "")
    elif sub.kind == "ITEM":
        EtimsItem.objects.filter(product_id=sub.source_id).update(
            registered_at=timezone.now(), registered_hash=_hash(sub.payload))
    elif sub.kind == "PURCHASE":
        EtimsPurchase.objects.filter(pk=sub.source_id).update(
            status="CONFIRMED" if sub.payload.get("pchsSttsCd") == "02" else "REJECTED")
    elif sub.kind == "IMPORT_UPDATE":
        EtimsImport.objects.filter(pk=sub.source_id).update(status=sub.payload.get("imptItemSttsCd") or "2")


def retry(sub):
    if sub.status not in ("FAILED", "HELD", "PENDING"):
        raise ValidationError("Only a waiting or refused submission can be sent again.")
    EtimsSubmission.objects.filter(pk=sub.pk).update(status="PENDING", next_try_at=None)
    return process(sub.company)


def send_soon(company):
    """After a document posts: try right away, so the receipt is signed while
    the customer waits. Whatever fails stays queued."""
    if not is_on(company) or not settings_for(company).auto_send:
        return

    def go():
        try:
            process(company, limit=50)
        except Exception:        # never let KRA break the sale that was just made
            pass
    transaction.on_commit(go)


# ─────────────────────────────────── pulls ──────────────────────────────────────

def _state(device, kind):
    found, _ = EtimsSyncState.objects.get_or_create(device=device, kind=kind)
    return found


def _pull(device, operation, kind, extra=None):
    conf = settings_for(device.company)
    state = _state(device, kind)
    payload = {"tin": conf.kra_pin, "bhfId": device.bhf_id, "lastReqDt": state.last_req_dt, **(extra or {})}
    started = now_stamp()
    try:
        resp = backend_for(conf, device).call(operation, payload)
    except EtimsTransportError as exc:
        raise ValidationError(str(exc))
    code = str(resp.get("resultCd", ""))
    EtimsSubmission.objects.create(
        company=device.company, device=device, kind=kind, status="SENT" if code in (OK, NO_RESULT) else "FAILED",
        payload=payload, response=resp, result_code=code,
        result_message=str(resp.get("resultMsg", ""))[:500], attempts=1, sent_at=timezone.now(),
        source_label=operation)
    if code not in (OK, NO_RESULT):
        raise ValidationError(f"KRA {code}: {resp.get('resultMsg', '')}")
    EtimsSyncState.objects.filter(pk=state.pk).update(last_req_dt=started, synced_at=timezone.now())
    return resp.get("data") or {}


def sync_codes(device):
    data = _pull(device, "codes", "CODES")
    n = 0
    for cls in data.get("clsList") or []:
        for d in cls.get("dtlList") or []:
            EtimsCode.objects.update_or_create(
                company=device.company, cd_cls=cls.get("cdCls"), cd=d.get("cd"),
                defaults={"cd_cls_name": cls.get("cdClsNm") or "", "name": d.get("cdNm") or "",
                          "user_dfn_cd1": str(d.get("userDfnCd1") or ""), "sort_order": d.get("srtOrd") or 0,
                          "use_yn": d.get("useYn", "Y") == "Y"})
            n += 1
    return n


def sync_item_classes(device):
    data = _pull(device, "item_classes", "ITEM_CLASSES")
    n = 0
    for c in data.get("itemClsList") or []:
        EtimsItemClass.objects.update_or_create(
            company=device.company, code=c.get("itemClsCd"),
            defaults={"name": c.get("itemClsNm") or "", "level": c.get("itemClsLvl") or 1,
                      "tax_type": c.get("taxTyCd") or "", "use_yn": c.get("useYn", "Y") == "Y"})
        n += 1
    return n


def sync_notices(device):
    data = _pull(device, "notices", "NOTICES")
    n = 0
    for x in data.get("noticeList") or []:
        EtimsNotice.objects.update_or_create(
            company=device.company, notice_no=str(x.get("noticeNo")),
            defaults={"title": x.get("title") or "", "content": x.get("cont") or "",
                      "detail_url": x.get("dtlUrl") or "", "registered_at": str(x.get("regDt") or "")})
        n += 1
    return n


def fetch_purchases(device):
    data = _pull(device, "purchase_list", "PURCHASE_LIST")
    n = 0
    for s in data.get("saleList") or []:
        _, created = EtimsPurchase.objects.get_or_create(
            device=device, supplier_tin=s.get("spplrTin") or "", supplier_bhf_id=s.get("spplrBhfId") or "",
            supplier_invoice_no=str(s.get("spplrInvcNo")),
            defaults={"company": device.company, "supplier_name": s.get("spplrNm") or "",
                      "sale_date": str(s.get("salesDt") or ""), "total_amount": D(str(s.get("totAmt") or 0)),
                      "total_tax": D(str(s.get("totTaxAmt") or 0)), "data": s})
        n += int(created)
    return n


def fetch_imports(device):
    data = _pull(device, "import_list", "IMPORT_LIST")
    n = 0
    for i in data.get("itemList") or []:
        _, created = EtimsImport.objects.get_or_create(
            device=device, task_cd=i.get("taskCd") or "", declaration_date=str(i.get("dclDe") or ""),
            item_seq=int(i.get("itemSeq") or 0),
            defaults={"company": device.company, "declaration_no": i.get("dclNo") or "",
                      "hs_code": i.get("hsCd") or "", "item_name": i.get("itemNm") or "",
                      "origin_country": i.get("orgnNatCd") or "", "quantity": D(str(i.get("qty") or 0)),
                      "qty_unit_cd": i.get("qtyUnitCd") or "", "data": i,
                      "status": str(i.get("imptItemsttsCd") or "2")})
        n += int(created)
    return n


def lookup_pin(company, pin):
    """selectCustomer: is this a KRA PIN, and whose?"""
    device = device_for(company)
    if device is None or not device.ready:
        raise ValidationError("Initialise an eTIMS device first.")
    conf = settings_for(company)
    try:
        resp = backend_for(conf, device).call("customer_lookup", {
            "tin": conf.kra_pin, "bhfId": device.bhf_id, "custmTin": (pin or "").strip().upper()})
    except EtimsTransportError as exc:
        raise ValidationError(str(exc))
    code = str(resp.get("resultCd", ""))
    if code == NO_RESULT:
        return None
    if code != OK:
        raise ValidationError(f"KRA {code}: {resp.get('resultMsg', '')}")
    found = ((resp.get("data") or {}).get("custList") or [None])[0]
    return found


def sync_all(company):
    """Everything a scheduled run does: pull, then send the queue."""
    out = {}
    for device in EtimsDevice.objects.filter(company=company, is_active=True):
        if not device.ready:
            continue
        for name, fn in (("codes", sync_codes), ("item_classes", sync_item_classes),
                         ("notices", sync_notices), ("purchases", fetch_purchases),
                         ("imports", fetch_imports)):
            try:
                out[f"{device.bhf_id}:{name}"] = fn(device)
            except ValidationError as exc:
                out[f"{device.bhf_id}:{name}"] = f"error: {exc.messages[0]}"
    out["queue"] = process(company)
    return out


# ─────────────────────────────────── reads ──────────────────────────────────────

def receipt_for(source_type, source_id, company=None):
    qs = EtimsReceipt.objects.filter(source_type=source_type, source_id=str(source_id))
    if company is not None:
        qs = qs.filter(company=company)
    return qs.select_related("device").first()


def status_for(source_type, source_id):
    """(receipt or None, the latest submission or None) for a document."""
    sub = EtimsSubmission.objects.filter(source_type=source_type, source_id=str(source_id),
                                         kind__in=SEQUENTIAL).order_by("-created_at").first()
    return receipt_for(source_type, source_id), sub


def queue_counts(company):
    qs = EtimsSubmission.objects.filter(company=company)
    return {s: qs.filter(status=s).count() for s, _ in EtimsSubmission.STATUSES}


def totals_today(company):
    start = timezone.make_aware(datetime.datetime.combine(timezone.localdate(), datetime.time.min))
    rec = EtimsReceipt.objects.filter(company=company, created_at__gte=start)
    return {"receipts": rec.filter(receipt_type="S").count(),
            "credit_notes": rec.filter(receipt_type="R").count()}
