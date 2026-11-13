"""Talking to the payment providers.

    M-Pesa   Safaricom Daraja: OAuth, Lipa na M-Pesa Online (STK push), STK
             query, C2B URL registration.
    Pesapal  API 3.0: RequestToken, RegisterIPN, SubmitOrderRequest (hosted
             checkout), GetTransactionStatus.
    DPO      3G Direct Pay API v6 (XML): createToken (hosted checkout),
             verifyToken.

Every call answers a plain dict. The SIMULATOR environment answers locally,
so the whole flow can be trained on and tested with no merchant account and
no network. Each driver's `status()` is what decides PAID: callbacks only
prompt us to ask.
"""
import base64
import datetime
import json
import xml.etree.ElementTree as ET
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.utils import timezone

TIMEOUT = 20


class GatewayError(Exception):
    pass


def _http(method, url, body=None, headers=None, raw=False):
    data = None
    headers = dict(headers or {})
    if body is not None:
        if raw:
            data = body.encode("utf-8") if isinstance(body, str) else body
        else:
            data = json.dumps(body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
    headers.setdefault("Accept", "application/json" if not raw else "application/xml")
    req = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(req, timeout=TIMEOUT) as resp:
            text = resp.read().decode("utf-8", "replace")
    except HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        if raw:
            return text
        try:
            return json.loads(text or "{}")
        except ValueError:
            raise GatewayError(f"HTTP {exc.code} from the provider: {text[:200]}")
    except (URLError, OSError) as exc:
        raise GatewayError(f"Could not reach the provider ({exc}).")
    if raw:
        return text
    try:
        return json.loads(text or "{}")
    except ValueError:
        raise GatewayError(f"The provider answered something that is not JSON: {text[:200]}")


def normalise_phone(phone):
    """07.. / 01.. / +2547.. / 2547.. -> 2547XXXXXXXX (what Daraja wants)."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    elif len(digits) == 9 and digits[0] in "17":
        digits = "254" + digits
    if not (len(digits) == 12 and digits.startswith("254") and digits[3] in "17"):
        raise GatewayError("Enter a Kenyan mobile number, e.g. 0712 345 678.")
    return digits


# ─────────────────────────────────── M-Pesa ───────────────────────────────────

class Mpesa:
    HOSTS = {"SANDBOX": "https://sandbox.safaricom.co.ke", "LIVE": "https://api.safaricom.co.ke"}

    def __init__(self, gateway):
        self.g = gateway
        self.host = self.HOSTS.get(gateway.environment, self.HOSTS["SANDBOX"])

    def token(self):
        basic = base64.b64encode(f"{self.g.consumer_key}:{self.g.consumer_secret}".encode()).decode()
        out = _http("GET", f"{self.host}/oauth/v1/generate?grant_type=client_credentials",
                    headers={"Authorization": f"Basic {basic}"})
        if not out.get("access_token"):
            raise GatewayError(out.get("errorMessage") or "Daraja refused the consumer key and secret.")
        return out["access_token"]

    def _password(self):
        stamp = timezone.localtime().strftime("%Y%m%d%H%M%S")
        raw = f"{self.g.shortcode}{self.g.passkey}{stamp}".encode()
        return base64.b64encode(raw).decode(), stamp

    def check(self):
        self.token()
        return "Daraja accepted the consumer key and secret."

    def start(self, req, callback_url, return_url=None):
        """Send the prompt to the customer's phone."""
        password, stamp = self._password()
        till = self.g.shortcode_type == "TILL"
        body = {
            "BusinessShortCode": self.g.shortcode, "Password": password, "Timestamp": stamp,
            "TransactionType": "CustomerBuyGoodsOnline" if till else "CustomerPayBillOnline",
            "Amount": int(Decimal(req.amount).to_integral_value()),
            "PartyA": req.phone, "PartyB": self.g.till_number if till and self.g.till_number else self.g.shortcode,
            "PhoneNumber": req.phone, "CallBackURL": callback_url,
            "AccountReference": req.reference[:12], "TransactionDesc": (req.description or "Payment")[:13],
        }
        out = _http("POST", f"{self.host}/mpesa/stkpush/v1/processrequest", body,
                    headers={"Authorization": f"Bearer {self.token()}"})
        if str(out.get("ResponseCode")) != "0":
            raise GatewayError(out.get("errorMessage") or out.get("ResponseDescription") or "M-Pesa refused the request.")
        return {"provider_ref": out["CheckoutRequestID"], "raw": out}

    def status(self, req):
        password, stamp = self._password()
        out = _http("POST", f"{self.host}/mpesa/stkpushquery/v1/query",
                    {"BusinessShortCode": self.g.shortcode, "Password": password, "Timestamp": stamp,
                     "CheckoutRequestID": req.provider_ref},
                    headers={"Authorization": f"Bearer {self.token()}"})
        code = str(out.get("ResultCode", ""))
        if code == "0":
            return {"status": "PAID", "raw": out}
        if code == "1032":
            return {"status": "CANCELLED", "note": "The customer cancelled the prompt.", "raw": out}
        if code in ("1037", "1019"):
            return {"status": "EXPIRED", "note": "The prompt timed out on the phone.", "raw": out}
        if code:
            return {"status": "FAILED", "note": out.get("ResultDesc", ""), "raw": out}
        return {"status": "PENDING", "raw": out}      # still being processed

    def register_c2b(self, confirm_url, validate_url):
        out = _http("POST", f"{self.host}/mpesa/c2b/v1/registerurl",
                    {"ShortCode": self.g.till_number if self.g.shortcode_type == "TILL" and self.g.till_number else self.g.shortcode,
                     "ResponseType": "Completed", "ConfirmationURL": confirm_url, "ValidationURL": validate_url},
                    headers={"Authorization": f"Bearer {self.token()}"})
        if str(out.get("ResponseCode", out.get("ResponseDescription", ""))) not in ("0", "00000000") and \
                "success" not in str(out.get("ResponseDescription", "")).lower():
            raise GatewayError(out.get("errorMessage") or out.get("ResponseDescription") or "Daraja refused the URLs.")
        return out


def stk_callback_items(body):
    """The useful parts of an STK callback body."""
    cb = (body or {}).get("Body", {}).get("stkCallback", {})
    items = {i.get("Name"): i.get("Value") for i in cb.get("CallbackMetadata", {}).get("Item", [])}
    return {"checkout_id": cb.get("CheckoutRequestID", ""), "result": str(cb.get("ResultCode", "")),
            "desc": cb.get("ResultDesc", ""), "receipt": str(items.get("MpesaReceiptNumber", "") or ""),
            "amount": items.get("Amount"), "phone": str(items.get("PhoneNumber", "") or "")}


# ─────────────────────────────────── Pesapal ──────────────────────────────────

class Pesapal:
    HOSTS = {"SANDBOX": "https://cybqa.pesapal.com/pesapalv3", "LIVE": "https://pay.pesapal.com/v3"}

    def __init__(self, gateway):
        self.g = gateway
        self.host = self.HOSTS.get(gateway.environment, self.HOSTS["SANDBOX"])

    def token(self):
        out = _http("POST", f"{self.host}/api/Auth/RequestToken",
                    {"consumer_key": self.g.consumer_key, "consumer_secret": self.g.consumer_secret})
        if not out.get("token"):
            err = out.get("error") or {}
            raise GatewayError((err.get("message") if isinstance(err, dict) else str(err))
                               or "Pesapal refused the key and secret.")
        return out["token"]

    def _auth(self):
        return {"Authorization": f"Bearer {self.token()}"}

    def check(self):
        self.token()
        return "Pesapal accepted the consumer key and secret."

    def ensure_ipn(self, ipn_url):
        if self.g.ipn_id:
            return self.g.ipn_id
        out = _http("POST", f"{self.host}/api/URLSetup/RegisterIPN",
                    {"url": ipn_url, "ipn_notification_type": "GET"}, headers=self._auth())
        if not out.get("ipn_id"):
            raise GatewayError("Pesapal did not register the notification address.")
        self.g.ipn_id = out["ipn_id"]
        self.g.save(update_fields=["ipn_id", "updated_at"])
        return self.g.ipn_id

    def start(self, req, callback_url, return_url=None, ipn_url=None):
        notification = self.ensure_ipn(ipn_url)
        body = {"id": str(req.pk), "currency": req.currency, "amount": float(req.amount),
                "description": (req.description or req.reference)[:100], "callback_url": return_url,
                "notification_id": notification,
                "billing_address": {"email_address": req.email, "phone_number": req.phone,
                                    "first_name": (req.payer or "Customer")[:50]}}
        out = _http("POST", f"{self.host}/api/Transactions/SubmitOrderRequest", body, headers=self._auth())
        if not out.get("redirect_url"):
            err = out.get("error") or {}
            raise GatewayError((err.get("message") if isinstance(err, dict) else "") or "Pesapal refused the order.")
        return {"provider_ref": out.get("order_tracking_id", ""), "checkout_url": out["redirect_url"], "raw": out}

    def status(self, req):
        out = _http("GET", f"{self.host}/api/Transactions/GetTransactionStatus?orderTrackingId={req.provider_ref}",
                    headers=self._auth())
        code = out.get("status_code")
        if code == 1:
            return {"status": "PAID", "receipt": out.get("confirmation_code", ""),
                    "amount": out.get("amount"), "raw": out}
        if code == 2:
            return {"status": "FAILED", "note": out.get("payment_status_description", ""), "raw": out}
        if code == 3:
            return {"status": "FAILED", "note": "Reversed", "raw": out}
        return {"status": "PENDING", "raw": out}


# ───────────────────────────────────── DPO ────────────────────────────────────

class Dpo:
    API = "https://secure.3gdirectpay.com/API/v6/"
    PAY = "https://secure.3gdirectpay.com/payv2.php?ID="

    def __init__(self, gateway):
        self.g = gateway

    def _call(self, inner):
        xml = f'<?xml version="1.0" encoding="utf-8"?><API3G><CompanyToken>{_x(self.g.company_token)}</CompanyToken>{inner}</API3G>'
        text = _http("POST", self.API, xml, headers={"Content-Type": "application/xml"}, raw=True)
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            raise GatewayError(f"DPO answered something that is not XML: {text[:200]}")
        return {child.tag: (child.text or "") for child in root}

    def check(self):
        # verifyToken on a dummy token: a wrong company token says so (code 801/802).
        out = self._call("<Request>verifyToken</Request><TransactionToken>00000000-0000-0000-0000-000000000000</TransactionToken>")
        if out.get("Result") in ("801", "802", "803", "804"):
            raise GatewayError(out.get("ResultExplanation") or "DPO refused the company token.")
        return "DPO accepted the company token."

    def start(self, req, callback_url, return_url=None):
        when = timezone.localtime().strftime("%Y/%m/%d %H:%M")
        out = self._call(
            "<Request>createToken</Request><Transaction>"
            f"<PaymentAmount>{req.amount}</PaymentAmount><PaymentCurrency>{_x(req.currency)}</PaymentCurrency>"
            f"<CompanyRef>{_x(req.reference)}</CompanyRef><RedirectURL>{_x(return_url)}</RedirectURL>"
            f"<BackURL>{_x(return_url)}</BackURL><CompanyRefUnique>0</CompanyRefUnique><PTL>24</PTL>"
            f"<customerEmail>{_x(req.email)}</customerEmail><customerPhone>{_x(req.phone)}</customerPhone>"
            "</Transaction><Services><Service>"
            f"<ServiceType>{_x(self.g.service_type)}</ServiceType><ServiceDescription>{_x(req.description or req.reference)}</ServiceDescription>"
            f"<ServiceDate>{when}</ServiceDate></Service></Services>")
        if out.get("Result") != "000" or not out.get("TransToken"):
            raise GatewayError(out.get("ResultExplanation") or "DPO refused the payment.")
        return {"provider_ref": out["TransToken"], "checkout_url": self.PAY + out["TransToken"], "raw": out}

    def status(self, req):
        out = self._call(f"<Request>verifyToken</Request><TransactionToken>{_x(req.provider_ref)}</TransactionToken>")
        code = out.get("Result", "")
        if code == "000":
            return {"status": "PAID", "receipt": out.get("TransactionApproval", "") or out.get("TransactionRef", ""),
                    "amount": out.get("TransactionAmount"), "raw": out}
        if code in ("901", "903"):
            return {"status": "FAILED", "note": out.get("ResultExplanation", ""), "raw": out}
        if code == "904":
            return {"status": "CANCELLED", "note": out.get("ResultExplanation", ""), "raw": out}
        return {"status": "PENDING", "raw": out}


def _x(value):
    return (str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# ───────────────────────────────── simulator ──────────────────────────────────

class Simulator:
    """No network: a prompt or checkout that a person (or a test) completes
    from the simulator page. status() reads what was decided there."""

    def __init__(self, gateway):
        self.g = gateway

    def check(self):
        return "Simulator: no money moves. Switch to Sandbox or Live with your merchant keys."

    def start(self, req, callback_url, return_url=None, **_):
        from django.urls import reverse
        ref = f"SIM-{req.pk.hex[:12].upper()}"
        url = "" if self.g.provider == "MPESA" else reverse("payments:simulate", args=[req.callback_key])
        return {"provider_ref": ref, "checkout_url": url, "raw": {"simulated": True}}

    def status(self, req):
        sim = (req.raw or {}).get("simulated_result")
        if sim == "PAID":
            stamp = datetime.datetime.now().strftime("%H%M%S")
            return {"status": "PAID", "receipt": (req.raw or {}).get("simulated_receipt") or f"SIM{stamp}",
                    "raw": {"simulated": True}}
        if sim in ("FAILED", "CANCELLED"):
            return {"status": sim, "note": "Simulated", "raw": {"simulated": True}}
        return {"status": "PENDING", "raw": {"simulated": True}}

    def register_c2b(self, confirm_url, validate_url):
        return {"simulated": True}


def driver(gateway):
    if gateway.environment == "SIMULATOR":
        return Simulator(gateway)
    return {"MPESA": Mpesa, "PESAPAL": Pesapal, "DPO": Dpo}[gateway.provider](gateway)
