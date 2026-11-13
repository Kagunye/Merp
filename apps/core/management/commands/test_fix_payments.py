"""Checks for the payment gateways (apps/payments).

    python manage.py test_fix_payments      # rolls back

    1. settings        -> secrets never sent back and kept when left blank;
                          Sandbox/Live need the keys; Live callbacks need https
    2. M-Pesa invoice  -> a prompt to a normalised phone; the receipt is booked
                          against the invoice only when the gateway confirms;
                          a callback's word alone books nothing; two
                          confirmations book once; a wrong key is refused
    3. till            -> the till sends a prompt and sees it paid
    4. paybill (C2B)   -> matched by the invoice number typed as the account,
                          booked once per M-Pesa code, otherwise left to match
    5. Pesapal and DPO -> a checkout link; paying it books the receipt
    6. the real APIs   -> Daraja, Pesapal and DPO answers are read correctly
                          (requests and replies checked without the network)
    7. roles           -> who may set up, ask, and read
"""
import datetime
import json
from decimal import Decimal
from unittest import mock

from django.core.management.base import BaseCommand
from django.db import transaction
from django.test import Client
from django.test.utils import setup_test_environment, teardown_test_environment
from django.urls import reverse
from django.utils import timezone

D = Decimal


class Command(BaseCommand):
    help = "Checks for M-Pesa, Pesapal and DPO payments."

    def handle(self, *args, **opts):
        self.passed, self.failures = 0, []
        setup_test_environment()
        try:
            with transaction.atomic():
                self.run()
                transaction.set_rollback(True)
        finally:
            teardown_test_environment()
        self.stdout.write("")
        self.stdout.write(f"Checks passed: {self.passed}")
        if self.failures:
            self.stdout.write(self.style.ERROR(f"Failures: {len(self.failures)}"))
            for f in self.failures:
                self.stderr.write(self.style.ERROR(f"  FAIL {f}"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("Failures: 0"))

    def section(self, t):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"-- {t}"))

    def expect(self, label, cond, detail=""):
        if cond:
            self.passed += 1
            self.stdout.write(f"  ok   {label}" + (f"  ({detail})" if detail else ""))
            return True
        self.failures.append(f"{label} — {detail}" if detail else label)
        self.stdout.write(self.style.ERROR(f"  FAIL {label} {detail}"))
        return False

    def client_for(self, u):
        c = Client(raise_request_exception=False)
        c.force_login(u, backend="django.contrib.auth.backends.ModelBackend")
        return c

    def user(self, name, role):
        from apps.accounts.models import User
        from apps.organizations.models import CompanyMembership
        u = User.objects.create(username=f"{name}.{self.stamp}", email=f"{name}.{self.stamp}@fixpay.local")
        u.set_password("fix-pay-pw")
        u.save()
        CompanyMembership.objects.create(company=self.co, user=u, role=role, branch=self.hq, is_default=True)
        return u

    def invoice(self, amount, ref):
        from apps.sales.models import Invoice, InvoiceLine
        today = timezone.localdate()
        inv = Invoice.objects.create(company=self.co, reference=ref, customer=self.cust, invoice_date=today,
                                     due_date=today + datetime.timedelta(days=30))
        InvoiceLine.objects.create(invoice=inv, description="Installation", quantity=D("1"), unit_price=D(amount))
        from apps.sales.views import _recalculate_invoice_totals
        _recalculate_invoice_totals(inv)
        r = self.ac.post(reverse("sales:invoice_post", args=[inv.pk]), {})
        inv.refresh_from_db()
        return inv

    def run(self):
        from apps.accounts.models import User
        from apps.crm.models import Customer
        from apps.organizations.models import Company
        from apps.payments import backends, services
        from apps.payments.forms import GatewayForm
        from apps.payments.models import PaybillReceipt, PaymentGateway, PaymentRequest
        from apps.pos import services as pos
        from apps.sales.models import CustomerPayment

        self.stamp = datetime.datetime.now().strftime("%H%M%S%f")
        admin = User.objects.create(username=f"pay.admin.{self.stamp}", email=f"pay.admin.{self.stamp}@fixpay.local")
        admin.set_password("fix-pay-pw")
        admin.save()
        self.ac = self.client_for(admin)
        name = f"Fix Pay {self.stamp}"
        self.ac.post(reverse("organizations:company_create"), {
            "name": name, "legal_name": name, "registration_number": f"PVT-{self.stamp}", "tax_id": "",
            "vat_number": "", "currency": "KES", "timezone": "Africa/Nairobi", "fiscal_year_start_month": "1",
            "industry": "Trading", "phone": "+254 700 000 007", "email": f"i@{self.stamp}.fixpay.local",
            "website": "", "address_line1": "7 Moi Avenue", "address_line2": "", "city": "Nairobi",
            "state_province": "Nairobi", "postal_code": "00100", "country": "Kenya"})
        self.co = Company.objects.get(name=name)
        self.hq = self.co.branches.get(is_headquarters=True)
        self.cust = Customer.objects.create(company=self.co, name="Wanjiku Stores", email="pay@wanjiku.example")
        accountant = self.user("pay.acct", "ACCOUNTANT")
        viewer = self.user("pay.viewer", "VIEWER")
        cashier = self.user("pay.cashier", "POS_CASHIER")

        # ── 1 ──
        self.section("1. settings")
        r = self.ac.post(reverse("payments:gateway_edit", args=["mpesa"]), {
            "is_active": "on", "environment": "SANDBOX", "consumer_key": "ck", "consumer_secret": "",
            "shortcode_type": "PAYBILL", "shortcode": "174379", "till_number": "", "passkey": "", "callback_base": ""})
        self.expect("Sandbox without the secret and passkey is refused", r.status_code == 200 and b"Needed to switch" in r.content)
        r = self.ac.post(reverse("payments:gateway_edit", args=["mpesa"]), {
            "is_active": "on", "environment": "SANDBOX", "consumer_key": "ck", "consumer_secret": "s3cret-value",
            "shortcode_type": "PAYBILL", "shortcode": "174379", "till_number": "", "passkey": "pk-value", "callback_base": ""})
        gw = PaymentGateway.objects.get(company=self.co, provider="MPESA")
        self.expect("with them it saves", r.status_code == 302 and gw.consumer_secret == "s3cret-value")
        page = self.ac.get(reverse("payments:gateway_edit", args=["mpesa"])).content
        self.expect("the secret and passkey are never sent back to the browser",
                    b"s3cret-value" not in page and b"pk-value" not in page)
        self.ac.post(reverse("payments:gateway_edit", args=["mpesa"]), {
            "is_active": "on", "environment": "SANDBOX", "consumer_key": "ck2", "consumer_secret": "",
            "shortcode_type": "PAYBILL", "shortcode": "174379", "till_number": "", "passkey": "", "callback_base": ""})
        gw.refresh_from_db()
        self.expect("left blank, the stored secret and passkey are kept",
                    gw.consumer_key == "ck2" and gw.consumer_secret == "s3cret-value" and gw.passkey == "pk-value")
        f = GatewayForm(data={"is_active": "on", "environment": "LIVE", "consumer_key": "a", "consumer_secret": "b",
                              "shortcode_type": "PAYBILL", "shortcode": "1", "passkey": "c",
                              "callback_base": "http://erp.example.co.ke"}, instance=gw, company=self.co)
        self.expect("Live callbacks must be https", not f.is_valid() and "callback_base" in f.errors)
        f = GatewayForm(data={"is_active": "on", "environment": "SANDBOX", "consumer_key": "a", "consumer_secret": "b",
                              "shortcode_type": "TILL", "shortcode": "1", "passkey": "c", "till_number": ""},
                        instance=gw, company=self.co)
        self.expect("a Buy Goods gateway needs its till number", not f.is_valid() and "till_number" in f.errors)
        for raw, want in (("0712 345 678", "254712345678"), ("+254 112 345678", "254112345678"), ("712345678", "254712345678")):
            self.expect(f"phone {raw!r} becomes {want}", backends.normalise_phone(raw) == want)
        try:
            backends.normalise_phone("12345")
            bad = False
        except backends.GatewayError:
            bad = True
        self.expect("a number that is not a Kenyan mobile is refused", bad)

        # simulator from here on
        gw.environment = "SIMULATOR"
        gw.save()
        for prov in ("PESAPAL", "DPO"):
            PaymentGateway.objects.create(company=self.co, provider=prov, is_active=True, environment="SIMULATOR")

        # ── 2 ──
        self.section("2. M-Pesa for an invoice")
        inv = self.invoice("5000", f"INV-P{self.stamp[-5:]}")
        self.expect("a posted invoice is owed 5,000", inv.status in ("SENT", "OVERDUE") and inv.outstanding_amount == D("5000.00"),
                    f"{inv.status} {inv.outstanding_amount}")
        acc = self.client_for(accountant)
        r = acc.post(reverse("payments:invoice_request", args=[inv.pk]),
                     {"provider": "MPESA", "amount": "5000", "phone": "0712 345 678"})
        req = PaymentRequest.objects.filter(invoice=inv).first()
        self.expect("the accountant sends a prompt to 254712345678",
                    r.status_code == 302 and req and req.phone == "254712345678" and req.status == "PENDING")
        r = acc.post(reverse("payments:invoice_request", args=[inv.pk]),
                     {"provider": "MPESA", "amount": "6000", "phone": "0712345678"})
        self.expect("more than the invoice owes is refused", PaymentRequest.objects.filter(invoice=inv).count() == 1)
        r = acc.post(reverse("payments:invoice_request", args=[inv.pk]),
                     {"provider": "MPESA", "amount": "100.50", "phone": "0712345678"})
        self.expect("M-Pesa takes whole shillings only", PaymentRequest.objects.filter(invoice=inv).count() == 1)

        body = {"Body": {"stkCallback": {"MerchantRequestID": "x", "CheckoutRequestID": req.provider_ref, "ResultCode": 0,
                                         "ResultDesc": "ok", "CallbackMetadata": {"Item": [
                                             {"Name": "Amount", "Value": 5000}, {"Name": "MpesaReceiptNumber", "Value": "FAKE123"}]}}}}
        anon = Client()
        r = anon.post(reverse("payments:callback", args=["mpesa", req.callback_key]), json.dumps(body),
                      content_type="application/json")
        req.refresh_from_db()
        self.expect("a callback that says 'paid' books nothing until M-Pesa itself confirms",
                    r.status_code == 200 and req.status == "PENDING" and not CustomerPayment.objects.filter(customer=self.cust).exists())
        r = anon.post(reverse("payments:callback", args=["mpesa", "not-a-key"]), "{}", content_type="application/json")
        self.expect("a callback with a wrong key is refused", r.status_code == 404)
        services.simulate(req, "PAID", receipt="QHX7Y2K9LM")
        req.refresh_from_db()
        inv.refresh_from_db()
        pay = req.customer_payment
        self.expect("confirmed: the receipt is booked against the invoice with the M-Pesa code",
                    req.status == "PAID" and pay and pay.payment_method == "MPESA" and pay.transaction_reference == "QHX7Y2K9LM"
                    and pay.journal_entry_id and inv.outstanding_amount == D("0.00"),
                    f"{req.status} {inv.outstanding_amount}")
        services.refresh(req)
        anon.post(reverse("payments:callback", args=["mpesa", req.callback_key]), json.dumps(body), content_type="application/json")
        self.expect("a second confirmation books nothing more",
                    CustomerPayment.objects.filter(customer=self.cust).count() == 1)

        # ── 3 ──
        self.section("3. the till")
        pos.open_session(self.co, cashier, self.hq, D("0"))
        cc = self.client_for(cashier)
        r = cc.post(reverse("payments:pos_mpesa_start"), {"amount": "350", "phone": "0722000111"})
        d = r.json()
        self.expect("the till sends an M-Pesa prompt", r.status_code == 200 and d.get("ok") and d.get("simulated"), str(d))
        r = cc.get(d["status_url"])
        self.expect("…which is waiting at first", r.json().get("status") == "PENDING")
        r = cc.post(d["status_url"], {"simulate": "PAID"})
        self.expect("…and shows paid with its code once the customer pays",
                    r.json().get("status") == "PAID" and r.json().get("receipt"), str(r.json()))
        r = cc.post(reverse("payments:pos_mpesa_start"), {"amount": "350", "phone": "07"})
        self.expect("a bad number is answered, not sent", r.status_code == 400 and "Kenyan" in r.json().get("error", ""))
        page = cc.get(reverse("pos:terminal")).content
        self.expect("the till's pay panel has the M-Pesa prompt", b'id="pay-mpesa-send"' in page)

        # ── 4 ──
        self.section("4. paybill (C2B)")
        inv2 = self.invoice("2400", f"INV-Q{self.stamp[-5:]}")
        c2b = {"TransactionType": "Pay Bill", "TransID": "RKT1ABC234", "TransAmount": "2400.00",
               "BusinessShortCode": "174379", "BillRefNumber": inv2.reference.lower(), "MSISDN": "2547***111",
               "FirstName": "Jane", "LastName": "Wanjiku"}
        url = reverse("payments:c2b_confirm", args=[gw.webhook_key])
        r = anon.post(url, json.dumps(c2b), content_type="application/json")
        inv2.refresh_from_db()
        rec = PaybillReceipt.objects.filter(trans_id="RKT1ABC234").first()
        self.expect("a paybill payment with the invoice number as account is booked to it",
                    r.status_code == 200 and r.json().get("ResultCode") == 0 and rec and rec.invoice_id == inv2.pk
                    and inv2.outstanding_amount == D("0.00"), f"{inv2.outstanding_amount}")
        anon.post(url, json.dumps(c2b), content_type="application/json")
        self.expect("the same M-Pesa code twice is booked once",
                    PaybillReceipt.objects.filter(trans_id="RKT1ABC234").count() == 1
                    and CustomerPayment.objects.filter(transaction_reference="RKT1ABC234").count() == 1)
        anon.post(url, json.dumps({**c2b, "TransID": "RKT1ZZZ999", "BillRefNumber": "WRONG"}), content_type="application/json")
        rec = PaybillReceipt.objects.get(trans_id="RKT1ZZZ999")
        self.expect("an unknown account number is kept to match by hand", rec.invoice_id is None and "by hand" in rec.note)
        r = anon.post(reverse("payments:c2b_confirm", args=["nope"]), json.dumps(c2b), content_type="application/json")
        self.expect("a wrong paybill key is refused", r.status_code == 404)

        # ── 5 ──
        self.section("5. Pesapal and DPO")
        for prov in ("PESAPAL", "DPO"):
            inv3 = self.invoice("1500", f"INV-{prov[:2]}{self.stamp[-5:]}")
            req = services.request_payment(self.co, prov, "1500", invoice=inv3, email="pay@wanjiku.example",
                                           request=None)
            self.expect(f"{prov}: a checkout link is made", req.status == "PENDING" and "/payments/simulate/" in req.checkout_url,
                        req.checkout_url)
            r = anon.get(reverse("payments:returned", args=[req.callback_key]))
            self.expect(f"{prov}: before paying, the return page says waiting", b"Waiting" in r.content)
            r = anon.post(reverse("payments:simulate", args=[req.callback_key]), {"result": "PAID"})
            req.refresh_from_db()
            inv3.refresh_from_db()
            self.expect(f"{prov}: paid on the checkout, the receipt is booked as a card payment",
                        req.status == "PAID" and req.customer_payment and req.customer_payment.payment_method == "CARD"
                        and inv3.outstanding_amount == D("0.00"))
            self.expect(f"{prov}: the return page says received", b"Payment received" in anon.get(
                reverse("payments:returned", args=[req.callback_key])).content)

        # ── 6 ──
        self.section("6. the real APIs (no network)")
        live = PaymentGateway(company=self.co, provider="MPESA", environment="SANDBOX", consumer_key="k",
                              consumer_secret="s", shortcode="174379", passkey="pk")
        seen = []

        def fake(method, url, body=None, headers=None, raw=False):
            seen.append((method, url, body))
            if "oauth" in url:
                return {"access_token": "tok", "expires_in": "3599"}
            if "processrequest" in url:
                return {"ResponseCode": "0", "CheckoutRequestID": "ws_CO_1", "MerchantRequestID": "m1"}
            if "stkpushquery" in url:
                return {"ResultCode": "1032", "ResultDesc": "Request cancelled by user"}
            return {}
        with mock.patch.object(backends, "_http", side_effect=fake):
            dreq = PaymentRequest(amount=D("350"), phone="254712345678", reference="INV-0001234567890", description="Invoice")
            out = backends.Mpesa(live).start(dreq, "https://erp.example/cb")
            push = [b for m, u, b in seen if "processrequest" in u][0]
            self.expect("Daraja STK push: paybill type, whole amount, 12-char account, our callback",
                        out["provider_ref"] == "ws_CO_1" and push["TransactionType"] == "CustomerPayBillOnline"
                        and push["Amount"] == 350 and len(push["AccountReference"]) == 12
                        and push["CallBackURL"] == "https://erp.example/cb" and push["PartyB"] == "174379")
            dreq.provider_ref = "ws_CO_1"
            self.expect("Daraja query 1032 reads as cancelled", backends.Mpesa(live).status(dreq)["status"] == "CANCELLED")
        live.shortcode_type, live.till_number = "TILL", "5551234"
        seen.clear()
        with mock.patch.object(backends, "_http", side_effect=fake):
            backends.Mpesa(live).start(dreq, "https://erp.example/cb")
        push = [b for m, u, b in seen if "processrequest" in u][0]
        self.expect("Buy Goods: CustomerBuyGoodsOnline to the till number",
                    push["TransactionType"] == "CustomerBuyGoodsOnline" and push["PartyB"] == "5551234")
        items = backends.stk_callback_items({"Body": {"stkCallback": {"CheckoutRequestID": "c", "ResultCode": 0,
                                             "CallbackMetadata": {"Item": [{"Name": "MpesaReceiptNumber", "Value": "ABC"}]}}}})
        self.expect("an STK callback is read", items["receipt"] == "ABC" and items["result"] == "0")

        pp = PaymentGateway(company=self.co, provider="PESAPAL", environment="SANDBOX", consumer_key="k", consumer_secret="s")
        no_token = mock.patch.object(backends.Pesapal, "token", return_value="tok")
        no_token.start()
        with mock.patch.object(backends, "_http", return_value={"status_code": 1, "confirmation_code": "PP123", "amount": 1500}):
            st = backends.Pesapal(pp).status(PaymentRequest(provider_ref="t1"))
        self.expect("Pesapal status_code 1 reads as paid with its code", st["status"] == "PAID" and st["receipt"] == "PP123")
        with mock.patch.object(backends, "_http", return_value={"status_code": 2, "payment_status_description": "Failed"}):
            st = backends.Pesapal(pp).status(PaymentRequest(provider_ref="t1"))
        self.expect("Pesapal status_code 2 reads as failed", st["status"] == "FAILED")
        no_token.stop()
        with mock.patch.object(backends, "_http", return_value={"error": {}}):
            try:
                backends.Pesapal(pp).token()
                said = ""
            except backends.GatewayError as exc:
                said = str(exc)
        self.expect("a refused Pesapal key says so in words", said == "Pesapal refused the key and secret.", said)
        dpo = PaymentGateway(company=self.co, provider="DPO", environment="SANDBOX", company_token="tok", service_type="3854")
        xml_ok = "<?xml version='1.0'?><API3G><Result>000</Result><ResultExplanation>Paid</ResultExplanation><TransactionApproval>DPO777</TransactionApproval></API3G>"
        with mock.patch.object(backends, "_http", return_value=xml_ok):
            st = backends.Dpo(dpo).status(PaymentRequest(provider_ref="tok"))
        self.expect("DPO verifyToken 000 reads as paid", st["status"] == "PAID" and st["receipt"] == "DPO777")
        sent = []
        with mock.patch.object(backends, "_http", side_effect=lambda *a, **k: sent.append(a) or
                               "<API3G><Result>000</Result><TransToken>T-1</TransToken></API3G>"):
            out = backends.Dpo(dpo).start(PaymentRequest(amount=D("10"), currency="KES", reference="A&B <1>"), "", "https://x/r")
        self.expect("DPO createToken escapes the XML and gives the payment page",
                    out["checkout_url"].endswith("T-1") and "A&amp;B &lt;1&gt;" in sent[0][2])

        # ── 7 ──
        self.section("7. roles")
        inv4 = self.invoice("900", f"INV-R{self.stamp[-5:]}")
        r = self.client_for(viewer).post(reverse("payments:invoice_request", args=[inv4.pk]),
                                         {"provider": "MPESA", "amount": "900", "phone": "0712345678"})
        self.expect("a viewer cannot ask a customer to pay", r.status_code == 403
                    and not PaymentRequest.objects.filter(invoice=inv4).exists(), f"{r.status_code}")
        r = acc.post(reverse("payments:gateway_edit", args=["mpesa"]), {"environment": "LIVE"})
        self.expect("an accountant cannot change the gateway", r.status_code == 403)
        self.expect("an accountant can read the payments log", acc.get(reverse("payments:home")).status_code == 200)
        self.expect("a cashier cannot read the log", cc.get(reverse("payments:home")).status_code == 403)
        r = self.ac.get(reverse("sales:invoice_detail", args=[inv4.pk]))
        self.expect("the invoice page offers the switched-on gateways with their logos",
                    b"Ask the customer to pay" in r.content and b"payments/logos/mpesa.svg" in r.content
                    and b"payments/logos/dpo.svg" in r.content)
