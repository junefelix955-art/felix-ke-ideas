"""Business features, phase 1: Marketplace, digital learning resources, Hire Me, and their admin screens.

Design notes
- ONE table (`products`) holds everything that can be sold or given away. `kind` says what it is:
  resource (a learning material, also listed on /learn), product (e.g. a website template) or service.
  price_kes = 0 means free. The Marketplace and the Learn page are two views of the same table.
- Phase 1 takes ORDER REQUESTS only. Nothing is marked paid here. Premium downloads stay locked until
  phase 2 adds verified payments, so no one can get a paid file for free.
- Files are stored in the database (the host's disk is wiped on every deploy). Lists never load file bytes.
"""
import io
import os
import re
import secrets
import time
import zipfile
from datetime import datetime, timedelta, timezone

from flask import render_template, request, redirect, url_for, session, flash, abort, Response

from . import common as fc
from . import payments

# ---------------------------------------------------------------- settings you may want to edit
CURRENCY = (os.getenv("CURRENCY", "KES").strip() or "KES")[:5]

MARKET_CATEGORIES = [
    "Programming resources",
    "Mathematics and statistics materials",
    "Website templates",
    "Excel and data analysis guides",
    "Digital learning materials",
    "Website development services",
]
RESOURCE_TOPICS = ["Python", "HTML and CSS", "Flask", "Excel", "Statistics", "Mathematics", "Data analysis"]
KINDS = [("resource", "Learning resource"), ("product", "Digital product"), ("service", "Service")]
AVAILABILITY = [("available", "Available"), ("limited", "Limited availability"), ("unavailable", "Currently unavailable")]
KIND_LABEL, AVAIL_LABEL = dict(KINDS), dict(AVAILABILITY)

# Hire Me page. Prices are YOUR decision, so none are invented: add one here to show "From KES ..." on a card,
# e.g. STARTING_PRICE_KES = {"Landing page": 8000}. Services without a price show "Request a quote".
STARTING_PRICE_KES = {}
SERVICES = [
    ("Personal portfolio website", "A clean, mobile-friendly site that presents your work, skills and contact details.",
     ["Responsive design", "Contact form", "Help with hosting"]),
    ("Business website", "A professional site for a small business: services, about, contact and a clear call to action.",
     ["Multi-page layout", "Enquiry form", "Search-friendly structure"]),
    ("Landing page", "One focused page to promote a product, event or offer and collect enquiries.",
     ["Single page", "Clear call to action", "Fast loading"]),
    ("Educational website", "Sites for schools, tutors and courses, with resources, sign-ups and simple dashboards.",
     ["Learning resources", "Accounts if needed", "Easy to update"]),
    ("Simple Flask application", "A small web app with a database: forms, lists, logins or a dashboard.",
     ["Python and Flask", "PostgreSQL database", "Secure forms"]),
    ("Website redesign and maintenance", "Give an existing site a fresh, responsive look, or keep it fixed and up to date.",
     ["Modern redesign", "Bug fixes", "Ongoing updates"]),
]
EXAMPLE_PROJECTS = [  # Add your real client or portfolio projects here (title, description, link or "")
    ("Felix KE Ideas", "This website: a Flask and PostgreSQL platform with member accounts, a community, live chat, a study "
                       "library, a maths problem solver and the Maths Sprint game.", ""),
]
PROJECT_TYPES = [s[0] for s in SERVICES] + ["Other"]
BUDGETS = ["Not sure yet", "Under %s 15,000" % CURRENCY, "%s 15,000 - 40,000" % CURRENCY,
           "%s 40,000 - 100,000" % CURRENCY, "Over %s 100,000" % CURRENCY]
TIMELINES = ["Flexible", "Within 2 weeks", "Within a month", "Within 3 months"]

INQUIRY_STATUS = [("pending", "Pending"), ("in_progress", "In progress"), ("completed", "Completed"), ("cancelled", "Cancelled")]
INQUIRY_LABEL = dict(INQUIRY_STATUS)
# 'paid' and 'failed' are written only by verified payment confirmations (phase 2), never by hand.
ORDER_LABEL = {"pending": "Awaiting payment", "contacted": "Customer contacted", "cancelled": "Cancelled",
               "paid": "Paid", "failed": "Payment failed"}
ORDER_ADMIN_SETTABLE = ("pending", "contacted", "cancelled")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()\-]{6,18}$")
IMAGE_MAX, PREVIEW_MAX, FILE_MAX = 1_000_000, 3 * 1024 * 1024, 10 * 1024 * 1024
ADMIN_UPLOAD_LIMIT = IMAGE_MAX + PREVIEW_MAX + FILE_MAX + 512 * 1024  # a whole product form with all three files
PER_PAGE, ADMIN_PER_PAGE = 12, 15
REF_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L, easy to read out over the phone

COLS = ("p.id, p.slug, p.title, p.summary, p.kind, p.category, p.topic, p.price_kes, p.availability, p.featured, "
        "p.image_v, p.file_size, p.preview_size, p.views, p.downloads, p.published, p.created_at, p.updated_at, "
        "p.file_kind, p.preview_kind, p.sponsored")
KEYS = ("id", "slug", "title", "summary", "kind", "category", "topic", "price_kes", "availability", "featured",
        "image_v", "file_size", "preview_size", "views", "downloads", "published", "created_at", "updated_at",
        "file_kind", "preview_kind", "sponsored")


# ---------------------------------------------------------------- small helpers
def money(n):
    n = int(n or 0)
    return "Free" if n == 0 else "{} {:,}".format(CURRENCY, n)


def wa_digits(phone):
    """07xx xxx xxx -> 2547xxxxxxxx (Kenya) for a wa.me link; other international numbers keep their digits."""
    d = re.sub(r"\D", "", phone or "")
    return "254" + d[1:] if d.startswith("0") and len(d) == 10 else d


def like(s):
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:70].strip("-") or "item"


def unique_slug(c, title, exclude_id=0):
    base = slugify(title)
    slug, n = base, 2
    while c.execute("SELECT 1 FROM products WHERE slug = %s AND id <> %s", (slug, exclude_id)).fetchone():
        slug = "%s-%d" % (base, n)
        n += 1
    return slug


def day_start(value):
    """'2026-11-05' typed in site time -> aware UTC datetime at the start of that day (or None)."""
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").replace(tzinfo=fc.SITE_TZ).astimezone(timezone.utc)
    except ValueError:
        return None


def date_filter(column, a, where, params):
    """Add from/to (YYYY-MM-DD) conditions for `column`; the 'to' day is included."""
    start, end = day_start(a.get("from", "")), day_start(a.get("to", ""))
    if start:
        where.append(column + " >= %s")
        params.append(start)
    if end:
        where.append(column + " < %s")
        params.append(end + timedelta(days=1))


def page_info():
    try:
        page = max(1, min(int(request.args.get("page", "1")), 100000))
    except ValueError:
        page = 1
    return page


def page_count(total, per):
    return max(1, -(-total // per))


def checked(f, name):
    """A browser sends a ticked box as "on" and leaves an unticked one out; treat empty/"0"/"false" as unticked too."""
    return (f.get(name) or "").strip().lower() not in ("", "0", "false", "off", "no")


def product_dict(row):
    d = dict(zip(KEYS, row))
    d["is_free"] = d["price_kes"] == 0
    d["kind_label"] = KIND_LABEL.get(d["kind"], d["kind"])
    d["avail_label"] = AVAIL_LABEL.get(d["availability"], d["availability"])
    d["has_file"] = d["file_size"] > 0
    d["can_download"] = d["is_free"] and d["has_file"]  # free files download straight away; paid ones need phase 2
    d["can_order"] = d["availability"] != "unavailable" and not d["can_download"]
    return d


# ---------------------------------------------------------------- files
def _zip_kind(data):
    """Inspect a zip-based file. Returns 'xlsx' | 'zip' or None when it looks unsafe (zip-bomb guard)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            if len(infos) > 3000 or sum(i.file_size for i in infos) > 200 * 1024 * 1024:
                return None
            return "xlsx" if "xl/workbook.xml" in set(z.namelist()) else "zip"
    except zipfile.BadZipFile:
        return None


DOWNLOAD_MIME = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "zip": "application/zip"}
DOWNLOAD_EXT = {"pdf": ".pdf", "docx": ".docx", "doc": ".doc", "pptx": ".pptx", "xlsx": ".xlsx", "zip": ".zip"}


def sniff_download(data, filename):
    """What is this file REALLY (from its bytes, not its name)? Returns {kind, mime, name} or None if not allowed.
    Allowed: PDF, Word, PowerPoint, Excel, zip, images, and plain text/code/CSV."""
    info = fc.sniff_file(data, filename, {"pdf", "docx", "doc", "pptx", "text", "image"})
    if not info and data.startswith(b"PK\x03\x04"):
        kind = _zip_kind(data)
        info = {"kind": kind, "mime": DOWNLOAD_MIME[kind]} if kind else None
    if not info:
        return None
    kind = info["kind"]
    stem = re.sub(r"[^A-Za-z0-9._() -]+", "_", (filename or "file").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]).strip(" ._") or "file"
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    ext = DOWNLOAD_EXT.get(kind) or fc.KIND_EXT.get(kind) or (("." + filename.rsplit(".", 1)[-1].lower()) if "." in (filename or "") else ".txt")
    ext = re.sub(r"[^a-z0-9.]", "", ext.lower())[:8] or ".txt"
    return {"kind": kind, "mime": info["mime"], "name": (stem[:80] or "file") + ext}


def read_upload(field, limit):
    """(bytes, filename) for an uploaded form file, or (None, None) when nothing was chosen. Raises ValueError if too big."""
    f = request.files.get(field)
    if not f or not f.filename:
        return None, None
    data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError("too large")
    return data, f.filename


# ---------------------------------------------------------------- data used by other pages
def home_data(c):
    """Featured products and free resources for the home page (never raises: the home page must always load)."""
    try:
        featured = [product_dict(r) for r in c.execute(
            "SELECT " + COLS + " FROM products p WHERE p.published ORDER BY p.featured DESC, p.id DESC LIMIT 3").fetchall()]
        free = [product_dict(r) for r in c.execute(
            "SELECT " + COLS + " FROM products p WHERE p.published AND p.kind = 'resource' AND p.price_kes = 0 "
            "AND p.file_size > 0 ORDER BY p.id DESC LIMIT 3").fetchall()]
        return {"featured": featured, "free": free}
    except Exception:
        return {"featured": [], "free": []}


def admin_overview(c):
    """Counts for the admin dashboard cards."""
    def one(sql):
        return c.execute(sql).fetchone()[0]
    return {
        "products": one("SELECT COUNT(*) FROM products"),
        "published": one("SELECT COUNT(*) FROM products WHERE published"),
        "orders": one("SELECT COUNT(*) FROM orders"),
        "open_orders": one("SELECT COUNT(*) FROM orders WHERE status = 'pending'"),
        "inquiries": one("SELECT COUNT(*) FROM service_inquiries"),
        "new_inquiries": one("SELECT COUNT(*) FROM service_inquiries WHERE NOT is_read"),
        "paid_orders": one("SELECT COUNT(*) FROM orders WHERE status = 'paid'"),
        "revenue": payments.verified_revenue(c),  # only payments Safaricom confirmed; never estimates
        "to_review": one("SELECT COUNT(*) FROM payments WHERE status = 'duplicate'"),
    }


# ---------------------------------------------------------------- routes
def register(app, h):
    app.jinja_env.filters["money"] = money
    app.jinja_env.filters["wa_digits"] = wa_digits
    def page_url(n):
        """URL of the current list page with the same filters but page number n."""
        args = request.args.to_dict()
        args["page"] = n
        return url_for(request.endpoint, **dict(request.view_args or {}, **args))

    app.jinja_env.globals.update(
        page_url=page_url, currency=CURRENCY, MARKET_CATEGORIES=MARKET_CATEGORIES, RESOURCE_TOPICS=RESOURCE_TOPICS,
        ORDER_LABEL=ORDER_LABEL, INQUIRY_LABEL=INQUIRY_LABEL)

    @app.before_request
    def bigger_admin_uploads():
        # A product form can carry an image, a preview and the product file together: allow that for the admin forms only.
        if request.method == "POST" and request.endpoint in ("admin_product_new", "admin_product_edit"):
            request.max_content_length = ADMIN_UPLOAD_LIMIT

    @app.after_request
    def private_pages(resp):
        ep = request.endpoint or ""
        if ep.startswith("admin_") or ep == "order_confirmation":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------ public: catalogue
    def catalogue(c, a, force_kind=None):
        where, params = ["p.published"], []
        q = a.get("q", "").strip()[:60]
        if q:
            where.append("(p.title ILIKE %s OR p.summary ILIKE %s OR p.description ILIKE %s OR p.topic ILIKE %s OR p.category ILIKE %s)")
            params += [like(q)] * 5
        kind = force_kind or a.get("kind", "")
        if kind in KIND_LABEL:
            where.append("p.kind = %s")
            params.append(kind)
        if a.get("category") in MARKET_CATEGORIES:
            where.append("p.category = %s")
            params.append(a["category"])
        if a.get("topic") in RESOURCE_TOPICS:
            where.append("p.topic = %s")
            params.append(a["topic"])
        if a.get("price") == "free":
            where.append("p.price_kes = 0")
        elif a.get("price") == "paid":
            where.append("p.price_kes > 0")
        order = {"price_asc": "p.price_kes ASC, p.id DESC", "price_desc": "p.price_kes DESC, p.id DESC",
                 "popular": "(p.downloads + p.views) DESC, p.id DESC"}.get(a.get("sort"), "p.featured DESC, p.id DESC")
        sql_where = " WHERE " + " AND ".join(where)
        page = page_info()
        total = c.execute("SELECT COUNT(*) FROM products p" + sql_where, tuple(params)).fetchone()[0]
        rows = c.execute("SELECT " + COLS + " FROM products p" + sql_where + " ORDER BY " + order + " LIMIT %s OFFSET %s",
                         tuple(params) + (PER_PAGE, (page - 1) * PER_PAGE)).fetchall()
        return [product_dict(r) for r in rows], total, page, page_count(total, PER_PAGE)

    @app.route("/marketplace")
    def marketplace():
        c = h.get_db_connection()
        try:
            items, total, page, pages = catalogue(c, request.args)
        finally:
            c.close()
        return render_template("marketplace.html", items=items, total=total, page=page, pages=pages, f=request.args)

    @app.route("/learn")
    def learn():
        c = h.get_db_connection()
        try:
            items, total, page, pages = catalogue(c, request.args, force_kind="resource")
        finally:
            c.close()
        return render_template("learn.html", items=items, total=total, page=page, pages=pages, f=request.args)

    # ------------------------------------------------------------ public: product page
    def show_product(slug, form=None, errors=None, status=200, count_view=True):
        c = h.get_db_connection()
        try:
            row = c.execute("SELECT " + COLS + ", p.description FROM products p WHERE p.slug = %s", (slug,)).fetchone()
            if not row or (not row[15] and not session.get("admin_logged_in")):
                abort(404)
            p = product_dict(row[:-1])
            p["description"] = row[-1]
            seen = session.get("_pv", [])
            if count_view and p["published"] and not session.get("admin_logged_in") and p["id"] not in seen:
                c.execute("UPDATE products SET views = views + 1 WHERE id = %s", (p["id"],))
                c.commit()
                session["_pv"] = (seen + [p["id"]])[-30:]
            owned = bool(payments.owned_order(c, session.get("user_id"), p["id"]))
            related = [product_dict(r) for r in c.execute(
                "SELECT " + COLS + " FROM products p WHERE p.published AND p.category = %s AND p.id <> %s "
                "ORDER BY p.id DESC LIMIT 3", (p["category"], p["id"])).fetchall()]
        finally:
            c.close()
        return render_template("product.html", p=p, related=related, owned=owned, form=form or {}, errors=errors or {}), status

    @app.route("/marketplace/<slug>")
    def product_detail(slug):
        return show_product(slug)

    def private_ok(published):
        return published or session.get("admin_logged_in")

    @app.route("/marketplace/<slug>/image")
    def product_image(slug):
        c = h.get_db_connection()
        try:
            row = c.execute("SELECT image, image_mime, published FROM products WHERE slug = %s", (slug,)).fetchone()
        finally:
            c.close()
        if not row or not row[0] or not private_ok(row[2]):
            abort(404)
        resp = Response(bytes(row[0]), mimetype=row[1])
        resp.headers["Cache-Control"] = "public, max-age=86400"  # the URL carries ?v=, so a new picture busts the cache
        resp.headers["Content-Security-Policy"] = "default-src 'none'"
        return resp

    @app.route("/marketplace/<slug>/preview")
    def product_preview(slug):
        c = h.get_db_connection()
        try:
            row = c.execute("SELECT preview_data, preview_mime, preview_name, preview_kind, published FROM products "
                            "WHERE slug = %s", (slug,)).fetchone()
        finally:
            c.close()
        if not row or not row[0] or not private_ok(row[4]):
            abort(404)
        return fc.file_response(row[0], row[1], row[2], row[3], download=False)

    @app.route("/marketplace/<slug>/download")
    @h.limiter.limit("60 per hour")
    def product_download(slug):
        c = h.get_db_connection()
        try:
            row = c.execute("SELECT id, published, price_kes, file_data, file_name, file_mime, file_kind, title FROM products "
                            "WHERE slug = %s", (slug,)).fetchone()
            if not row or not private_ok(row[1]):
                abort(404)
            admin = session.get("admin_logged_in")
            order_id = payments.owned_order(c, session.get("user_id"), row[0]) if row[2] > 0 else None
            if row[2] > 0 and not admin and not order_id:
                # Premium file: only a VERIFIED-paid order unlocks it.
                flash("This is a premium resource. Place an order to get access.", "error")
                return redirect(url_for("product_detail", slug=slug))
            if not row[3]:
                abort(404)
            if order_id:
                payments.log_download(c, order_id, row[0], session.get("user_id"), row[7])
            elif not admin:
                c.execute("UPDATE products SET downloads = downloads + 1 WHERE id = %s", (row[0],))
                c.commit()
        finally:
            c.close()
        return fc.file_response(row[3], row[5], row[4], row[6], download=True)

    # ------------------------------------------------------------ public: place an order request
    @app.route("/marketplace/<slug>/order", methods=["POST"])
    @h.limiter.limit("5 per minute;20 per hour")
    def product_order(slug):
        h.validate_csrf()
        if request.form.get("website"):  # honeypot: real visitors never see this field
            return redirect(url_for("product_detail", slug=slug))
        f = request.form
        name, email = h.clean_text(f.get("name"), 100), f.get("email", "").strip()
        phone = h.clean_text(f.get("phone"), 25)
        note = (f.get("note") or "").replace("\r\n", "\n").strip()[:1000]
        errors = {}
        if len(name) < 2:
            errors["name"] = "Please enter your name."
        if not EMAIL_RE.match(email) or len(email) > 254:
            errors["email"] = "Please enter a valid email address, like you@example.com."
        if phone and not PHONE_RE.match(phone):
            errors["phone"] = "Use digits only, for example 0712 345 678 or +254712345678."
        c = h.get_db_connection()
        try:
            p = c.execute("SELECT id, title, price_kes, availability, file_size, published FROM products WHERE slug = %s",
                          (slug,)).fetchone()
            if not p or not p[5]:
                abort(404)
            if p[3] == "unavailable":
                errors["product"] = "Sorry, this item is not available right now."
            elif p[2] == 0 and p[4] > 0:
                return redirect(url_for("product_download", slug=slug))  # free files need no order
            if errors:
                flash("Please fix the highlighted fields.", "error")
                return show_product(slug, form=f, errors=errors, status=400, count_view=False)
            # Double-click / refresh protection: the same person ordering the same item within 30 minutes gets the same order.
            dup = c.execute("SELECT access_token FROM orders WHERE product_id = %s AND lower(customer_email) = lower(%s) "
                            "AND status = 'pending' AND created_at > %s ORDER BY id DESC LIMIT 1",
                            (p[0], email, fc.utcnow() - timedelta(minutes=30))).fetchone()
            if dup:
                flash("You already placed this order a moment ago. Here is your confirmation.", "success")
                return redirect(url_for("order_confirmation", token=dup[0]))
            reference = None
            for _ in range(8):
                candidate = "FKE-%s-%s" % (fc.utcnow().strftime("%y%m%d"), "".join(secrets.choice(REF_ALPHABET) for _ in range(5)))
                if not c.execute("SELECT 1 FROM orders WHERE reference = %s", (candidate,)).fetchone():
                    reference = candidate
                    break
            if not reference:
                abort(500)
            token = secrets.token_urlsafe(24)
            # The price is copied from the product at this moment, so later price changes never alter an old order.
            c.execute("INSERT INTO orders (reference, access_token, product_id, product_title, total_kes, customer_name, "
                      "customer_email, customer_phone, note, user_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                      (reference, token, p[0], p[1], p[2], name, email, phone, note, session.get("user_id")))
            c.commit()
        finally:
            c.close()
        return redirect(url_for("order_confirmation", token=token))

    # ------------------------------------------------------------ public: Hire Me
    @app.route("/hire", methods=["GET", "POST"])
    @h.limiter.limit("5 per minute;20 per hour", methods=["POST"])
    def hire():
        form, errors = {}, {}
        if request.method == "POST":
            h.validate_csrf()
            if request.form.get("website"):  # honeypot
                return redirect(url_for("hire_thanks"))
            f = form = request.form
            v = {"name": h.clean_text(f.get("name"), 100), "email": f.get("email", "").strip(),
                 "phone": h.clean_text(f.get("phone"), 25), "project_type": f.get("project_type", ""),
                 "budget": f.get("budget", ""), "timeline": f.get("timeline", ""),
                 "description": (f.get("description") or "").replace("\r\n", "\n").strip()}
            if len(v["name"]) < 2:
                errors["name"] = "Please enter your name."
            if not EMAIL_RE.match(v["email"]) or len(v["email"]) > 254:
                errors["email"] = "Please enter a valid email address, like you@example.com."
            if v["phone"] and not PHONE_RE.match(v["phone"]):
                errors["phone"] = "Use digits only, for example 0712 345 678 or +254712345678."
            if v["project_type"] not in PROJECT_TYPES:
                errors["project_type"] = "Choose the kind of project."
            if v["budget"] not in BUDGETS:
                errors["budget"] = "Choose a budget range (or 'Not sure yet')."
            if v["timeline"] and v["timeline"] not in TIMELINES:
                errors["timeline"] = "Choose one of the options."
            if len(v["description"]) < 20:
                errors["description"] = "Tell me a little more about the project (at least 20 characters)."
            elif len(v["description"]) > 3000:
                errors["description"] = "Please keep the description under 3000 characters."
            if not errors:
                c = h.get_db_connection()
                try:
                    c.execute("INSERT INTO service_inquiries (name, email, phone, project_type, budget_range, timeline, description) "
                              "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                              (v["name"], v["email"], v["phone"], v["project_type"], v["budget"], v["timeline"], v["description"]))
                    c.commit()
                finally:
                    c.close()
                session["hire_name"] = v["name"]
                return redirect(url_for("hire_thanks"))
            flash("Please fix the highlighted fields.", "error")
        else:
            wanted = request.args.get("type", "")
            form = {"project_type": wanted} if wanted in PROJECT_TYPES else {}
        services = [{"title": t, "text": d, "points": pts, "price": STARTING_PRICE_KES.get(t)} for t, d, pts in SERVICES]
        return render_template("hire.html", services=services, examples=EXAMPLE_PROJECTS, form=form, errors=errors,
                               project_types=PROJECT_TYPES, budgets=BUDGETS, timelines=TIMELINES), (400 if errors else 200)

    @app.route("/hire/thanks")
    def hire_thanks():
        return render_template("hire_thanks.html", name=session.pop("hire_name", None))

    # ------------------------------------------------------------ admin: marketplace
    def read_product_form(f):
        v = {"title": h.clean_text(f.get("title"), 120), "summary": h.clean_text(f.get("summary"), 240),
             "description": (f.get("description") or "").replace("\r\n", "\n").strip(),
             "kind": f.get("kind", ""), "category": f.get("category", ""), "topic": f.get("topic", ""),
             "availability": f.get("availability", ""), "price": f.get("price_kes", "").strip(),
             "published": checked(f, "published"), "featured": checked(f, "featured"),
             "sponsored": checked(f, "sponsored")}
        errors = {}
        if len(v["title"]) < 3:
            errors["title"] = "Give it a title (at least 3 characters)."
        if len(v["description"]) > 4000:
            errors["description"] = "The description can be up to 4000 characters."
        if v["kind"] not in KIND_LABEL:
            errors["kind"] = "Choose what kind of listing this is."
        if v["category"] not in MARKET_CATEGORIES:
            errors["category"] = "Choose a category."
        if v["kind"] == "resource" and v["topic"] not in RESOURCE_TOPICS:
            errors["topic"] = "Choose a topic for this learning resource."
        if v["kind"] != "resource":
            v["topic"] = ""
        if v["availability"] not in AVAIL_LABEL:
            errors["availability"] = "Choose the availability."
        if not v["price"].isdigit() or not (0 <= int(v["price"]) <= 10_000_000):
            errors["price_kes"] = "Enter the price as a whole number of %s (0 for free)." % CURRENCY
            v["price_int"] = 0
        else:
            v["price_int"] = int(v["price"])
        return v, errors

    def read_product_files(f, errors, existing):
        """Validate the three optional uploads. Returns a dict of column changes; fills `errors`."""
        changes = {}
        for field, limit, label in (("image", IMAGE_MAX, "picture"), ("preview", PREVIEW_MAX, "preview"), ("file", FILE_MAX, "file")):
            try:
                data, fname = read_upload(field, limit)
            except ValueError:
                errors[field] = "The %s is too large (maximum %s)." % (label, fc.human_size(limit))
                continue
            if data is None:
                if checked(f, "remove_" + field) and existing:
                    changes[field] = None  # remove it
                continue
            if field == "image":
                mime = h.sniff_image(data)
                if not mime:
                    errors[field] = "Use a PNG, JPEG or WebP picture."
                else:
                    changes[field] = {"data": data, "mime": mime}
            elif field == "preview":
                info = fc.sniff_file(data, fname, {"pdf", "image"})
                if not info:
                    errors[field] = "The preview must be a PDF or an image (PNG, JPEG, WebP)."
                else:
                    changes[field] = {"data": data, "mime": info["mime"], "kind": info["kind"],
                                      "name": fc.clean_filename(fname, info["kind"])}
            else:
                info = sniff_download(data, fname)
                if not info:
                    errors[field] = "That file type is not allowed. Use PDF, Word, PowerPoint, Excel, zip, an image or a text/code file."
                else:
                    changes[field] = dict(info, data=data)
        return changes

    def apply_file_changes(c, pid, changes):
        for field, ch in changes.items():
            if field == "image":
                if ch is None:
                    c.execute("UPDATE products SET image = NULL, image_mime = NULL, image_v = 0 WHERE id = %s", (pid,))
                else:
                    c.execute("UPDATE products SET image = %s, image_mime = %s, image_v = %s WHERE id = %s",
                              (ch["data"], ch["mime"], int(time.time()), pid))
            elif field == "preview":
                if ch is None:
                    c.execute("UPDATE products SET preview_data = NULL, preview_name = '', preview_mime = '', preview_kind = '', "
                              "preview_size = 0 WHERE id = %s", (pid,))
                else:
                    c.execute("UPDATE products SET preview_data = %s, preview_name = %s, preview_mime = %s, preview_kind = %s, "
                              "preview_size = %s WHERE id = %s", (ch["data"], ch["name"], ch["mime"], ch["kind"], len(ch["data"]), pid))
            else:
                if ch is None:
                    c.execute("UPDATE products SET file_data = NULL, file_name = '', file_mime = '', file_kind = '', file_size = 0 "
                              "WHERE id = %s", (pid,))
                else:
                    c.execute("UPDATE products SET file_data = %s, file_name = %s, file_mime = %s, file_kind = %s, file_size = %s "
                              "WHERE id = %s", (ch["data"], ch["name"], ch["mime"], ch["kind"], len(ch["data"]), pid))

    def product_form(existing=None, form=None, errors=None, status=200):
        return render_template("admin_product_form.html", p=existing, form=form or {}, errors=errors or {},
                               kinds=KINDS, availability=AVAILABILITY, limits={"image": fc.human_size(IMAGE_MAX),
                               "preview": fc.human_size(PREVIEW_MAX), "file": fc.human_size(FILE_MAX)}), status

    def load_for_edit(c, pid):
        row = c.execute("SELECT " + COLS + ", p.description, p.preview_name, p.file_name FROM products p WHERE p.id = %s", (pid,)).fetchone()
        if not row:
            abort(404)
        p = product_dict(row[:len(KEYS)])
        p["description"], p["preview_name"], p["file_name"] = row[len(KEYS)], row[len(KEYS) + 1], row[len(KEYS) + 2]
        return p

    @app.route("/admin/marketplace")
    @h.admin_required
    def admin_marketplace():
        a = request.args
        where, params = [], []
        q = a.get("q", "").strip()[:60]
        if q:
            where.append("(p.title ILIKE %s OR p.summary ILIKE %s)")
            params += [like(q)] * 2
        if a.get("status") == "published":
            where.append("p.published")
        elif a.get("status") == "draft":
            where.append("NOT p.published")
        if a.get("kind") in KIND_LABEL:
            where.append("p.kind = %s")
            params.append(a["kind"])
        if a.get("category") in MARKET_CATEGORIES:
            where.append("p.category = %s")
            params.append(a["category"])
        sql_where = (" WHERE " + " AND ".join(where)) if where else ""
        page = page_info()
        c = h.get_db_connection()
        try:
            total = c.execute("SELECT COUNT(*) FROM products p" + sql_where, tuple(params)).fetchone()[0]
            rows = c.execute("SELECT " + COLS + " FROM products p" + sql_where + " ORDER BY p.id DESC LIMIT %s OFFSET %s",
                             tuple(params) + (ADMIN_PER_PAGE, (page - 1) * ADMIN_PER_PAGE)).fetchall()
        finally:
            c.close()
        return render_template("admin_products.html", items=[product_dict(r) for r in rows], total=total, page=page,
                               pages=page_count(total, ADMIN_PER_PAGE), f=a, kinds=KINDS)

    @app.route("/admin/marketplace/new", methods=["GET", "POST"])
    @h.admin_required
    def admin_product_new():
        if request.method == "GET":
            return product_form(form={"kind": "resource", "availability": "available", "price_kes": "0"})
        h.validate_csrf()
        v, errors = read_product_form(request.form)
        changes = read_product_files(request.form, errors, existing=False)
        if v["published"] and v["kind"] == "resource" and v["price_int"] == 0 and not changes.get("file") and not errors:
            errors["file"] = "A free learning resource needs a file to download before it can be published."
        if errors:
            flash("Please fix the highlighted fields. (Choose any files again.)", "error")
            return product_form(form=request.form.to_dict(), errors=errors, status=400)
        c = h.get_db_connection()
        try:
            slug = unique_slug(c, v["title"])
            pid = c.execute(
                "INSERT INTO products (slug, title, summary, description, kind, category, topic, price_kes, availability, published, featured, sponsored) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (slug, v["title"], v["summary"], v["description"], v["kind"], v["category"], v["topic"], v["price_int"],
                 v["availability"], v["published"], v["featured"], v["sponsored"])).fetchone()[0]
            apply_file_changes(c, pid, changes)
            c.commit()
        finally:
            c.close()
        flash("Listing created." + (" It is live on the site." if v["published"] else " It is saved as a draft."), "success")
        return redirect(url_for("admin_marketplace"))

    @app.route("/admin/marketplace/<int:pid>/edit", methods=["GET", "POST"])
    @h.admin_required
    def admin_product_edit(pid):
        c = h.get_db_connection()
        try:
            p = load_for_edit(c, pid)
            if request.method == "GET":
                return product_form(existing=p, form={"title": p["title"], "summary": p["summary"], "description": p["description"],
                                                      "kind": p["kind"], "category": p["category"], "topic": p["topic"],
                                                      "availability": p["availability"], "price_kes": str(p["price_kes"]),
                                                      "published": p["published"], "featured": p["featured"], "sponsored": p["sponsored"]})
            h.validate_csrf()
            v, errors = read_product_form(request.form)
            changes = read_product_files(request.form, errors, existing=True)
            has_file = p["has_file"] and "file" not in changes or bool(changes.get("file"))
            if v["published"] and v["kind"] == "resource" and v["price_int"] == 0 and not has_file and not errors:
                errors["file"] = "A free learning resource needs a file to download before it can be published."
            if errors:
                flash("Please fix the highlighted fields. (Choose any files again.)", "error")
                return product_form(existing=p, form=request.form.to_dict(), errors=errors, status=400)
            c.execute("UPDATE products SET title = %s, summary = %s, description = %s, kind = %s, category = %s, topic = %s, "
                      "price_kes = %s, availability = %s, published = %s, featured = %s, sponsored = %s, updated_at = NOW() WHERE id = %s",
                      (v["title"], v["summary"], v["description"], v["kind"], v["category"], v["topic"], v["price_int"],
                       v["availability"], v["published"], v["featured"], v["sponsored"], pid))
            apply_file_changes(c, pid, changes)
            c.commit()
        finally:
            c.close()
        flash("Listing saved.", "success")
        return redirect(url_for("admin_marketplace"))

    @app.route("/admin/marketplace/<int:pid>/publish", methods=["POST"])
    @h.admin_required
    def admin_product_toggle(pid):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            row = c.execute("SELECT published, kind, price_kes, file_size FROM products WHERE id = %s", (pid,)).fetchone()
            if not row:
                abort(404)
            if not row[0] and row[1] == "resource" and row[2] == 0 and row[3] == 0:
                flash("Add a file to this free resource before publishing it.", "error")
            else:
                c.execute("UPDATE products SET published = NOT published, updated_at = NOW() WHERE id = %s", (pid,))
                c.commit()
                flash("Listing unpublished. Visitors can no longer see it." if row[0] else "Listing published. It is live now.", "success")
        finally:
            c.close()
        return redirect(h.safe_next(request.form.get("next"), url_for("admin_marketplace")))

    @app.route("/admin/marketplace/<int:pid>/delete", methods=["POST"])
    @h.admin_required
    def admin_product_delete(pid):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            # Orders keep their own copy of the title and price, so deleting a listing never loses order history.
            n = c.execute("DELETE FROM products WHERE id = %s", (pid,)).rowcount
            c.commit()
        finally:
            c.close()
        flash("Listing deleted." if n else "That listing was already gone.", "success" if n else "error")
        return redirect(url_for("admin_marketplace"))

    # ------------------------------------------------------------ admin: orders
    @app.route("/admin/orders")
    @h.admin_required
    def admin_orders():
        a = request.args
        where, params = [], []
        q = a.get("q", "").strip()[:60]
        if q:
            where.append("(o.reference ILIKE %s OR o.customer_name ILIKE %s OR o.customer_email ILIKE %s OR o.product_title ILIKE %s)")
            params += [like(q)] * 4
        if a.get("status") in ORDER_LABEL:
            where.append("o.status = %s")
            params.append(a["status"])
        date_filter("o.created_at", a, where, params)
        sql_where = (" WHERE " + " AND ".join(where)) if where else ""
        page = page_info()
        c = h.get_db_connection()
        try:
            total = c.execute("SELECT COUNT(*) FROM orders o" + sql_where, tuple(params)).fetchone()[0]
            rows = c.execute(
                "SELECT o.id, o.reference, o.product_title, o.total_kes, o.status, o.customer_name, o.customer_email, "
                "o.customer_phone, o.note, o.created_at, p.slug, "
                "(SELECT pm.receipt FROM payments pm WHERE pm.order_id = o.id AND pm.status = 'success' LIMIT 1) "
                "FROM orders o LEFT JOIN products p ON p.id = o.product_id"
                + sql_where + " ORDER BY o.id DESC LIMIT %s OFFSET %s",
                tuple(params) + (ADMIN_PER_PAGE, (page - 1) * ADMIN_PER_PAGE)).fetchall()
        finally:
            c.close()
        keys = ("id", "reference", "title", "total", "status", "name", "email", "phone", "note", "created_at", "slug", "receipt")
        return render_template("admin_orders.html", orders=[dict(zip(keys, r)) for r in rows], total=total, page=page,
                               pages=page_count(total, ADMIN_PER_PAGE), f=a, statuses=list(ORDER_LABEL.items()),
                               settable=ORDER_ADMIN_SETTABLE)

    @app.route("/admin/orders/<int:oid>/status", methods=["POST"])
    @h.admin_required
    def admin_order_status(oid):
        h.validate_csrf()
        new = request.form.get("status", "")
        if new not in ORDER_ADMIN_SETTABLE:
            abort(400, description="That status can only be set by a verified payment.")
        c = h.get_db_connection()
        try:
            # Never overwrite a verified payment by hand.
            n = c.execute("UPDATE orders SET status = %s, updated_at = NOW() WHERE id = %s AND status IN ('pending', 'contacted', 'cancelled')",
                          (new, oid)).rowcount
            c.commit()
        finally:
            c.close()
        flash("Order updated." if n else "That order cannot be changed (it may be paid already).", "success" if n else "error")
        return redirect(h.safe_next(request.form.get("next"), url_for("admin_orders")))

    # ------------------------------------------------------------ admin: Hire Me inquiries
    INQ_COLS = "id, name, email, phone, project_type, budget_range, timeline, description, status, admin_notes, is_read, created_at"
    INQ_KEYS = ("id", "name", "email", "phone", "project_type", "budget", "timeline", "description", "status", "notes", "is_read", "created_at")

    @app.route("/admin/inquiries")
    @h.admin_required
    def admin_inquiries():
        a = request.args
        where, params = [], []
        q = a.get("q", "").strip()[:60]
        if q:
            where.append("(name ILIKE %s OR email ILIKE %s OR description ILIKE %s)")
            params += [like(q)] * 3
        if a.get("status") in INQUIRY_LABEL:
            where.append("status = %s")
            params.append(a["status"])
        if a.get("type") in PROJECT_TYPES:
            where.append("project_type = %s")
            params.append(a["type"])
        if a.get("unread"):
            where.append("NOT is_read")
        date_filter("created_at", a, where, params)
        sql_where = (" WHERE " + " AND ".join(where)) if where else ""
        page = page_info()
        c = h.get_db_connection()
        try:
            total = c.execute("SELECT COUNT(*) FROM service_inquiries" + sql_where, tuple(params)).fetchone()[0]
            rows = c.execute("SELECT " + INQ_COLS + " FROM service_inquiries" + sql_where + " ORDER BY id DESC LIMIT %s OFFSET %s",
                             tuple(params) + (ADMIN_PER_PAGE, (page - 1) * ADMIN_PER_PAGE)).fetchall()
        finally:
            c.close()
        return render_template("admin_inquiries.html", items=[dict(zip(INQ_KEYS, r)) for r in rows], total=total, page=page,
                               pages=page_count(total, ADMIN_PER_PAGE), f=a, statuses=INQUIRY_STATUS, project_types=PROJECT_TYPES)

    @app.route("/admin/inquiries/<int:iid>")
    @h.admin_required
    def admin_inquiry(iid):
        c = h.get_db_connection()
        try:
            row = c.execute("SELECT " + INQ_COLS + " FROM service_inquiries WHERE id = %s", (iid,)).fetchone()
            if not row:
                abort(404)
            if not row[10]:
                c.execute("UPDATE service_inquiries SET is_read = TRUE WHERE id = %s", (iid,))  # opening it marks it read
                c.commit()
        finally:
            c.close()
        return render_template("admin_inquiry.html", i=dict(zip(INQ_KEYS, row)), statuses=INQUIRY_STATUS)

    @app.route("/admin/inquiries/<int:iid>/update", methods=["POST"])
    @h.admin_required
    def admin_inquiry_update(iid):
        h.validate_csrf()
        status = request.form.get("status", "")
        if status not in INQUIRY_LABEL:
            abort(400, description="Choose a valid status.")
        notes = (request.form.get("notes") or "").replace("\r\n", "\n").strip()[:2000]
        c = h.get_db_connection()
        try:
            n = c.execute("UPDATE service_inquiries SET status = %s, admin_notes = %s, is_read = TRUE, updated_at = NOW() "
                          "WHERE id = %s", (status, notes, iid)).rowcount
            c.commit()
        finally:
            c.close()
        if not n:
            abort(404)
        flash("Inquiry updated.", "success")
        return redirect(url_for("admin_inquiry", iid=iid))

    @app.route("/admin/inquiries/<int:iid>/read", methods=["POST"])
    @h.admin_required
    def admin_inquiry_read(iid):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            c.execute("UPDATE service_inquiries SET is_read = NOT is_read WHERE id = %s", (iid,))
            c.commit()
        finally:
            c.close()
        return redirect(h.safe_next(request.form.get("next"), url_for("admin_inquiries")))
