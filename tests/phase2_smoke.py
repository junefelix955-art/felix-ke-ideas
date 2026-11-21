r"""Phase 2 smoke test: M-Pesa payments (with a FAKE Safaricom), order tracking, purchased downloads, receipts,
account pages and password-reset links.   py tests\phase2_smoke.py
It cannot prove the live Safaricom connection works. Do one real sandbox payment after deploying."""
import io, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shim import *
from features import payments

PDF = b"%PDF-1.4\n" + b"premium content\n" * 40
TOKEN = "cb-secret-token-0123456789"

# ---------------- fake Safaricom
FAKE = {"pushes": [], "queries": {}, "net_down": False, "push_fail": False}
def fake_http(method, url, headers=None, body=None, timeout=15):
    if FAKE["net_down"]:
        raise OSError("network down")
    if "oauth" in url:
        return 200, {"access_token": "T0K", "expires_in": "3599"}
    if "processrequest" in url:
        if FAKE["push_fail"]:
            return 400, {"errorCode": "400.002.02", "errorMessage": "Bad Request - Invalid PhoneNumber"}
        FAKE["pushes"].append(body)
        n = len(FAKE["pushes"])
        return 200, {"MerchantRequestID": "mr-%d" % n, "CheckoutRequestID": "ws_CO_%d" % n, "ResponseCode": "0", "ResponseDescription": "Success"}
    if "stkpushquery" in url:
        what = FAKE["queries"].get(body["CheckoutRequestID"], "pending")
        if what == "pending":
            return 500, {"requestId": "x", "errorCode": "500.001.1001", "errorMessage": "The transaction is being processed"}
        if what == "success":
            return 200, {"ResponseCode": "0", "ResultCode": "0", "ResultDesc": "The service request is processed successfully."}
        return 200, {"ResponseCode": "0", "ResultCode": "1032", "ResultDesc": "Request cancelled by user"}
    raise AssertionError("unexpected URL " + url)
payments.http_json = fake_http

def callback(cl, checkout, code=0, amount=1500, receipt="NLJ7RT61SV", token=TOKEN):
    cb = {"MerchantRequestID": "x", "CheckoutRequestID": checkout, "ResultCode": code, "ResultDesc": "d"}
    if code == 0:
        cb["CallbackMetadata"] = {"Item": [{"Name": "Amount", "Value": amount}, {"Name": "MpesaReceiptNumber", "Value": receipt},
                                           {"Name": "PhoneNumber", "Value": 254712345678}]}
    return cl.post("/payments/mpesa/callback/" + token, json={"Body": {"stkCallback": cb}})

def user_client(name):
    cl = fresh()
    r = post(cl, "/register", {"username": name, "password": "longenough1"})
    assert r.status_code == 302, r.status_code
    with cl.session_transaction() as s:
        s["_csrf_token"] = "tok"
    return cl

def order(cl, slug, email="amina@example.com", phone="0712 345 678"):
    r = post(cl, "/marketplace/%s/order" % slug, {"name": "Amina Otieno", "email": email, "phone": phone})
    assert r.status_code == 302, (r.status_code, r.data[:300])
    return r.headers["Location"]

def db(sql, *p):
    c = raw(); r = c.execute(sql, p).fetchall(); c.close(); return r

def rev():
    c = Conn()
    try:
        return payments.verified_revenue(c)
    finally:
        c.close()

def enable_mpesa():
    os.environ.update(MPESA_CONSUMER_KEY="ck", MPESA_CONSUMER_SECRET="cs", MPESA_SHORTCODE="174379", MPESA_PASSKEY="pk",
                      MPESA_CALLBACK_TOKEN=TOKEN, SITE_URL="https://felix.example.com")

anon, admin = fresh(), fresh()
post(admin, "/login", {"username": "admin", "password": "correct horse"})
with admin.session_transaction() as s:
    s["_csrf_token"] = "tok"

def new_product(**over):
    d = {"title": "Flask Guide Premium", "summary": "s", "description": "d", "kind": "resource", "category": "Programming resources",
         "topic": "Flask", "price_kes": "1500", "availability": "available", "published": "on", "file": (io.BytesIO(PDF), "flask.pdf")}
    d.update(over)
    r = post(admin, "/admin/marketplace/new", d, content_type="multipart/form-data")
    assert r.status_code == 302, r.data[:300]
new_product()
new_product(title="Consulting Hour", kind="service", category="Website development services", topic="", price_kes="800", file=None)
new_product(title="Free Notes", price_kes="0")

print("Payments switched off (no credentials)")
loc0 = order(anon, "flask-guide-premium")
page = anon.get(loc0)
ok("order page shows manual instructions, no pay button", b"I'll contact you" in page.data and b"Pay 1,500" not in page.data and b"Pay KES" not in page.data)
r = post(anon, loc0 + "/pay", {"phone": "0712345678"})
ok("pay endpoint refuses politely when disabled", r.status_code == 302 and db("SELECT COUNT(*) FROM payments")[0][0] == 0)
ok("admin sees M-Pesa is off and what is missing", b"M-Pesa is <strong>off</strong>" in admin.get("/admin/payments").data and b"MPESA_PASSKEY" in admin.get("/admin/payments").data)
ok("a weak callback token does not enable payments", not payments.config()["enabled"])

print("Switching M-Pesa on")
enable_mpesa()
ok("config enabled", payments.config()["enabled"])
page = anon.get(loc0)
ok("pay box appears with the phone prefilled", b"Pay with M-Pesa" in page.data and b'value="0712 345 678"' in page.data)
for bad in ("12345", "abc", "0612345678", "+1 555 123 4567"):
    post(anon, loc0 + "/pay", {"phone": bad})
ok("bad phone numbers never reach Safaricom", FAKE["pushes"] == [] and db("SELECT COUNT(*) FROM payments")[0][0] == 0)
ok("phone normalising", [payments.msisdn(x) for x in ("0712 345 678", "+254712345678", "254112345678", "0112345678", "712345678")] ==
   ["254712345678", "254712345678", "254112345678", "254112345678", "254712345678"] and payments.msisdn("0812345678") is None)

print("Starting a payment")
r = post(anon, loc0 + "/pay", {"phone": "0712 345 678", "amount": "1", "total_kes": "1"})
ok("pay redirects back to the order", r.status_code == 302 and r.headers["Location"] == loc0)
push = FAKE["pushes"][0]
ok("customer cannot change the amount (read from our database)", push["Amount"] == 1500)
ok("push goes to the right number with a secret callback URL", push["PartyA"] == "254712345678" and push["PhoneNumber"] == "254712345678"
   and push["CallBackURL"] == "https://felix.example.com/payments/mpesa/callback/" + TOKEN)
ok("AccountReference within Daraja's 12 character limit", len(push["AccountReference"]) <= 12 and len(push["TransactionDesc"]) <= 13)
ok("timestamp/password are base64 of shortcode+passkey+timestamp", __import__("base64").b64decode(push["Password"]).decode() == "174379pk" + push["Timestamp"])
rows = db("SELECT status, amount_kes, phone, checkout_request_id FROM payments")
ok("payment row recorded as initiated", rows == [("initiated", 1500, "254712345678", "ws_CO_1")], rows)
page = anon.get(loc0)
ok("order page now says 'Check your phone' and polls for status", b"Check your phone" in page.data and b"/status" in page.data and b"0712" not in page.data.split(b"Check your phone")[1][:300])
post(anon, loc0 + "/pay", {"phone": "0712 345 678"})
ok("double click does not send a second prompt", len(FAKE["pushes"]) == 1)
ok("order is NOT paid just because a prompt was sent", db("SELECT status FROM orders WHERE reference LIKE 'FKE%'")[0][0] == "pending")
st = anon.get(loc0 + "/status").get_json()
ok("status endpoint: waiting while Safaricom says pending", st["state"] == "waiting")
ok("browser redirect cannot mark an order paid", db("SELECT status FROM orders")[0][0] == "pending" and anon.get(loc0 + "?paid=1&status=paid").status_code == 200
   and db("SELECT status FROM orders")[0][0] == "pending")

print("Forged and replayed callbacks")
ok("callback with a wrong secret is rejected", callback(anon, "ws_CO_1", token="wrong-secret-000000000000").status_code == 404)
ok("callback on a non-configured token (empty) is rejected", anon.post("/payments/mpesa/callback/x", json={}).status_code == 404)
r = callback(anon, "ws_CO_1")  # claims success, but Safaricom's own answer is still 'pending'
ok("forged success callback is acknowledged but does NOT pay the order", r.status_code == 200 and r.get_json()["ResultCode"] == 0
   and db("SELECT status FROM orders")[0][0] == "pending" and db("SELECT status FROM payments")[0][0] == "initiated")
ok("garbage callback bodies are harmless", anon.post("/payments/mpesa/callback/" + TOKEN, data="not json", content_type="application/json").status_code == 200
   and anon.post("/payments/mpesa/callback/" + TOKEN, json={"Body": {}}).status_code == 200)
ok("callback for an unknown checkout id is ignored", callback(anon, "ws_CO_999").status_code == 200)

print("A genuine payment")
FAKE["queries"]["ws_CO_1"] = "success"
callback(anon, "ws_CO_1")
ok("verified callback marks the order paid", db("SELECT status FROM orders")[0][0] == "paid")
rows = db("SELECT status, receipt, result_code, paid_at IS NOT NULL FROM payments")
ok("payment verified, receipt + time stored", rows == [("success", "NLJ7RT61SV", 0, 1)], rows)
callback(anon, "ws_CO_1"); callback(anon, "ws_CO_1", receipt="ZZZZZZZZZZ")
ok("replayed callbacks change nothing (idempotent)", db("SELECT COUNT(*), SUM(amount_kes) FROM payments WHERE status='success'")[0] == (1, 1500)
   and db("SELECT receipt FROM payments")[0][0] == "NLJ7RT61SV")
ok("revenue counts the verified payment once", rev() == 1500)
page = anon.get(loc0)
ok("order page shows paid, receipt number, download and receipt links", b"Payment received" in page.data and b"NLJ7RT61SV" in page.data
   and b"Download your file" in page.data and b"receipt" in page.data.lower())
ok("status endpoint says paid", anon.get(loc0 + "/status").get_json()["state"] == "paid")
r = anon.get(loc0 + "/download")
ok("buyer downloads the premium file with the order link", r.status_code == 200 and r.data == PDF and "attachment" in r.headers["Content-Disposition"])
ok("download recorded in the history", db("SELECT COUNT(*) FROM download_log WHERE order_id IS NOT NULL")[0][0] == 1 and db("SELECT downloads FROM products WHERE slug='flask-guide-premium'")[0][0] == 1)
ok("anonymous visitors still cannot fetch the premium file by product URL", anon.get("/marketplace/flask-guide-premium/download").status_code == 302)
ok("paying again for a paid order is refused", (post(anon, loc0 + "/pay", {"phone": "0712345678"}), len(FAKE["pushes"]))[1] == 1)
rc = anon.get(loc0 + "/receipt")
ok("receipt page for a paid order (phone masked)", rc.status_code == 200 and b"NLJ7RT61SV" in rc.data and b"254712345678" not in rc.data and b"678" in rc.data)
ok("order link with a different token is a 404", anon.get("/order/" + "b" * 32 + "/download").status_code == 404 and anon.get("/order/" + "b" * 32 + "/receipt").status_code == 404)

print("Unpaid orders cannot get files or receipts")
locU = order(anon, "flask-guide-premium", email="other@example.com")
ok("download blocked before payment", anon.get(locU + "/download").status_code == 302 and b"attachment" not in anon.get(locU + "/download").headers.get("Content-Disposition", "").encode())
ok("receipt 404 before payment", anon.get(locU + "/receipt").status_code == 404)

print("Failed and cancelled payments")
loc2 = order(anon, "consulting-hour", email="c@example.com")
post(anon, loc2 + "/pay", {"phone": "0722000111"})
FAKE["queries"]["ws_CO_2"] = "cancelled"
callback(anon, "ws_CO_2", code=1032)
ok("customer cancelled: payment failed, order still open", db("SELECT status FROM payments WHERE checkout_request_id='ws_CO_2'")[0][0] == "failed"
   and db("SELECT status FROM orders WHERE product_title='Consulting Hour'")[0][0] == "pending")
page = anon.get(loc2)
ok("customer sees why and can try again", b"cancelled by user" in page.data and b"Pay KES 800 with M-Pesa" in page.data)
post(anon, loc2 + "/pay", {"phone": "0722000111"})
ok("retry sends a new prompt", len(FAKE["pushes"]) == 3)
FAKE["queries"]["ws_CO_3"] = "success"
ok("paid via the status poll alone (callback never arrived)", anon.get(loc2 + "/status").get_json()["state"] == "paid" and db("SELECT status FROM orders WHERE product_title='Consulting Hour'")[0][0] == "paid")
ok("poll-settled payment has no receipt number but is verified", db("SELECT status, receipt FROM payments WHERE checkout_request_id='ws_CO_3'")[0] == ("success", None))
ok("service order (no file) shows delivery message", b"no instant download" in anon.get(loc2).data)

print("Safaricom problems")
loc3 = order(anon, "consulting-hour", email="d@example.com")
FAKE["push_fail"] = True
post(anon, loc3 + "/pay", {"phone": "0733000222"})
ok("refused STK push: friendly error, attempt recorded as failed, nothing paid", db("SELECT status FROM payments ORDER BY id DESC LIMIT 1")[0][0] == "failed")
FAKE["push_fail"] = False
post(anon, loc3 + "/pay", {"phone": "0733000222"})
FAKE["net_down"] = True
cid = db("SELECT checkout_request_id FROM payments ORDER BY id DESC LIMIT 1")[0][0]
FAKE["queries"][cid] = "success"
callback(anon, cid)
ok("Safaricom unreachable during verification: NOT paid, stays pending", db("SELECT status FROM orders WHERE customer_email='d@example.com'")[0][0] == "pending")
ok("status endpoint survives the outage", anon.get(loc3 + "/status").get_json()["state"] == "waiting")
FAKE["net_down"] = False
pid = db("SELECT id FROM payments ORDER BY id DESC LIMIT 1")[0][0]
r = post(admin, "/admin/payments/%d/recheck" % pid)
ok("admin re-check settles it once Safaricom is reachable", db("SELECT status FROM orders WHERE customer_email='d@example.com'")[0][0] == "paid")
ok("re-check on an already-settled payment is harmless", post(admin, "/admin/payments/%d/recheck" % pid).status_code == 302)
ok("visitors cannot re-check", post(anon, "/admin/payments/%d/recheck" % pid).status_code == 302 and db("SELECT COUNT(*) FROM payments WHERE status='success'")[0][0] == 3)

print("Double payment")
c = raw(); c.execute("INSERT INTO payments (order_id, amount_kes, phone, status, checkout_request_id) VALUES (1, 1500, '254712345678', 'initiated', 'ws_CO_dup')"); c.commit(); c.close()
FAKE["queries"]["ws_CO_dup"] = "success"
callback(anon, "ws_CO_dup")
ok("second verified payment is flagged as duplicate, not double-counted", db("SELECT status FROM payments WHERE checkout_request_id='ws_CO_dup'")[0][0] == "duplicate"
   and rev() == 1500 + 800 + 800)
ok("duplicate shows up for the admin to refund", b"Duplicates to refund" in admin.get("/admin/payments").data and b"Duplicate" in admin.get("/admin/payments?status=duplicate").data)
c = raw()
try:
    c.execute("INSERT INTO payments (order_id, amount_kes, phone, status) VALUES (1, 1500, '254712345678', 'success')"); c.commit(); dbblock = False
except Exception:
    dbblock = True
c.rollback(); c.close()
ok("the database itself refuses a second 'success' for one order", dbblock)

print("Admin views")
ok("admin payments page lists receipts", b"NLJ7RT61SV" in admin.get("/admin/payments").data)
ok("filters: status / search / dates", b"NLJ7RT61SV" in admin.get("/admin/payments?status=success&q=NLJ7").data and b"No payments match" in admin.get("/admin/payments?q=nothingmatches").data
   and b"No payments match" in admin.get("/admin/payments?from=2000-01-01&to=2000-01-02").data)
ok("dashboard shows verified revenue", b"KES 3,100" in admin.get("/admin").data)
ok("admin orders show the receipt, 'paid' not settable by hand", b"NLJ7RT61SV" in admin.get("/admin/orders").data
   and post(admin, "/admin/orders/1/status", {"status": "paid"}).status_code == 400)
ok("payments pages closed to visitors", anon.get("/admin/payments").status_code == 302 and anon.get("/admin/users").status_code == 302)
ok("callback is not subject to CSRF checks", callback(anon, "ws_CO_1").status_code == 200)

print("Customer accounts, orders and downloads")
alice, bob = user_client("alice_k"), user_client("bob_m")
locA = order(alice, "flask-guide-premium", email="alice@example.com")
ok("order placed while signed in is linked to the account", db("SELECT user_id FROM orders WHERE customer_email='alice@example.com'")[0][0] is not None)
ok("unpaid: alice's product page offers ordering, not download", b"Place order" in alice.get("/marketplace/flask-guide-premium").data)
ok("alice cannot download before paying", alice.get("/marketplace/flask-guide-premium/download").status_code == 302)
post(alice, locA + "/pay", {"phone": "0711000111"})
cidA = db("SELECT checkout_request_id FROM payments ORDER BY id DESC LIMIT 1")[0][0]
FAKE["queries"][cidA] = "success"
callback(anon, cidA, receipt="ALICE12345", amount=1500)
html = alice.get("/account/orders").data.decode()
ok("alice's order history shows the paid order with receipt + download", "FKE-" in html and "Paid" in html and "Receipt" in html and "Download" in html)
ok("alice got a notification when payment was verified", db("SELECT COUNT(*) FROM notifications WHERE title LIKE 'Payment received%'")[0][0] == 1)
ok("bob cannot see alice's orders", "FKE-" not in bob.get("/account/orders").data.decode() and b"No orders yet" in bob.get("/account/orders").data)
r = alice.get("/marketplace/flask-guide-premium/download")
ok("alice (paid owner) downloads from the product page too", r.status_code == 200 and r.data == PDF)
ok("product page tells alice she owns it", b"You own this" in alice.get("/marketplace/flask-guide-premium").data)
ok("bob (not an owner) is still locked out", bob.get("/marketplace/flask-guide-premium/download").status_code == 302 and b"You own this" not in bob.get("/marketplace/flask-guide-premium").data)
dl = alice.get("/account/downloads").data
ok("alice's download history lists it; bob's is empty", b"Flask Guide Premium" in dl and b"No downloads yet" in bob.get("/account/downloads").data)
ok("visitors are sent to sign in", anon.get("/account/orders").status_code == 302 and anon.get("/account/downloads").status_code == 302)
r = post(bob, locA + "/claim")
ok("bob cannot steal alice's order via claim", db("SELECT user_id FROM orders WHERE customer_email='alice@example.com'")[0][0] == db("SELECT id FROM users WHERE username='alice_k'")[0][0])
ok("guest order can be claimed by a signed-in user", post(bob, loc3 + "/claim").status_code == 302 and "FKE-" in bob.get("/account/orders").data.decode())
ok("claim button shown only when it makes sense", b"Add this order to my account" in bob.get(locU).data and b"Add this order to my account" not in anon.get(locU).data)

print("Password reset links")
uid = db("SELECT id FROM users WHERE username='bob_m'")[0][0]
r = post(admin, "/admin/users/%d/reset-link" % uid)
link = re.search(rb"https?://[^\s\"<]+/reset/[A-Za-z0-9_-]+", r.data).group(0).decode()
path = "/reset/" + link.rsplit("/", 1)[1]
ok("admin gets a one-time link (shown once, no-store)", r.status_code == 200 and "no-store" in r.headers.get("Cache-Control", ""))
ok("link page opens for the member", fresh().get(path).status_code == 200)
ok("token is stored hashed, not in the database", link.rsplit("/", 1)[1] not in str(db("SELECT token_hash FROM password_resets")))
cl = fresh()
ok("too-short and mismatched passwords are refused", post(cl, path, {"new_password": "short", "confirm_password": "short"}).status_code == 400
   and post(cl, path, {"new_password": "newpassword9", "confirm_password": "different99"}).status_code == 400)
r = post(cl, path, {"new_password": "newpassword9", "confirm_password": "newpassword9"})
ok("password changed and member sent to sign in", r.status_code == 302 and r.headers["Location"].endswith("/signin"))
ok("new password works, old one does not", post(fresh(), "/signin", {"username": "bob_m", "password": "newpassword9"}).status_code == 302
   and post(fresh(), "/signin", {"username": "bob_m", "password": "longenough1"}).status_code == 400)
ok("link cannot be used twice", fresh().get(path).status_code == 410)
r1 = post(admin, "/admin/users/%d/reset-link" % uid); p1 = "/reset/" + re.search(rb"/reset/([A-Za-z0-9_-]+)", r1.data).group(1).decode()
r2 = post(admin, "/admin/users/%d/reset-link" % uid); p2 = "/reset/" + re.search(rb"/reset/([A-Za-z0-9_-]+)", r2.data).group(1).decode()
ok("a newer link cancels the older one", fresh().get(p1).status_code == 410 and fresh().get(p2).status_code == 200)
c = raw(); c.execute("UPDATE password_resets SET expires_at = '2000-01-01 00:00:00' WHERE used_at IS NULL"); c.commit(); c.close()
ok("expired links stop working", fresh().get(p2).status_code == 410)
ok("made-up tokens are 404/410, not errors", fresh().get("/reset/" + "z" * 40).status_code == 410 and fresh().get("/reset/x").status_code == 404)
ok("only the admin can create reset links", post(anon, "/admin/users/%d/reset-link" % uid).status_code == 302 and post(alice, "/admin/users/%d/reset-link" % uid).status_code == 302)
ok("customers page lists members with order counts", b"alice_k" in admin.get("/admin/users").data and b"alice_k" not in admin.get("/admin/users?q=zzz").data)

finish()
