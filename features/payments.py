"""Business features, phase 2: M-Pesa payments, order tracking, purchased downloads, receipts, customer order history,
and admin-issued password-reset links.

How a payment is trusted (read this before changing anything):
- The customer's browser is NEVER proof of payment. Nothing on a redirect or a form field can mark an order paid.
- Safaricom's callback is only a TRIGGER ("something happened, go and check"). Anyone who learned the callback URL could
  post a fake one, so the server then asks Safaricom directly (STK Push Query API) and believes only that answer.
- A payment is settled at most once. A second verified payment for an already-paid order is stored as 'duplicate'
  (money to refund) and the database also refuses a second 'success' per order.
- Card numbers and M-Pesa PINs never touch this server: the PIN is typed on the customer's phone.

With no M-Pesa settings in the environment, payments are simply switched off and orders stay manual (phase 1 behaviour).
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from datetime import timedelta

from flask import render_template, request, redirect, url_for, session, flash, abort, jsonify

from . import common as fc

log = logging.getLogger("felix.payments")

PER_PAGE = 15
RESET_MINUTES = 60
PAY_STATUS_LABEL = {"initiated": "Waiting for customer", "success": "Verified", "failed": "Failed", "duplicate": "Duplicate (refund?)"}
SANDBOX_URL, LIVE_URL = "https://sandbox.safaricom.co.ke", "https://api.safaricom.co.ke"


class PaymentError(Exception):
    """A problem talking to the payment provider. The message is safe to show to the customer."""


# ---------------------------------------------------------------- configuration (read on every call so tests/hosts can change it)
def config():
    e = lambda k, d="": os.getenv(k, d).strip()
    till = e("MPESA_TILL_NUMBER")
    cfg = {
        "env": "production" if e("MPESA_ENV", "sandbox").lower() == "production" else "sandbox",
        "key": e("MPESA_CONSUMER_KEY"), "secret": e("MPESA_CONSUMER_SECRET"), "shortcode": e("MPESA_SHORTCODE"),
        "passkey": e("MPESA_PASSKEY"), "token": e("MPESA_CALLBACK_TOKEN"), "site": e("SITE_URL").rstrip("/"),
        "till": till, "txn_type": "CustomerBuyGoodsOnline" if till else "CustomerPayBillOnline",
    }
    cfg["base"] = LIVE_URL if cfg["env"] == "production" else SANDBOX_URL
    cfg["callback_url"] = "%s/payments/mpesa/callback/%s" % (cfg["site"], cfg["token"]) if cfg["token"] else ""
    cfg["enabled"] = bool(all(cfg[k] for k in ("key", "secret", "shortcode", "passkey", "site")) and cfg["site"].startswith("https://")
                          and len(cfg["token"]) >= 16)
    return cfg


def msisdn(phone):
    """0712 345 678 / +254712345678 / 254112345678 -> '254712345678'. None if it is not a Kenyan mobile number."""
    d = re.sub(r"[\s()\-]", "", phone or "")
    m = re.fullmatch(r"(?:\+?254|0)?([17]\d{8})", d)
    return "254" + m.group(1) if m else None


# ---------------------------------------------------------------- talking to Safaricom (everything goes through http_json so tests can fake it)
def http_json(method, url, headers=None, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as ex:  # Daraja answers 4xx/5xx with a JSON body that we want to read
        try:
            return ex.code, json.loads(ex.read().decode() or "{}")
        except ValueError:
            return ex.code, {}


_TOKEN = {"value": None, "expires": 0.0, "key": None}


def access_token(cfg):
    now = time.time()
    if _TOKEN["value"] and _TOKEN["expires"] > now + 30 and _TOKEN["key"] == (cfg["base"], cfg["key"]):
        return _TOKEN["value"]
    basic = base64.b64encode(("%s:%s" % (cfg["key"], cfg["secret"])).encode()).decode()
    try:
        status, body = http_json("GET", cfg["base"] + "/oauth/v1/generate?grant_type=client_credentials", {"Authorization": "Basic " + basic})
    except Exception as ex:
        log.error("M-Pesa token request failed: %s", ex.__class__.__name__)
        raise PaymentError("M-Pesa is not reachable right now. Please try again in a minute.")
    if status != 200 or not body.get("access_token"):
        log.error("M-Pesa token request refused (HTTP %s)", status)
        raise PaymentError("M-Pesa payments are temporarily unavailable. Please try again later or contact me.")
    _TOKEN.update(value=body["access_token"], expires=now + int(body.get("expires_in", 3000) or 3000), key=(cfg["base"], cfg["key"]))
    return _TOKEN["value"]


def _stamp_password(cfg):
    stamp = fc.utcnow().astimezone(fc.SITE_TZ).strftime("%Y%m%d%H%M%S")
    return stamp, base64.b64encode((cfg["shortcode"] + cfg["passkey"] + stamp).encode()).decode()


def stk_push(cfg, reference, amount, phone):
    """Ask Safaricom to show the PIN prompt on the customer's phone. Returns (checkout_request_id, merchant_request_id)."""
    stamp, password = _stamp_password(cfg)
    body = {"BusinessShortCode": cfg["shortcode"], "Password": password, "Timestamp": stamp, "TransactionType": cfg["txn_type"],
            "Amount": int(amount), "PartyA": phone, "PartyB": cfg["till"] or cfg["shortcode"], "PhoneNumber": phone,
            "CallBackURL": cfg["callback_url"], "AccountReference": re.sub(r"[^A-Za-z0-9]", "", reference)[:12],
            "TransactionDesc": "Felix KE"}
    try:
        status, res = http_json("POST", cfg["base"] + "/mpesa/stkpush/v1/processrequest",
                                {"Authorization": "Bearer " + access_token(cfg)}, body)
    except PaymentError:
        raise
    except Exception as ex:
        log.error("STK push failed to send: %s", ex.__class__.__name__)
        raise PaymentError("M-Pesa did not respond. Nothing was charged. Please try again.")
    if str(res.get("ResponseCode")) != "0" or not res.get("CheckoutRequestID"):
        log.warning("STK push refused: HTTP %s %s", status, str(res.get("errorMessage") or res.get("ResponseDescription"))[:120])
        raise PaymentError("M-Pesa could not send the payment request to that number. Check the number and try again.")
    return str(res["CheckoutRequestID"])[:64], str(res.get("MerchantRequestID") or "")[:64]


def query_status(cfg, checkout_id):
    """Ask Safaricom what really happened. Returns ('success'|'failed'|'pending', result_code, description).
    Raises PaymentError if Safaricom cannot be reached (the caller leaves the payment unsettled and tries again later)."""
    stamp, password = _stamp_password(cfg)
    try:
        status, res = http_json("POST", cfg["base"] + "/mpesa/stkpushquery/v1/query", {"Authorization": "Bearer " + access_token(cfg)},
                                {"BusinessShortCode": cfg["shortcode"], "Password": password, "Timestamp": stamp,
                                 "CheckoutRequestID": checkout_id})
    except PaymentError:
        raise
    except Exception as ex:
        raise PaymentError("query failed: " + ex.__class__.__name__)
    if "ResultCode" not in res:
        # While the customer is still deciding, Daraja answers with an error body ("being processed"). That is "pending", not a failure.
        if str(res.get("errorCode", "")).startswith("500.001.1001") or "being processed" in str(res.get("errorMessage", "")).lower():
            return "pending", None, "Still being processed"
        raise PaymentError("unexpected query answer (HTTP %s)" % status)
    try:
        code = int(str(res["ResultCode"]))
    except ValueError:
        raise PaymentError("unreadable ResultCode")
    return ("success" if code == 0 else "failed"), code, str(res.get("ResultDesc") or "")[:200]


# ---------------------------------------------------------------- settling (the only place an order becomes 'paid')
_notify = None  # set by register()


def settle(c, pay_id, verdict, code, desc):
    """Apply a Safaricom-VERIFIED result to one payment, exactly once. Returns 'paid' | 'duplicate' | 'failed' | 'unchanged'."""
    row = c.execute("SELECT order_id, status, claimed_receipt FROM payments WHERE id = %s", (pay_id,)).fetchone()
    if not row or row[1] != "initiated":
        return "unchanged"  # already settled by a callback, a poll or the admin: nothing to do
    order_id, _, claimed = row
    desc = (desc or "")[:200]
    if verdict != "success":
        n = c.execute("UPDATE payments SET status = 'failed', result_code = %s, result_desc = %s, updated_at = NOW() "
                      "WHERE id = %s AND status = 'initiated'", (code, desc, pay_id)).rowcount
        c.commit()
        return "failed" if n else "unchanged"
    other = c.execute("SELECT 1 FROM payments WHERE order_id = %s AND status = 'success' AND id <> %s", (order_id, pay_id)).fetchone()
    status = "duplicate" if other else "success"
    try:
        n = c.execute("UPDATE payments SET status = %s, result_code = %s, result_desc = %s, receipt = %s, paid_at = NOW(), "
                      "updated_at = NOW() WHERE id = %s AND status = 'initiated'", (status, code, desc, claimed, pay_id)).rowcount
        if n and status == "success":
            # Even a cancelled or manually edited order becomes paid: the money has arrived.
            c.execute("UPDATE orders SET status = 'paid', updated_at = NOW() WHERE id = %s", (order_id,))
        c.commit()
    except Exception:
        # Two payments for the same order verified at the same instant, or the receipt number already exists on another payment.
        c.rollback()
        log.warning("payment %s: unique constraint hit while settling; recording as duplicate for review", pay_id)
        n = c.execute("UPDATE payments SET status = 'duplicate', result_code = %s, result_desc = %s, receipt = NULL, paid_at = NOW(), "
                      "updated_at = NOW() WHERE id = %s AND status = 'initiated'", (code, desc, pay_id)).rowcount
        c.commit()
        status = "duplicate"
    if not n:
        return "unchanged"
    if status == "success" and _notify:
        try:
            o = c.execute("SELECT user_id, product_title FROM orders WHERE id = %s", (order_id,)).fetchone()
            if o and o[0]:
                _notify(c, o[0], "system", "Payment received: " + o[1], "Your order is paid. Open it to download or print your receipt.", "/account/orders")
                c.commit()
        except Exception:
            c.rollback()
    return "paid" if status == "success" else "duplicate"


def refresh_payment(c, pay_id, checkout_id):
    """Verify one in-flight payment with Safaricom and settle it. Returns settle()'s words, or 'pending' / 'unknown'."""
    if not checkout_id:
        return "unknown"
    cfg = config()
    if not cfg["enabled"]:
        return "unknown"
    try:
        verdict, code, desc = query_status(cfg, checkout_id)
    except PaymentError as ex:
        log.warning("payment %s: could not verify yet (%s)", pay_id, ex)
        c.execute("UPDATE payments SET last_checked = NOW() WHERE id = %s", (pay_id,))
        c.commit()
        return "unknown"
    c.execute("UPDATE payments SET last_checked = NOW() WHERE id = %s", (pay_id,))
    c.commit()
    if verdict == "pending":
        return "pending"
    return settle(c, pay_id, verdict, code, desc)


# ---------------------------------------------------------------- helpers shared with the marketplace module
def owned_order(c, user_id, product_id):
    """The id of a PAID order for this product that belongs to this signed-in user, or None."""
    if not user_id:
        return None
    row = c.execute("SELECT id FROM orders WHERE user_id = %s AND product_id = %s AND status = 'paid' ORDER BY id DESC LIMIT 1",
                    (user_id, product_id)).fetchone()
    return row[0] if row else None


def log_download(c, order_id, product_id, user_id, title):
    c.execute("INSERT INTO download_log (order_id, product_id, user_id, product_title) VALUES (%s, %s, %s, %s)",
              (order_id, product_id, user_id, (title or "")[:120]))
    if product_id:
        c.execute("UPDATE products SET downloads = downloads + 1 WHERE id = %s", (product_id,))
    c.commit()


def verified_revenue(c):
    return c.execute("SELECT COALESCE(SUM(amount_kes), 0) FROM payments WHERE status = 'success'").fetchone()[0]


def _like(s):
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _page():
    try:
        return max(1, min(int(request.args.get("page", "1")), 100000))
    except ValueError:
        return 1


def _pages(total):
    return max(1, -(-total // PER_PAGE))


# ---------------------------------------------------------------- routes
def register(app, h):
    global _notify
    _notify = h.notify

    @app.template_filter("pay_label")
    def pay_label(status):
        return PAY_STATUS_LABEL.get(status, status)

    # The order lookup used by every token-based page. The long random token IS the customer's key to their order.
    def get_order(c, token):
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", token or ""):
            abort(404)
        row = c.execute(
            "SELECT o.id, o.reference, o.product_id, o.product_title, o.total_kes, o.status, o.customer_name, o.customer_email, "
            "o.customer_phone, o.created_at, o.user_id, p.slug, COALESCE(p.file_size, 0) FROM orders o "
            "LEFT JOIN products p ON p.id = o.product_id WHERE o.access_token = %s", (token,)).fetchone()
        if not row:
            abort(404)
        keys = ("id", "reference", "product_id", "title", "total", "status", "name", "email", "phone", "created_at", "user_id", "slug", "file_size")
        return dict(zip(keys, row))

    def last_payment(c, order_id):
        r = c.execute("SELECT id, status, checkout_request_id, created_at, result_desc, receipt, paid_at, phone FROM payments "
                      "WHERE order_id = %s AND status <> 'duplicate' ORDER BY id DESC LIMIT 1", (order_id,)).fetchone()
        return dict(zip(("id", "status", "checkout", "created_at", "desc", "receipt", "paid_at", "phone"), r)) if r else None

    @app.route("/order/<token>")
    def order_confirmation(token):
        cfg = config()
        c = h.get_db_connection()
        try:
            o = get_order(c, token)
            pay = last_payment(c, o["id"])
            if pay and pay["status"] == "initiated" and pay["checkout"]:
                # The customer is looking at the page: take the chance to verify (quietly) so a missed callback never strands a payment.
                if (fc.utcnow() - pay["created_at"]).total_seconds() > 20:
                    refresh_payment(c, pay["id"], pay["checkout"])
                    o = get_order(c, token)
                    pay = last_payment(c, o["id"])
            can_claim = bool(session.get("user_id")) and o["user_id"] is None
        finally:
            c.close()
        waiting = bool(pay and pay["status"] == "initiated" and pay["checkout"] and (fc.utcnow() - pay["created_at"]) < timedelta(minutes=10))
        return render_template("order_confirmation.html", o=o, pay=pay, waiting=waiting, pay_enabled=cfg["enabled"], can_claim=can_claim,
                               paid=o["status"] == "paid", can_pay=cfg["enabled"] and o["status"] in ("pending", "contacted") and o["total"] > 0,
                               token=token, phone_default=o["phone"])

    @app.route("/order/<token>/pay", methods=["POST"])
    @h.limiter.limit("5 per minute;20 per hour")
    def order_pay(token):
        h.validate_csrf()
        cfg = config()
        back = url_for("order_confirmation", token=token)
        c = h.get_db_connection()
        try:
            o = get_order(c, token)
            if o["status"] == "paid":
                flash("This order is already paid. Thank you!", "success")
                return redirect(back)
            if o["status"] == "cancelled" or o["total"] < 1:
                flash("This order can't be paid online.", "error")
                return redirect(back)
            if not cfg["enabled"]:
                flash("Online payment isn't switched on yet. I'll contact you with payment instructions.", "error")
                return redirect(back)
            phone = msisdn(request.form.get("phone") or o["phone"])
            if not phone:
                flash("Enter the Safaricom number to pay from, like 0712 345 678.", "error")
                return redirect(back)
            recent = c.execute("SELECT 1 FROM payments WHERE order_id = %s AND status = 'initiated' AND created_at > %s",
                               (o["id"], fc.utcnow() - timedelta(seconds=90))).fetchone()
            if recent:
                flash("A payment request was just sent to your phone. Enter your M-Pesa PIN there, or wait a minute to try again.", "error")
                return redirect(back)
            # The amount is read from OUR database, never from the form: the customer cannot change what they are charged.
            pid = c.execute("INSERT INTO payments (order_id, amount_kes, phone) VALUES (%s, %s, %s) RETURNING id",
                            (o["id"], o["total"], phone)).fetchone()[0]
            c.commit()  # recorded before we call Safaricom, so a crash can never lose track of an attempt
            try:
                checkout, merchant = stk_push(cfg, o["reference"], o["total"], phone)
            except PaymentError as ex:
                c.execute("UPDATE payments SET status = 'failed', result_desc = %s, updated_at = NOW() WHERE id = %s", (str(ex)[:200], pid))
                c.commit()
                flash(str(ex), "error")
                return redirect(back)
            c.execute("UPDATE payments SET checkout_request_id = %s, merchant_request_id = %s WHERE id = %s", (checkout, merchant, pid))
            c.commit()
        finally:
            c.close()
        flash("Check your phone and enter your M-Pesa PIN to finish paying.", "success")
        return redirect(back)

    @app.route("/order/<token>/status")
    @h.limiter.limit("60 per minute")
    def order_status(token):
        c = h.get_db_connection()
        try:
            o = get_order(c, token)
            pay = last_payment(c, o["id"])
            if o["status"] != "paid" and pay and pay["status"] == "initiated":
                if not pay["checkout"]:
                    if (fc.utcnow() - pay["created_at"]).total_seconds() > 120:  # the push never left: give up on it
                        c.execute("UPDATE payments SET status = 'failed', result_desc = 'Request was never sent', updated_at = NOW() "
                                  "WHERE id = %s AND status = 'initiated'", (pay["id"],))
                        c.commit()
                else:
                    last = c.execute("SELECT last_checked FROM payments WHERE id = %s", (pay["id"],)).fetchone()[0]
                    if not last or (fc.utcnow() - last).total_seconds() >= 5:  # at most one question to Safaricom per 5 s
                        result = refresh_payment(c, pay["id"], pay["checkout"])
                        if result == "pending" and (fc.utcnow() - pay["created_at"]) > timedelta(minutes=15):
                            settle(c, pay["id"], "failed", None, "Payment request expired")
                o = get_order(c, token)
                pay = last_payment(c, o["id"])
        finally:
            c.close()
        if o["status"] == "paid":
            state, msg = "paid", "Payment confirmed. Thank you!"
        elif pay and pay["status"] == "initiated":
            state, msg = "waiting", "Waiting for you to enter your M-Pesa PIN on your phone..."
        elif pay and pay["status"] == "failed":
            state, msg = "failed", (pay["desc"] or "The payment did not go through.")
        else:
            state, msg = "none", ""
        resp = jsonify({"state": state, "message": msg})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/payments/mpesa/callback/<token>", methods=["POST"])
    def mpesa_callback(token):
        cfg = config()
        if not cfg["token"] or not hmac.compare_digest(token.encode(), cfg["token"].encode()):
            abort(404)
        ack = jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})  # Safaricom does not retry, so always acknowledge a well-formed call
        payload = request.get_json(silent=True, force=True) or {}
        try:
            cb = payload["Body"]["stkCallback"]
            checkout = str(cb["CheckoutRequestID"])[:64]
        except (KeyError, TypeError):
            return ack
        c = h.get_db_connection()
        try:
            pay = c.execute("SELECT id, amount_kes, status FROM payments WHERE checkout_request_id = %s", (checkout,)).fetchone()
            if pay and pay[2] == "initiated":
                try:
                    items = {i.get("Name"): i.get("Value") for i in (cb.get("CallbackMetadata") or {}).get("Item", [])}
                    receipt, amount = str(items.get("MpesaReceiptNumber") or ""), items.get("Amount")
                    if (int(str(cb.get("ResultCode"))) == 0 and re.fullmatch(r"[A-Za-z0-9]{8,20}", receipt)
                            and int(round(float(amount))) == pay[1]):
                        # Remembered for the records, and used only if Safaricom itself then confirms the payment.
                        c.execute("UPDATE payments SET claimed_receipt = %s WHERE id = %s AND status = 'initiated'", (receipt, pay[0]))
                        c.commit()
                except (TypeError, ValueError):
                    pass
                refresh_payment(c, pay[0], checkout)  # THE verification: ask Safaricom, believe only its answer
        except Exception:
            log.exception("error while handling an M-Pesa callback")
        finally:
            c.close()
        return ack

    # ------------------------------------------------------------ purchased downloads, receipts, claiming
    @app.route("/order/<token>/download")
    @h.limiter.limit("30 per hour")
    def order_download(token):
        c = h.get_db_connection()
        try:
            o = get_order(c, token)
            if o["status"] != "paid":
                flash("Your download unlocks as soon as your payment is confirmed.", "error")
                return redirect(url_for("order_confirmation", token=token))
            row = c.execute("SELECT file_data, file_name, file_mime, file_kind FROM products WHERE id = %s", (o["product_id"],)).fetchone() \
                if o["product_id"] else None
            if not row or not row[0]:
                flash("There is no file to download for this order. I'll deliver it to you directly.", "error")
                return redirect(url_for("order_confirmation", token=token))
            log_download(c, o["id"], o["product_id"], session.get("user_id") or o["user_id"], o["title"])
        finally:
            c.close()
        return fc.file_response(row[0], row[2], row[1], row[3], download=True)

    @app.route("/order/<token>/receipt")
    def order_receipt(token):
        c = h.get_db_connection()
        try:
            o = get_order(c, token)
            if o["status"] != "paid":
                abort(404)
            pay = c.execute("SELECT receipt, paid_at, phone, amount_kes FROM payments WHERE order_id = %s AND status = 'success' LIMIT 1", (o["id"],)).fetchone()
        finally:
            c.close()
        phone = ("*" * 6 + pay[2][-3:]) if pay and pay[2] else ""  # show only the last digits on a receipt
        return render_template("receipt.html", o=o, receipt=pay[0] if pay else "", paid_at=pay[1] if pay else None,
                               phone=phone, amount=pay[3] if pay else o["total"])

    @app.route("/order/<token>/claim", methods=["POST"])
    @h.user_required
    def order_claim(token):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            o = get_order(c, token)
            if o["user_id"] is None:
                c.execute("UPDATE orders SET user_id = %s WHERE id = %s AND user_id IS NULL", (session["user_id"], o["id"]))
                c.commit()
                flash("Order added to your account.", "success")
            elif o["user_id"] != session["user_id"]:
                flash("This order already belongs to another account.", "error")
        finally:
            c.close()
        return redirect(url_for("order_confirmation", token=token))

    # ------------------------------------------------------------ customer account pages (private to the signed-in user)
    @app.route("/account/orders")
    @h.user_required
    def account_orders():
        page = _page()
        c = h.get_db_connection()
        try:
            uid = session["user_id"]
            total = c.execute("SELECT COUNT(*) FROM orders WHERE user_id = %s", (uid,)).fetchone()[0]
            rows = c.execute(
                "SELECT o.reference, o.product_title, o.total_kes, o.status, o.created_at, o.access_token, p.slug, COALESCE(p.file_size, 0) "
                "FROM orders o LEFT JOIN products p ON p.id = o.product_id WHERE o.user_id = %s ORDER BY o.id DESC LIMIT %s OFFSET %s",
                (uid, PER_PAGE, (page - 1) * PER_PAGE)).fetchall()
            spent = c.execute("SELECT COALESCE(SUM(total_kes), 0) FROM orders WHERE user_id = %s AND status = 'paid'", (uid,)).fetchone()[0]
        finally:
            c.close()
        keys = ("reference", "title", "total", "status", "created_at", "token", "slug", "file_size")
        return render_template("account_orders.html", orders=[dict(zip(keys, r)) for r in rows], total=total, page=page, pages=_pages(total), spent=spent)

    @app.route("/account/downloads")
    @h.user_required
    def account_downloads():
        page = _page()
        c = h.get_db_connection()
        try:
            uid = session["user_id"]
            total = c.execute("SELECT COUNT(*) FROM download_log WHERE user_id = %s", (uid,)).fetchone()[0]
            rows = c.execute(
                "SELECT d.product_title, d.created_at, p.slug, o.reference, o.access_token FROM download_log d "
                "LEFT JOIN products p ON p.id = d.product_id LEFT JOIN orders o ON o.id = d.order_id "
                "WHERE d.user_id = %s ORDER BY d.id DESC LIMIT %s OFFSET %s", (uid, PER_PAGE, (page - 1) * PER_PAGE)).fetchall()
        finally:
            c.close()
        return render_template("account_downloads.html", items=[dict(zip(("title", "when", "slug", "reference", "token"), r)) for r in rows],
                               total=total, page=page, pages=_pages(total))

    # ------------------------------------------------------------ admin: payment records
    @app.route("/admin/payments")
    @h.admin_required
    def admin_payments():
        from .business import date_filter  # shared 'from/to' helper
        a = request.args
        where, params = [], []
        q = a.get("q", "").strip()[:60]
        if q:
            where.append("(pm.receipt ILIKE %s OR o.reference ILIKE %s OR pm.phone ILIKE %s OR o.customer_name ILIKE %s)")
            params += [_like(q)] * 4
        if a.get("status") in PAY_STATUS_LABEL:
            where.append("pm.status = %s")
            params.append(a["status"])
        date_filter("pm.created_at", a, where, params)
        sql_where = (" WHERE " + " AND ".join(where)) if where else ""
        page = _page()
        c = h.get_db_connection()
        try:
            base = " FROM payments pm JOIN orders o ON o.id = pm.order_id" + sql_where
            total = c.execute("SELECT COUNT(*)" + base, tuple(params)).fetchone()[0]
            shown_sum = c.execute("SELECT COALESCE(SUM(CASE WHEN pm.status = 'success' THEN pm.amount_kes ELSE 0 END), 0)" + base, tuple(params)).fetchone()[0]
            rows = c.execute("SELECT pm.id, pm.created_at, o.reference, o.product_title, pm.amount_kes, pm.phone, pm.status, pm.receipt, pm.result_desc, "
                             "pm.checkout_request_id, o.customer_name" + base + " ORDER BY pm.id DESC LIMIT %s OFFSET %s",
                             tuple(params) + (PER_PAGE, (page - 1) * PER_PAGE)).fetchall()
            revenue = verified_revenue(c)
            review = c.execute("SELECT COUNT(*) FROM payments WHERE status = 'duplicate'").fetchone()[0]
        finally:
            c.close()
        keys = ("id", "created_at", "reference", "title", "amount", "phone", "status", "receipt", "desc", "checkout", "name")
        cfg = config()
        return render_template("admin_payments.html", payments=[dict(zip(keys, r)) for r in rows], total=total, page=page, pages=_pages(total),
                               f=a, shown_sum=shown_sum, revenue=revenue, review=review, statuses=list(PAY_STATUS_LABEL.items()),
                               cfg={"enabled": cfg["enabled"], "env": cfg["env"], "callback_url": cfg["callback_url"].replace(cfg["token"], "<secret>") if cfg["token"] else "",
                                    "missing": [n for n, k in (("MPESA_CONSUMER_KEY", "key"), ("MPESA_CONSUMER_SECRET", "secret"), ("MPESA_SHORTCODE", "shortcode"),
                                                               ("MPESA_PASSKEY", "passkey"), ("SITE_URL (https)", "site")) if not cfg[k]]
                                              + ([] if len(cfg["token"]) >= 16 else ["MPESA_CALLBACK_TOKEN (16+ characters)"])})

    @app.route("/admin/payments/<int:pid>/recheck", methods=["POST"])
    @h.admin_required
    @h.limiter.limit("30 per minute")
    def admin_payment_recheck(pid):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            row = c.execute("SELECT checkout_request_id, status FROM payments WHERE id = %s", (pid,)).fetchone()
            if not row:
                abort(404)
            if row[1] != "initiated":
                flash("That payment is already settled (%s)." % PAY_STATUS_LABEL.get(row[1], row[1]), "error")
            else:
                result = refresh_payment(c, pid, row[0])
                flash({"paid": "Safaricom confirmed the payment. The order is now paid.", "failed": "Safaricom says the payment did not go through.",
                       "pending": "Safaricom says it is still being processed.", "duplicate": "Verified, but the order was already paid: refund this one."}
                      .get(result, "Could not reach Safaricom to verify. Try again shortly."), "success" if result in ("paid", "pending") else "error")
        finally:
            c.close()
        return redirect(h.safe_next(request.form.get("next"), url_for("admin_payments")))

    # ------------------------------------------------------------ admin: customer accounts and password-reset links
    @app.route("/admin/users")
    @h.admin_required
    def admin_users():
        q = request.args.get("q", "").strip()[:30]
        where, params = "", ()
        if q:
            where, params = " WHERE u.username ILIKE %s", (_like(q),)
        page = _page()
        c = h.get_db_connection()
        try:
            total = c.execute("SELECT COUNT(*) FROM users u" + where, params).fetchone()[0]
            rows = c.execute("SELECT u.id, u.username, u.created_at, (SELECT COUNT(*) FROM orders o WHERE o.user_id = u.id), "
                             "(SELECT COUNT(*) FROM orders o WHERE o.user_id = u.id AND o.status = 'paid') FROM users u" + where +
                             " ORDER BY u.id DESC LIMIT %s OFFSET %s", params + (PER_PAGE, (page - 1) * PER_PAGE)).fetchall()
        finally:
            c.close()
        return render_template("admin_users.html", users=[dict(zip(("id", "username", "joined", "orders", "paid"), r)) for r in rows],
                               total=total, page=page, pages=_pages(total), f=request.args)

    @app.route("/admin/users/<int:uid>/reset-link", methods=["POST"])
    @h.admin_required
    @h.limiter.limit("20 per hour")
    def admin_reset_link(uid):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            u = c.execute("SELECT username FROM users WHERE id = %s", (uid,)).fetchone()
            if not u:
                abort(404)
            raw = secrets.token_urlsafe(32)
            c.execute("UPDATE password_resets SET used_at = NOW() WHERE user_id = %s AND used_at IS NULL", (uid,))  # only the newest link works
            c.execute("INSERT INTO password_resets (user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
                      (uid, hashlib.sha256(raw.encode()).hexdigest(), fc.utcnow() + timedelta(minutes=RESET_MINUTES)))
            c.commit()
        finally:
            c.close()
        resp = app.make_response(render_template("admin_reset_link.html", username=u[0], link=url_for("password_reset", token=raw, _external=True),
                                                 minutes=RESET_MINUTES))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/reset/<token>", methods=["GET", "POST"])
    @h.limiter.limit("10 per minute", methods=["POST"])
    def password_reset(token):
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", token):
            abort(404)
        digest = hashlib.sha256(token.encode()).hexdigest()
        c = h.get_db_connection()
        try:
            row = c.execute("SELECT r.id, r.user_id, u.username FROM password_resets r JOIN users u ON u.id = r.user_id "
                            "WHERE r.token_hash = %s AND r.used_at IS NULL AND r.expires_at > %s", (digest, fc.utcnow())).fetchone()
            if not row:
                return render_template("password_reset.html", valid=False, username=""), 410
            if request.method == "POST":
                h.validate_csrf()
                new, again = request.form.get("new_password", ""), request.form.get("confirm_password", "")
                if len(new) < 8 or len(new) > 128:
                    flash("The new password must be 8-128 characters.", "error")
                elif new != again:
                    flash("The two passwords don't match.", "error")
                else:
                    from werkzeug.security import generate_password_hash
                    used = c.execute("UPDATE password_resets SET used_at = NOW() WHERE id = %s AND used_at IS NULL", (row[0],)).rowcount
                    if used:
                        c.execute("UPDATE users SET password_hash = %s WHERE id = %s", (generate_password_hash(new), row[1]))
                        c.commit()
                        session.clear()
                        flash("Password changed. Please sign in with your new password.", "success")
                        return redirect(url_for("signin"))
                return render_template("password_reset.html", valid=True, username=row[2]), 400
        finally:
            c.close()
        return render_template("password_reset.html", valid=True, username=row[2])

    @app.after_request
    def no_cache_private(resp):
        ep = request.endpoint or ""
        if ep in ("order_status", "order_pay", "order_download", "order_receipt", "account_orders", "account_downloads", "password_reset"):
            resp.headers["Cache-Control"] = "no-store"
        return resp
