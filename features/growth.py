"""Business features, phase 3: online tutoring bookings, advertising and sponsorship enquiries, newsletter,
announcements, and the WhatsApp / social links used across the site.

Notes
- Like the Hire Me form, tutoring and advertising are REQUESTS: nothing is charged or promised by the form. The owner
  reviews each one in the admin area and replies.
- Prices are the owner's decision, so none are invented: add them to LESSON_PRICE_KES to show "From KES ...".
- There is no email-sending service in this site. Newsletter addresses are stored (with a personal unsubscribe link each)
  and exported from the admin area to be mailed from a proper email tool.
"""
import csv
import io
import os
import re
import secrets
from datetime import datetime, timedelta

from flask import render_template, request, redirect, url_for, session, flash, abort, Response

from . import common as fc
from .business import CURRENCY, EMAIL_RE, PHONE_RE, like, date_filter, page_count

# ---------------------------------------------------------------- settings you may want to edit
DEFAULT_CHANNEL = "https://whatsapp.com/channel/0029VbEEsiAHVvTk3uyqwg08"  # override with the WHATSAPP_CHANNEL_URL setting
SOCIALS = [("Facebook", "FACEBOOK_URL"), ("Instagram", "INSTAGRAM_URL"), ("X", "X_URL"), ("YouTube", "YOUTUBE_URL"),
           ("TikTok", "TIKTOK_URL"), ("LinkedIn", "LINKEDIN_URL"), ("GitHub", "GITHUB_URL")]  # each shown only if you set its setting

LESSON_PRICE_KES = {}  # e.g. {"Python programming": 1500} shows "From KES 1,500 per session"; others show "Price on request"
IN_PERSON_NOTE = "In-person sessions depend on location and availability. Choose 'In person' or 'Either' and I'll tell you what is possible."
LESSONS = [
    ("Python programming", "Learn to think like a programmer, from your first line of code to small working programs.",
     ["Write and run your own Python programs", "Use variables, loops, conditions and functions", "Build a small project and learn to debug it"]),
    ("HTML and CSS", "Build and style real web pages that look good on phones and computers.",
     ["Structure a page with HTML", "Style it with CSS and responsive layouts", "Put a simple site together"]),
    ("Flask web development", "Turn Python into working websites with pages, forms and a database.",
     ["Create routes and templates", "Handle forms and data safely", "Understand how a Flask app is deployed"]),
    ("Excel", "Get comfortable with spreadsheets for work, study and small businesses.",
     ["Use formulas, sorting and filters", "Make charts and pivot tables", "Clean and analyse a small data set"]),
    ("Basic statistics", "Understand data: averages, spread, probability and how to read results.",
     ["Calculate and explain averages and spread", "Read and draw common charts", "Interpret simple results with care"]),
    ("Mathematics", "Strengthen your foundations and your exam confidence, one step at a time.",
     ["Work through algebra, geometry and problem solving", "Practise exam-style questions", "Show clear step-by-step working"]),
]
SUBJECTS = [l[0] for l in LESSONS]
LEVELS = ["Beginner", "Intermediate", "Advanced"]
MODES = [("online", "Online"), ("in_person", "In person"), ("either", "Either")]
TIMES = ["Morning", "Afternoon", "Evening", "Flexible"]
DURATIONS = [30, 60, 90]
BOOKING_STATUS = [("pending", "Pending"), ("confirmed", "Confirmed"), ("rescheduled", "Rescheduled"), ("cancelled", "Cancelled")]
BOOKING_LABEL, MODE_LABEL = dict(BOOKING_STATUS), dict(MODES)

PLACEMENTS = [
    ("Sponsored educational content", "A clearly labelled tutorial, guide or lesson from your organisation, published in the Marketplace."),
    ("Sponsored marketplace listing", "Your product or service shown in the Marketplace with a \"Sponsored\" label."),
    ("Home page sponsor spotlight", "A labelled spotlight for your brand on the home page."),
    ("WhatsApp Channel mention", "A labelled sponsor message to the Felix KE Ideas WhatsApp Channel audience."),
    ("Something else / not sure", "Tell me what you have in mind and we will shape it together."),
]
PLACEMENT_NAMES = [p[0] for p in PLACEMENTS]
AD_BUDGETS = ["Not sure yet", "Under %s 5,000" % CURRENCY, "%s 5,000 - 20,000" % CURRENCY, "%s 20,000 - 50,000" % CURRENCY, "Over %s 50,000" % CURRENCY]
AD_STATUS = [("new", "New"), ("reviewing", "Reviewing"), ("approved", "Approved"), ("declined", "Declined")]
AD_LABEL = dict(AD_STATUS)
PER_PAGE = 15


# ---------------------------------------------------------------- helpers
def today():
    return fc.utcnow().astimezone(fc.SITE_TZ).date()


def parse_date(value):
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def social_context():
    ch = os.getenv("WHATSAPP_CHANNEL_URL", DEFAULT_CHANNEL).strip()
    if not ch.startswith("https://whatsapp.com/channel/") and not ch.startswith("https://www.whatsapp.com/channel/"):
        ch = ""
    links = []
    for label, env in SOCIALS:
        url = os.getenv(env, "").strip()
        if url.startswith("https://") and " " not in url and len(url) <= 200:
            links.append((label, url))
    return {"channel_url": ch, "social_links": links}


def latest_updates(c, n=3):
    try:
        rows = c.execute("SELECT id, title, body, link, created_at FROM announcements WHERE published ORDER BY id DESC LIMIT %s", (n,)).fetchall()
    except Exception:
        return []
    return [dict(zip(("id", "title", "body", "link", "created_at"), r)) for r in rows]


def admin_overview(c):
    one = lambda sql: c.execute(sql).fetchone()[0]
    return {"bookings_pending": one("SELECT COUNT(*) FROM tutoring_bookings WHERE status = 'pending'"),
            "ads_new": one("SELECT COUNT(*) FROM ad_inquiries WHERE NOT is_read"),
            "subscribers": one("SELECT COUNT(*) FROM newsletter_subscribers")}


def csv_safe(v):
    v = str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v  # stops spreadsheet formula injection


# ---------------------------------------------------------------- routes
def register(app, h):
    @app.context_processor
    def social_links():
        return social_context()

    @app.template_filter("fmt_date")
    def fmt_date(d, fmt="%a %d %b %Y"):
        if not d:
            return ""
        if isinstance(d, str):
            d = parse_date(d[:10])
        return d.strftime(fmt) if d else ""

    @app.template_filter("iso_date")
    def iso_date(d):
        if not d:
            return ""
        return d[:10] if isinstance(d, str) else d.isoformat()

    app.jinja_env.globals.update(BOOKING_LABEL=BOOKING_LABEL, AD_LABEL=AD_LABEL, MODE_LABEL=MODE_LABEL)

    def page():
        try:
            return max(1, min(int(request.args.get("page", "1")), 100000))
        except ValueError:
            return 1

    def thanks(title, message, buttons):
        return render_template("thanks.html", title=title, message=message, buttons=buttons)

    # ------------------------------------------------------------ tutoring
    def lessons_ctx():
        return [{"title": t, "text": d, "points": pts, "price": LESSON_PRICE_KES.get(t)} for t, d, pts in LESSONS]

    @app.route("/tutoring", methods=["GET", "POST"])
    @h.limiter.limit("5 per minute;20 per hour", methods=["POST"])
    def tutoring():
        form, errors = {}, {}
        if request.method == "POST":
            h.validate_csrf()
            if request.form.get("website"):  # honeypot
                return redirect(url_for("tutoring_thanks"))
            f = form = request.form
            v = {"name": h.clean_text(f.get("name"), 100), "email": f.get("email", "").strip(), "phone": h.clean_text(f.get("phone"), 25),
                 "subject": f.get("subject", ""), "level": f.get("level", ""), "mode": f.get("mode", ""),
                 "time": f.get("preferred_time", ""), "notes": (f.get("notes") or "").replace("\r\n", "\n").strip()}
            if len(v["name"]) < 2:
                errors["name"] = "Please enter your name."
            if not EMAIL_RE.match(v["email"]) or len(v["email"]) > 254:
                errors["email"] = "Please enter a valid email address, like you@example.com."
            if v["phone"] and not PHONE_RE.match(v["phone"]):
                errors["phone"] = "Use digits only, for example 0712 345 678 or +254712345678."
            if v["subject"] not in SUBJECTS:
                errors["subject"] = "Choose what you want to learn."
            if v["level"] not in LEVELS:
                errors["level"] = "Choose your level (Beginner is fine)."
            if v["mode"] not in MODE_LABEL:
                errors["mode"] = "Choose online, in person or either."
            if v["time"] and v["time"] not in TIMES:
                errors["preferred_time"] = "Choose one of the options."
            try:
                duration = int(f.get("duration", "60"))
            except ValueError:
                duration = 0
            if duration not in DURATIONS:
                errors["duration"] = "Choose a session length."
            pref = None
            if f.get("preferred_date", "").strip():
                pref = parse_date(f.get("preferred_date"))
                if not pref or pref < today() or pref > today() + timedelta(days=366):
                    errors["preferred_date"] = "Pick a date from today up to a year ahead, or leave it empty."
            if len(v["notes"]) > 1000:
                errors["notes"] = "Please keep the notes under 1000 characters."
            if not errors:
                c = h.get_db_connection()
                try:
                    c.execute("INSERT INTO tutoring_bookings (user_id, name, email, phone, subject, level, mode, preferred_date, preferred_time, "
                              "duration_min, notes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                              (session.get("user_id"), v["name"], v["email"], v["phone"], v["subject"], v["level"], v["mode"], pref,
                               v["time"], duration, v["notes"]))
                    c.commit()
                finally:
                    c.close()
                session["tutor_name"] = v["name"]
                return redirect(url_for("tutoring_thanks"))
            flash("Please fix the highlighted fields.", "error")
        else:
            want = request.args.get("subject", "")
            form = {"subject": want} if want in SUBJECTS else {}
        return render_template("tutoring.html", lessons=lessons_ctx(), form=form, errors=errors, levels=LEVELS, modes=MODES, times=TIMES,
                               durations=DURATIONS, subjects=SUBJECTS, in_person_note=IN_PERSON_NOTE, min_date=today().isoformat(),
                               max_date=(today() + timedelta(days=366)).isoformat()), (400 if errors else 200)

    @app.route("/tutoring/thanks")
    def tutoring_thanks():
        name = session.pop("tutor_name", None)
        return thanks("Thank you%s." % (", " + name if name else ""),
                      "Your tutoring request has been received. I'll reply by email with a time that works, or to ask a question first. Nothing is charged by this form.",
                      [(url_for("learn"), "Browse learning resources", False), (url_for("index"), "Back to home", True)])

    # ------------------------------------------------------------ advertising
    @app.route("/advertise", methods=["GET", "POST"])
    @h.limiter.limit("5 per minute;20 per hour", methods=["POST"])
    def advertise():
        form, errors = {}, {}
        if request.method == "POST":
            h.validate_csrf()
            if request.form.get("website_url_hp"):  # honeypot
                return redirect(url_for("advertise_thanks"))
            f = form = request.form
            v = {"name": h.clean_text(f.get("name"), 100), "org": h.clean_text(f.get("organisation"), 120), "email": f.get("email", "").strip(),
                 "site": f.get("website", "").strip(), "placement": f.get("placement", ""), "budget": f.get("budget", ""),
                 "description": (f.get("description") or "").replace("\r\n", "\n").strip()}
            if len(v["name"]) < 2:
                errors["name"] = "Please enter your name."
            if len(v["org"]) < 2:
                errors["organisation"] = "Please enter your organisation or business name."
            if not EMAIL_RE.match(v["email"]) or len(v["email"]) > 254:
                errors["email"] = "Please enter a valid email address, like you@example.com."
            if v["site"] and not (re.match(r"^https?://[^\s]+\.[^\s]+$", v["site"]) and len(v["site"]) <= 200):
                errors["website"] = "Enter the full address, starting with https://"
            if v["placement"] not in PLACEMENT_NAMES:
                errors["placement"] = "Choose the kind of placement you are interested in."
            if v["budget"] not in AD_BUDGETS:
                errors["budget"] = "Choose a budget range (or 'Not sure yet')."
            if len(v["description"]) < 30:
                errors["description"] = "Tell me about your campaign (at least 30 characters)."
            elif len(v["description"]) > 3000:
                errors["description"] = "Please keep it under 3000 characters."
            if not errors:
                c = h.get_db_connection()
                try:
                    c.execute("INSERT INTO ad_inquiries (name, organisation, email, website, placement, budget_range, description) "
                              "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                              (v["name"], v["org"], v["email"], v["site"], v["placement"], v["budget"], v["description"]))
                    c.commit()
                finally:
                    c.close()
                session["ad_name"] = v["name"]
                return redirect(url_for("advertise_thanks"))
            flash("Please fix the highlighted fields.", "error")
        else:
            want = request.args.get("placement", "")
            form = {"placement": want} if want in PLACEMENT_NAMES else {}
        return render_template("advertise.html", placements=PLACEMENTS, placement_names=PLACEMENT_NAMES, budgets=AD_BUDGETS, form=form,
                               errors=errors), (400 if errors else 200)

    @app.route("/advertise/thanks")
    def advertise_thanks():
        name = session.pop("ad_name", None)
        return thanks("Thank you%s." % (", " + name if name else ""),
                      "Your advertising enquiry has been received. I review every request and will reply by email. Nothing is booked or charged until we both agree.",
                      [(url_for("index"), "Back to home", True)])

    # ------------------------------------------------------------ newsletter
    @app.route("/newsletter", methods=["POST"])
    @h.limiter.limit("5 per minute;30 per hour")
    def newsletter_subscribe():
        h.validate_csrf()
        back = h.safe_next(request.form.get("next"), url_for("index"))
        if request.form.get("company"):  # honeypot
            return redirect(back)
        email = request.form.get("email", "").strip()
        if not EMAIL_RE.match(email) or len(email) > 254:
            flash("Please enter a valid email address to subscribe.", "error")
            return redirect(back)
        c = h.get_db_connection()
        try:
            if not c.execute("SELECT 1 FROM newsletter_subscribers WHERE lower(email) = lower(%s)", (email,)).fetchone():
                try:
                    c.execute("INSERT INTO newsletter_subscribers (email, token) VALUES (%s, %s)", (email, secrets.token_urlsafe(24)))
                    c.commit()
                except Exception:  # two clicks at once: the unique index already has this address
                    c.rollback()
        finally:
            c.close()
        # The same message whether or not the address was already on the list, so the form cannot be used to check who is subscribed.
        flash("Thanks! You're on the list. You can unsubscribe at any time.", "success")
        return redirect(back)

    @app.route("/newsletter/unsubscribe/<token>", methods=["GET", "POST"])
    @h.limiter.limit("10 per minute", methods=["POST"])
    def newsletter_unsubscribe(token):
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,40}", token):
            abort(404)
        if request.method == "POST":  # a button, not a plain link, so email scanners that open links cannot unsubscribe people
            h.validate_csrf()
            c = h.get_db_connection()
            try:
                c.execute("DELETE FROM newsletter_subscribers WHERE token = %s", (token,))
                c.commit()
            finally:
                c.close()
            return thanks("You're unsubscribed.", "Your email address has been removed from the list.", [(url_for("index"), "Back to home", True)])
        return render_template("unsubscribe.html", token=token)

    # ------------------------------------------------------------ announcements
    @app.route("/updates")
    def updates():
        pg = page()
        c = h.get_db_connection()
        try:
            total = c.execute("SELECT COUNT(*) FROM announcements WHERE published").fetchone()[0]
            rows = c.execute("SELECT id, title, body, link, created_at FROM announcements WHERE published ORDER BY id DESC LIMIT %s OFFSET %s",
                             (10, (pg - 1) * 10)).fetchall()
        finally:
            c.close()
        return render_template("updates.html", items=[dict(zip(("id", "title", "body", "link", "created_at"), r)) for r in rows],
                               page=pg, pages=page_count(total, 10))

    # ------------------------------------------------------------ admin: tutoring bookings
    BK_COLS = ("id, name, email, phone, subject, level, mode, preferred_date, preferred_time, duration_min, notes, status, session_date, "
               "session_time, admin_notes, is_read, created_at")
    BK_KEYS = ("id", "name", "email", "phone", "subject", "level", "mode", "pref_date", "pref_time", "duration", "notes", "status",
               "session_date", "session_time", "admin_notes", "is_read", "created_at")

    @app.route("/admin/bookings")
    @h.admin_required
    def admin_bookings():
        a = request.args
        where, params = [], []
        q = a.get("q", "").strip()[:60]
        if q:
            where.append("(name ILIKE %s OR email ILIKE %s OR subject ILIKE %s)")
            params += [like(q)] * 3
        if a.get("status") in BOOKING_LABEL:
            where.append("status = %s")
            params.append(a["status"])
        if a.get("subject") in SUBJECTS:
            where.append("subject = %s")
            params.append(a["subject"])
        if a.get("unread"):
            where.append("NOT is_read")
        date_filter("created_at", a, where, params)
        sw = (" WHERE " + " AND ".join(where)) if where else ""
        pg = page()
        c = h.get_db_connection()
        try:
            total = c.execute("SELECT COUNT(*) FROM tutoring_bookings" + sw, tuple(params)).fetchone()[0]
            rows = c.execute("SELECT " + BK_COLS + " FROM tutoring_bookings" + sw + " ORDER BY id DESC LIMIT %s OFFSET %s",
                             tuple(params) + (PER_PAGE, (pg - 1) * PER_PAGE)).fetchall()
        finally:
            c.close()
        return render_template("admin_bookings.html", items=[dict(zip(BK_KEYS, r)) for r in rows], total=total, page=pg,
                               pages=page_count(total, PER_PAGE), f=a, statuses=BOOKING_STATUS, subjects=SUBJECTS)

    @app.route("/admin/bookings/<int:bid>")
    @h.admin_required
    def admin_booking(bid):
        c = h.get_db_connection()
        try:
            row = c.execute("SELECT " + BK_COLS + " FROM tutoring_bookings WHERE id = %s", (bid,)).fetchone()
            if not row:
                abort(404)
            if not row[15]:
                c.execute("UPDATE tutoring_bookings SET is_read = TRUE WHERE id = %s", (bid,))
                c.commit()
        finally:
            c.close()
        return render_template("admin_booking.html", b=dict(zip(BK_KEYS, row)), statuses=BOOKING_STATUS)

    @app.route("/admin/bookings/<int:bid>/update", methods=["POST"])
    @h.admin_required
    def admin_booking_update(bid):
        h.validate_csrf()
        status = request.form.get("status", "")
        if status not in BOOKING_LABEL:
            abort(400, description="Choose a valid status.")
        sdate = parse_date(request.form.get("session_date"))
        stime = h.clean_text(request.form.get("session_time"), 30)
        notes = (request.form.get("notes") or "").replace("\r\n", "\n").strip()[:1000]
        if status in ("confirmed", "rescheduled") and not sdate:
            flash("Pick the session date before confirming or rescheduling.", "error")
            return redirect(url_for("admin_booking", bid=bid))
        c = h.get_db_connection()
        try:
            n = c.execute("UPDATE tutoring_bookings SET status = %s, session_date = %s, session_time = %s, admin_notes = %s, is_read = TRUE, "
                          "updated_at = NOW() WHERE id = %s", (status, sdate, stime, notes, bid)).rowcount
            c.commit()
        finally:
            c.close()
        if not n:
            abort(404)
        flash("Booking updated.", "success")
        return redirect(url_for("admin_booking", bid=bid))

    @app.route("/admin/bookings/<int:bid>/read", methods=["POST"])
    @h.admin_required
    def admin_booking_read(bid):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            c.execute("UPDATE tutoring_bookings SET is_read = NOT is_read WHERE id = %s", (bid,))
            c.commit()
        finally:
            c.close()
        return redirect(h.safe_next(request.form.get("next"), url_for("admin_bookings")))

    # ------------------------------------------------------------ admin: advertising enquiries
    AD_COLS = "id, name, organisation, email, website, placement, budget_range, description, status, admin_notes, is_read, created_at"
    AD_KEYS = ("id", "name", "org", "email", "website", "placement", "budget", "description", "status", "notes", "is_read", "created_at")

    @app.route("/admin/ads")
    @h.admin_required
    def admin_ads():
        a = request.args
        where, params = [], []
        q = a.get("q", "").strip()[:60]
        if q:
            where.append("(name ILIKE %s OR organisation ILIKE %s OR email ILIKE %s OR description ILIKE %s)")
            params += [like(q)] * 4
        if a.get("status") in AD_LABEL:
            where.append("status = %s")
            params.append(a["status"])
        if a.get("placement") in PLACEMENT_NAMES:
            where.append("placement = %s")
            params.append(a["placement"])
        if a.get("unread"):
            where.append("NOT is_read")
        date_filter("created_at", a, where, params)
        sw = (" WHERE " + " AND ".join(where)) if where else ""
        pg = page()
        c = h.get_db_connection()
        try:
            total = c.execute("SELECT COUNT(*) FROM ad_inquiries" + sw, tuple(params)).fetchone()[0]
            rows = c.execute("SELECT " + AD_COLS + " FROM ad_inquiries" + sw + " ORDER BY id DESC LIMIT %s OFFSET %s",
                             tuple(params) + (PER_PAGE, (pg - 1) * PER_PAGE)).fetchall()
        finally:
            c.close()
        return render_template("admin_ads.html", items=[dict(zip(AD_KEYS, r)) for r in rows], total=total, page=pg,
                               pages=page_count(total, PER_PAGE), f=a, statuses=AD_STATUS, placements=PLACEMENT_NAMES)

    @app.route("/admin/ads/<int:aid>")
    @h.admin_required
    def admin_ad(aid):
        c = h.get_db_connection()
        try:
            row = c.execute("SELECT " + AD_COLS + " FROM ad_inquiries WHERE id = %s", (aid,)).fetchone()
            if not row:
                abort(404)
            if not row[10]:
                c.execute("UPDATE ad_inquiries SET is_read = TRUE WHERE id = %s", (aid,))
                c.commit()
        finally:
            c.close()
        return render_template("admin_ad.html", i=dict(zip(AD_KEYS, row)), statuses=AD_STATUS)

    @app.route("/admin/ads/<int:aid>/update", methods=["POST"])
    @h.admin_required
    def admin_ad_update(aid):
        h.validate_csrf()
        status = request.form.get("status", "")
        if status not in AD_LABEL:
            abort(400, description="Choose a valid status.")
        notes = (request.form.get("notes") or "").replace("\r\n", "\n").strip()[:1000]
        c = h.get_db_connection()
        try:
            n = c.execute("UPDATE ad_inquiries SET status = %s, admin_notes = %s, is_read = TRUE, updated_at = NOW() WHERE id = %s",
                          (status, notes, aid)).rowcount
            c.commit()
        finally:
            c.close()
        if not n:
            abort(404)
        flash("Enquiry updated.", "success")
        return redirect(url_for("admin_ad", aid=aid))

    @app.route("/admin/ads/<int:aid>/read", methods=["POST"])
    @h.admin_required
    def admin_ad_read(aid):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            c.execute("UPDATE ad_inquiries SET is_read = NOT is_read WHERE id = %s", (aid,))
            c.commit()
        finally:
            c.close()
        return redirect(h.safe_next(request.form.get("next"), url_for("admin_ads")))

    # ------------------------------------------------------------ admin: announcements and newsletter
    @app.route("/admin/updates")
    @h.admin_required
    def admin_updates():
        pg = page()
        c = h.get_db_connection()
        try:
            total = c.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]
            rows = c.execute("SELECT id, title, body, link, created_at, published FROM announcements ORDER BY id DESC LIMIT %s OFFSET %s",
                             (PER_PAGE, (pg - 1) * PER_PAGE)).fetchall()
        finally:
            c.close()
        return render_template("admin_updates.html", items=[dict(zip(("id", "title", "body", "link", "created_at", "published"), r)) for r in rows],
                               total=total, page=pg, pages=page_count(total, PER_PAGE), form={})

    @app.route("/admin/updates/new", methods=["POST"])
    @h.admin_required
    def admin_update_new():
        h.validate_csrf()
        title = h.clean_text(request.form.get("title"), 120)
        body = (request.form.get("body") or "").replace("\r\n", "\n").strip()[:1000]
        link = request.form.get("link", "").strip()
        if len(title) < 3:
            flash("Give the announcement a title (at least 3 characters).", "error")
        elif link and not ((link.startswith("/") and not link.startswith("//")) or (link.startswith("https://") and " " not in link)) or len(link) > 200:
            flash("The link must be a page on this site (starting with /) or a full https:// address.", "error")
        else:
            c = h.get_db_connection()
            try:
                c.execute("INSERT INTO announcements (title, body, link, published) VALUES (%s, %s, %s, %s)",
                          (title, body, link, "unpublished" not in request.form))
                c.commit()
            finally:
                c.close()
            flash("Announcement posted.", "success")
        return redirect(url_for("admin_updates"))

    @app.route("/admin/updates/<int:uid>/publish", methods=["POST"])
    @h.admin_required
    def admin_update_toggle(uid):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            c.execute("UPDATE announcements SET published = NOT published WHERE id = %s", (uid,))
            c.commit()
        finally:
            c.close()
        return redirect(h.safe_next(request.form.get("next"), url_for("admin_updates")))

    @app.route("/admin/updates/<int:uid>/delete", methods=["POST"])
    @h.admin_required
    def admin_update_delete(uid):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            c.execute("DELETE FROM announcements WHERE id = %s", (uid,))
            c.commit()
        finally:
            c.close()
        flash("Announcement deleted.", "success")
        return redirect(url_for("admin_updates"))

    @app.route("/admin/subscribers")
    @h.admin_required
    def admin_subscribers():
        q = request.args.get("q", "").strip()[:60]
        where, params = ("", ()) if not q else (" WHERE email ILIKE %s", (like(q),))
        pg = page()
        c = h.get_db_connection()
        try:
            total = c.execute("SELECT COUNT(*) FROM newsletter_subscribers" + where, params).fetchone()[0]
            rows = c.execute("SELECT id, email, created_at FROM newsletter_subscribers" + where + " ORDER BY id DESC LIMIT %s OFFSET %s",
                             params + (PER_PAGE, (pg - 1) * PER_PAGE)).fetchall()
        finally:
            c.close()
        return render_template("admin_subscribers.html", items=[dict(zip(("id", "email", "created_at"), r)) for r in rows], total=total,
                               page=pg, pages=page_count(total, PER_PAGE), f=request.args)

    @app.route("/admin/subscribers.csv")
    @h.admin_required
    def admin_subscribers_csv():
        c = h.get_db_connection()
        try:
            rows = c.execute("SELECT email, created_at, token FROM newsletter_subscribers ORDER BY id").fetchall()
        finally:
            c.close()
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["email", "subscribed_at", "unsubscribe_link"])
        for email, created, token in rows:
            w.writerow([csv_safe(email), created.strftime("%Y-%m-%d %H:%M") if hasattr(created, "strftime") else created,
                        url_for("newsletter_unsubscribe", token=token, _external=True)])
        return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=subscribers.csv"})

    @app.route("/admin/subscribers/<int:sid>/delete", methods=["POST"])
    @h.admin_required
    def admin_subscriber_delete(sid):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            c.execute("DELETE FROM newsletter_subscribers WHERE id = %s", (sid,))
            c.commit()
        finally:
            c.close()
        flash("Subscriber removed.", "success")
        return redirect(h.safe_next(request.form.get("next"), url_for("admin_subscribers")))

    @app.after_request
    def private_growth_pages(resp):
        if (request.endpoint or "").startswith("newsletter_unsub"):
            resp.headers["Cache-Control"] = "no-store"
        return resp
