"""Transports to KRA eTIMS. Every backend takes the canonical OSCU payload and
returns KRA's envelope {resultCd, resultMsg, resultDt, data}.

Sources: KRA OSCU Specification v2.0 (Apr 2023), VSCU Specification v2.0,
TIS Technical Specification v2.0 (kra.go.ke/images/publications/...).
"""
import base64
import hashlib
import json
import random
import string
import urllib.error
import urllib.request

from django.utils import timezone

# operation -> (OSCU path, VSCU path)
OPERATIONS = {
    "init": ("/selectInitOsdcInfo", "/initializer/selectInitInfo"),
    "codes": ("/selectCodeList", "/code/selectCodes"),
    "item_classes": ("/selectItemClsList", "/itemClass/selectItemsClass"),
    "customer_lookup": ("/selectCustomer", "/customers/selectCustomer"),
    "branches": ("/selectBhfList", "/branches/selectBranches"),
    "notices": ("/selectNoticeList", "/notices/selectNotices"),
    "customer_save": ("/saveBhfCustomer", "/branches/saveBrancheCustomers"),
    "user_save": ("/saveBhfUser", "/branches/saveBrancheUsers"),
    "item_save": ("/saveItem", "/items/saveItems"),
    "item_list": ("/selectItemList", "/items/selectItems"),
    "import_list": ("/selectImportItemList", "/imports/selectImportItems"),
    "import_update": ("/updateImportItem", "/imports/updateImportItems"),
    "sale_save": ("/saveTrnsSalesOsdc", "/trnsSales/saveSales"),
    "purchase_list": ("/selectTrnsPurchaseSalesList", "/trnsPurchase/selectTrnsPurchaseSales"),
    "purchase_save": ("/insertTrnsPurchase", "/trnsPurchase/savePurchases"),
    "stock_io": ("/insertStockIO", "/stock/saveStockItems"),
    "stock_master": ("/saveStockMaster", "/stockMaster/saveStockMaster"),
    "stock_moves": ("/selectStockMoveList", "/stock/selectStockItems"),
}

DEFAULT_URLS = {
    ("OSCU", "SANDBOX"): "https://etims-api-sbx.kra.go.ke/etims-api",
    ("OSCU", "PRODUCTION"): "https://etims-api.kra.go.ke/etims-api",
    ("VSCU", "SANDBOX"): "http://localhost:8088",
    ("VSCU", "PRODUCTION"): "http://localhost:8088",
}
TOKEN_URLS = {"SANDBOX": "https://sbx.kra.go.ke/v1/token/generate?grant_type=client_credentials",
              "PRODUCTION": "https://api.kra.go.ke/v1/token/generate?grant_type=client_credentials"}
RECEIPT_URLS = {
    "SANDBOX": "https://etims-sbx.kra.go.ke/common/link/etims/receipt/indexEtimsReceiptData?Data=",
    "PRODUCTION": "https://etims.kra.go.ke/common/link/etims/receipt/indexEtimsReceiptData?Data=",
}

# Codes that mean "KRA already has this": a resend after a lost reply.
DUPLICATE = "994"
NO_RESULT = "001"
OK = "000"


class EtimsTransportError(Exception):
    """KRA could not be reached (network, timeout, HTTP error): try again later."""


def now_stamp():
    return timezone.localtime().strftime("%Y%m%d%H%M%S")


def envelope(code, message, data=None):
    return {"resultCd": code, "resultMsg": message, "resultDt": now_stamp(), "data": data}


class Backend:
    name = "base"

    def __init__(self, settings, device=None):
        self.settings = settings
        self.device = device

    def call(self, operation, payload):  # pragma: no cover - interface
        raise NotImplementedError

    def receipt_url(self, tin, bhf_id, signature):
        return RECEIPT_URLS[self.settings.environment] + f"{tin}{bhf_id}{signature}"


class HttpBackend(Backend):
    """OSCU (KRA hosted) and VSCU (KRA JAR on your server) share the payloads."""

    def __init__(self, settings, device=None, kind="OSCU"):
        super().__init__(settings, device)
        self.kind = kind
        self.name = kind
        self.base = (settings.base_url or DEFAULT_URLS[(kind, settings.environment)]).rstrip("/")
        self._token = None

    def path(self, operation):
        oscu, vscu = OPERATIONS[operation]
        return vscu if self.kind == "VSCU" else oscu

    def token(self):
        """GavaConnect OAuth token, only when keys are configured (KRA has been
        reported to front OSCU with it; uncertain, so optional)."""
        s = self.settings
        if not (s.oauth_consumer_key and s.oauth_consumer_secret):
            return None
        if self._token:
            return self._token
        basic = base64.b64encode(f"{s.oauth_consumer_key}:{s.oauth_consumer_secret}".encode()).decode()
        req = urllib.request.Request(TOKEN_URLS[s.environment], headers={"Authorization": f"Basic {basic}"})
        try:
            with urllib.request.urlopen(req, timeout=s.timeout_seconds) as resp:
                self._token = json.loads(resp.read().decode() or "{}").get("access_token")
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise EtimsTransportError(f"Could not get a KRA access token: {exc}") from exc
        return self._token

    def headers(self, operation):
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if operation != "init" and self.device is not None:
            h.update({"tin": self.settings.kra_pin, "bhfId": self.device.bhf_id,
                      "cmcKey": self.device.cmc_key})
        tok = self.token()
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        return h

    def call(self, operation, payload):
        body = json.dumps(payload, default=str).encode()
        req = urllib.request.Request(self.base + self.path(operation), data=body, method="POST",
                                     headers=self.headers(operation))
        try:
            with urllib.request.urlopen(req, timeout=self.settings.timeout_seconds) as resp:
                text = resp.read().decode("utf-8") or "{}"
        except urllib.error.HTTPError as exc:
            raise EtimsTransportError(f"KRA answered HTTP {exc.code} for {operation}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise EtimsTransportError(f"KRA could not be reached for {operation}: {exc}") from exc
        try:
            return json.loads(text)
        except ValueError as exc:
            raise EtimsTransportError(f"KRA sent something that is not JSON for {operation}") from exc


class PergamonBackend(HttpBackend):
    """Pergamon Easy Tax (KRA-approved, VSCU based). Their API is not public:
    this posts the canonical KRA payload to `{endpoint}/{operation}` with the
    key they issue as a Bearer token. Adjust `path`/`headers` to their spec
    when Pergamon provides it; everything else (payloads, queue, receipts) is
    unchanged."""

    def __init__(self, settings, device=None):
        Backend.__init__(self, settings, device)
        self.kind = "PERGAMON"
        self.name = "PERGAMON"
        self.base = (settings.pergamon_endpoint or "").rstrip("/")
        self._token = None

    def path(self, operation):
        return "/" + operation

    def headers(self, operation):
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.settings.pergamon_api_key:
            h["Authorization"] = f"Bearer {self.settings.pergamon_api_key}"
        if self.device is not None:
            h.update({"tin": self.settings.kra_pin, "bhfId": self.device.bhf_id})
        return h

    def call(self, operation, payload):
        if not self.base:
            raise EtimsTransportError("Pergamon's fiscal endpoint is not set in eTIMS settings.")
        return super().call(operation, payload)


class SimulatorBackend(Backend):
    """An in-process KRA that applies KRA's rules, for testing without
    credentials: sequential invoice and stock numbers per device, 994 on a
    duplicate, tax buckets that must add up, signed receipts."""
    name = "SIMULATOR"

    TAX = {"A": "0", "B": "16", "C": "0", "D": "0", "E": "8"}

    def call(self, operation, payload):
        handler = getattr(self, "op_" + operation, None)
        if handler is None:
            return envelope("912", f"Unknown operation {operation}")
        return handler(payload)

    # — helpers —
    def _need(self, payload, *fields):
        missing = [f for f in fields if payload.get(f) in (None, "")]
        return envelope("910", "Parameter error: " + ", ".join(missing) + " required") if missing else None

    def _sign(self, *parts):
        digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest().upper()
        return digest

    # — operations —
    def op_init(self, p):
        bad = self._need(p, "tin", "bhfId", "dvcSrlNo")
        if bad:
            return bad
        key = self._sign("cmc", p["tin"], p["bhfId"], p["dvcSrlNo"])[:40]
        return envelope(OK, "It is succeeded", {"info": {
            "tin": p["tin"], "taxprNm": "SIMULATED TAXPAYER", "bhfId": p["bhfId"],
            "bhfNm": "Head Office" if p["bhfId"] == "00" else f"Branch {p['bhfId']}",
            "hqYn": "Y" if p["bhfId"] == "00" else "N",
            "dvcId": "99" + p["bhfId"] + "0001",
            "sdcId": f"KRACU0{p['bhfId']}00000{random.randint(10, 99)}",
            "mrcNo": f"WIS0{p['bhfId']}00000{random.randint(10, 99)}", "cmcKey": key}})

    def op_codes(self, p):
        cls = [
            {"cdCls": "04", "cdClsNm": "Taxation Type", "dtlList": [
                {"cd": k, "cdNm": n, "userDfnCd1": self.TAX[k], "srtOrd": i, "useYn": "Y"}
                for i, (k, n) in enumerate([("A", "A-Exempt"), ("B", "B-16.00%"), ("C", "C-0%"),
                                            ("D", "D-Non-VAT"), ("E", "E-8%")])]},
            {"cdCls": "07", "cdClsNm": "Payment Type", "dtlList": [
                {"cd": c, "cdNm": n, "srtOrd": i, "useYn": "Y"} for i, (c, n) in enumerate([
                    ("01", "CASH"), ("02", "CREDIT"), ("03", "CASH/CREDIT"), ("04", "BANK CHECK"),
                    ("05", "DEBIT&CREDIT CARD"), ("06", "MOBILE MONEY"), ("07", "OTHER")])]},
            {"cdCls": "10", "cdClsNm": "Quantity Unit", "dtlList": [
                {"cd": c, "cdNm": n, "srtOrd": i, "useYn": "Y"} for i, (c, n) in enumerate([
                    ("U", "Pieces/item"), ("KG", "Kilo-Gramme"), ("L", "Litre"), ("MTR", "Metre"),
                    ("BX", "Box"), ("PA", "Packet"), ("DZ", "Dozen")])]},
            {"cdCls": "17", "cdClsNm": "Packing Unit", "dtlList": [
                {"cd": c, "cdNm": n, "srtOrd": i, "useYn": "Y"} for i, (c, n) in enumerate([
                    ("NT", "Net"), ("BG", "Bag"), ("BX", "Box"), ("CT", "Carton"), ("CA", "Can")])]},
            {"cdCls": "32", "cdClsNm": "Refund Reason", "dtlList": [
                {"cd": c, "cdNm": n, "srtOrd": i, "useYn": "Y"} for i, (c, n) in enumerate([
                    ("01", "Missing Quantity"), ("02", "Missing Data"), ("03", "Damaged"),
                    ("04", "Wasted"), ("05", "Raw Material Shortage"), ("06", "Refund")])]},
        ]
        return envelope(OK, "It is succeeded", {"clsList": cls})

    def op_item_classes(self, p):
        return envelope(OK, "It is succeeded", {"itemClsList": [
            {"itemClsCd": "5059690800", "itemClsNm": "General goods", "itemClsLvl": 4,
             "taxTyCd": "B", "useYn": "Y"},
            {"itemClsCd": "5020230500", "itemClsNm": "Beverages", "itemClsLvl": 4,
             "taxTyCd": "B", "useYn": "Y"},
            {"itemClsCd": "8111180000", "itemClsNm": "Services", "itemClsLvl": 4,
             "taxTyCd": "B", "useYn": "Y"}]})

    def op_customer_lookup(self, p):
        tin = (p.get("custmTin") or "").upper()
        if len(tin) == 11 and tin[0].isalpha() and tin[1:10].isdigit() and tin[10].isalpha():
            return envelope(OK, "It is succeeded", {"custList": [
                {"tin": tin, "taxprNm": f"TAXPAYER {tin}", "taxprSttsCd": "A"}]})
        return envelope(NO_RESULT, "There is no search result")

    def op_branches(self, p):
        return envelope(OK, "It is succeeded", {"bhfList": [
            {"tin": p.get("tin"), "bhfId": "00", "bhfNm": "Head Office", "bhfSttsCd": "01", "hqYn": "Y"}]})

    def op_notices(self, p):
        return envelope(OK, "It is succeeded", {"noticeList": [
            {"noticeNo": 1, "title": "Simulated notice", "cont": "eTIMS sandbox maintenance window.",
             "dtlUrl": "https://etims-sbx.kra.go.ke", "regrNm": "KRA", "regDt": now_stamp()}]})

    def op_customer_save(self, p):
        return self._need(p, "custNo", "custTin", "custNm") or envelope(OK, "It is succeeded")

    def op_user_save(self, p):
        return self._need(p, "userId", "userNm") or envelope(OK, "It is succeeded")

    def op_item_save(self, p):
        bad = self._need(p, "itemCd", "itemClsCd", "itemTyCd", "itemNm", "pkgUnitCd", "qtyUnitCd",
                         "taxTyCd")
        if bad:
            return bad
        if p["taxTyCd"] not in self.TAX:
            return envelope("910", f"Unknown tax type {p['taxTyCd']}")
        return envelope(OK, "It is succeeded")

    def op_item_list(self, p):
        return envelope(NO_RESULT, "There is no search result")

    def op_import_list(self, p):
        return envelope(OK, "It is succeeded", {"itemList": [{
            "taskCd": "SIM2026001", "dclDe": timezone.localdate().strftime("%Y%m%d"), "itemSeq": 1,
            "dclNo": "C1234-2026", "hsCd": "2203000000", "itemNm": "Imported lager 500ml",
            "imptItemsttsCd": "2", "orgnNatCd": "UG", "exptNatCd": "UG", "pkg": 10, "pkgUnitCd": "CT",
            "qty": 240, "qtyUnitCd": "U", "totWt": 130, "netWt": 120, "spplrNm": "Kampala Brewers",
            "agntNm": "Clearing Agent Ltd", "invcFcurAmt": 1200, "invcFcurCd": "USD",
            "invcFcurExcrt": 129.5}]})

    def op_import_update(self, p):
        bad = self._need(p, "taskCd", "dclDe", "itemSeq", "imptItemSttsCd")
        if bad:
            return bad
        if p["imptItemSttsCd"] == "3" and not p.get("itemCd"):
            return envelope("910", "Approving an import needs the item it maps to (itemCd)")
        return envelope(OK, "It is succeeded")

    def _expected(self, kind):
        from .models import EtimsReceipt, EtimsSubmission
        if self.device is None:
            return None
        if kind == "sale":
            last = EtimsReceipt.objects.filter(device=self.device).order_by("-invoice_no").first()
            return (last.invoice_no + 1) if last else 1, (
                set(EtimsReceipt.objects.filter(device=self.device).values_list("invoice_no", flat=True)))
        done = EtimsSubmission.objects.filter(device=self.device, kind="STOCK_IO", status="SENT")
        seqs = set(done.values_list("sequence", flat=True))
        return (max(seqs) + 1) if seqs else 1, seqs

    def op_sale_save(self, p):
        bad = self._need(p, "invcNo", "salesTyCd", "rcptTyCd", "pmtTyCd", "salesSttsCd",
                         "cfmDt", "salesDt", "totItemCnt", "itemList")
        if bad:
            return bad
        exp = self._expected("sale")
        if exp:
            expected, seen = exp
            if int(p["invcNo"]) in seen:
                return envelope(DUPLICATE, "Overlapped data: this invoice number was already sent")
            if int(p["invcNo"]) != expected:
                return envelope("910", f"invcNo {p['invcNo']} out of sequence; expected {expected}")
        if p["rcptTyCd"] == "R" and not int(p.get("orgInvcNo") or 0):
            return envelope("910", "A credit note needs the original invoice number (orgInvcNo)")
        totals = {k: 0.0 for k in "ABCDE"}
        tax = {k: 0.0 for k in "ABCDE"}
        for i, line in enumerate(p["itemList"], 1):
            for f in ("itemCd", "itemClsCd", "itemNm", "qty", "prc", "splyAmt", "taxTyCd",
                      "taxblAmt", "taxAmt", "totAmt"):
                if line.get(f) in (None, ""):
                    return envelope("910", f"Line {i}: {f} required")
            if line["taxTyCd"] not in totals:
                return envelope("910", f"Line {i}: unknown tax type {line['taxTyCd']}")
            totals[line["taxTyCd"]] += float(line["taxblAmt"])
            tax[line["taxTyCd"]] += float(line["taxAmt"])
        for k in "ABCDE":
            if abs(totals[k] - float(p.get(f"taxblAmt{k}") or 0)) > 0.011:
                return envelope("910", f"taxblAmt{k} does not equal the sum of its lines")
            if abs(tax[k] - float(p.get(f"taxAmt{k}") or 0)) > 0.011:
                return envelope("910", f"taxAmt{k} does not equal the sum of its lines")
        if abs(sum(totals.values()) - float(p.get("totAmt") or 0)) > 0.011:
            return envelope("910", "totAmt does not equal the sum of the tax buckets")
        n = int(p["invcNo"])
        sign = self._sign("sale", p.get("tin"), p.get("bhfId"), n, p["totAmt"])
        return envelope(OK, "It is succeeded", {
            "curRcptNo": n, "totRcptNo": n, "intrlData": sign[:26], "rcptSign": sign[26:42],
            "sdcDateTime": now_stamp()})

    def op_purchase_list(self, p):
        if (p.get("lastReqDt") or "").startswith("2020"):
            return envelope(OK, "It is succeeded", {"saleList": [{
                "spplrTin": "P051234567X", "spplrNm": "Simulated Supplier Ltd", "spplrBhfId": "00",
                "spplrInvcNo": 501, "rcptTyCd": "S", "pmtTyCd": "01", "cfmDt": now_stamp(),
                "salesDt": timezone.localdate().strftime("%Y%m%d"), "totItemCnt": 1,
                "taxblAmtB": 1160, "taxAmtB": 160, "totTaxblAmt": 1160, "totTaxAmt": 160,
                "totAmt": 1160, "itemList": [{
                    "itemSeq": 1, "itemCd": "KE2NTU0000099", "itemClsCd": "5059690800",
                    "itemNm": "Sugar 1kg", "pkgUnitCd": "NT", "pkg": 10, "qtyUnitCd": "U", "qty": 10,
                    "prc": 116, "splyAmt": 1160, "dcRt": 0, "dcAmt": 0, "taxTyCd": "B",
                    "taxblAmt": 1160, "taxAmt": 160, "totAmt": 1160}]}]})
        return envelope(NO_RESULT, "There is no search result")

    def op_purchase_save(self, p):
        return self._need(p, "invcNo", "spplrTin", "pchsTyCd", "rcptTyCd", "pmtTyCd", "pchsSttsCd",
                          "pchsDt", "itemList") or envelope(OK, "It is succeeded")

    def op_stock_io(self, p):
        bad = self._need(p, "sarNo", "sarTyCd", "ocrnDt", "itemList")
        if bad:
            return bad
        exp = self._expected("stock")
        if exp:
            expected, seen = exp
            if int(p["sarNo"]) in seen:
                return envelope(DUPLICATE, "Overlapped data: this stock number was already sent")
            if int(p["sarNo"]) != expected:
                return envelope("910", f"sarNo {p['sarNo']} out of sequence; expected {expected}")
        return envelope(OK, "It is succeeded")

    def op_stock_master(self, p):
        return self._need(p, "itemCd", "rsdQty") or envelope(OK, "It is succeeded")

    def op_stock_moves(self, p):
        return envelope(NO_RESULT, "There is no search result")


def backend_for(settings, device=None):
    if settings.backend == "SIMULATOR":
        return SimulatorBackend(settings, device)
    if settings.backend == "PERGAMON":
        return PergamonBackend(settings, device)
    return HttpBackend(settings, device, kind=settings.backend)


def random_serial(prefix="MERP"):
    return prefix + "".join(random.choice(string.digits) for _ in range(10))
