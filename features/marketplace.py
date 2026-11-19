"""Marketplace catalogue, admin listing management and unpaid order requests.

Payment processing is deliberately not implemented here; verified payments are a later phase.
"""
import re
import secrets
import unicodedata
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import abort, flash, redirect, render_template, request, session, url_for

CATEGORIES = (
    "Programming resources", "Mathematics and statistics", "Website templates",
    "Excel and data analysis", "Digital learning materials", "Website development services",
)
ORDER_STATUSES = ("pending", "contacted", "fulfilled", "cancelled")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def slugify(value):
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return (value[:150].strip("-") or "product")

def money(value):
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0 or amount > Decimal("9999999999.99"):
        return None
    return amount.quantize(Decimal("0.01"))

def register(app, h):
    def admin_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("admin_logged_in"):
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)
        return wrapped

    def clean_image_url(value):
        value = (value or "").strip()[:500]
        if not value:
            return ""
        # Only allow HTTPS image URLs; never accept javascript:, data: or arbitrary schemes.
        if not value.startswith("https://") or any(ch in value for ch in "\r\n\t"):
            return None
        return value

    def get_product(c, product_id, published_only=False):
        sql = "SELECT id, title, slug, description, category, price_kes, image_url, is_published, created_at FROM marketplace_products WHERE id = %s"
        if published_only:
            sql += " AND is_published = TRUE"
        return c.execute(sql, (product_id,)).fetchone()

    @app.route("/marketplace")
    def marketplace():
        q = request.args.get("q", "").strip()[:70]
        category = request.args.get("category", "")
        where, args = ["is_published = TRUE"], []
        if q:
            pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(title ILIKE %s OR description ILIKE %s)")
            args.extend([pattern, pattern])
        if category in CATEGORIES:
            where.append("category = %s")
            args.append(category)
        c = h.get_db_connection()
        try:
            products = c.execute("SELECT id, title, slug, description, category, price_kes, image_url FROM marketplace_products WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT 60", tuple(args)).fetchall()
        finally:
            c.close()
        return render_template("marketplace.html", products=products, categories=CATEGORIES, q=q, selected_category=category)

    @app.route("/marketplace/<int:product_id>")
    def marketplace_product(product_id):
        c = h.get_db_connection()
        try:
            product = get_product(c, product_id, published_only=True)
        finally:
            c.close()
        if not product:
            abort(404)
        return render_template("marketplace_product.html", product=product)

    @app.route("/marketplace/<int:product_id>/order", methods=["GET", "POST"])
    @h.limiter.limit("5 per hour", methods=["POST"])
    def marketplace_order(product_id):
        c = h.get_db_connection()
        try:
            product = get_product(c, product_id, published_only=True)
        finally:
            c.close()
        if not product:
            abort(404)
        form = request.form if request.method == "POST" else {}
        if request.method == "POST":
            h.validate_csrf()
            name = h.clean_text(form.get("name", ""), 100)
            email = (form.get("email", "") or "").strip().lower()[:254]
            phone = h.clean_text(form.get("phone", ""), 30)
            note = h.clean_text(form.get("note", ""), 1000)
            if len(name) < 2 or not EMAIL_RE.match(email):
                flash("Enter your name and a valid email address.", "error")
            else:
                order_ref = "FKI-" + secrets.token_hex(6).upper()
                c = h.get_db_connection()
                try:
                    # Re-read the published price at order creation to avoid trusting browser values.
                    current = get_product(c, product_id, published_only=True)
                    if not current:
                        abort(404)
                    row = c.execute(
                        "INSERT INTO marketplace_orders (order_ref, product_id, product_title, customer_name, customer_email, customer_phone, customer_note, amount_kes) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                        (order_ref, current[0], current[1], name, email, phone, note, current[5]),
                    ).fetchone()
                    c.commit()
                finally:
                    c.close()
                session["marketplace_order_ref"] = order_ref
                return redirect(url_for("marketplace_order_confirmation", order_ref=order_ref))
        return render_template("marketplace_order.html", product=product, form=form)

    @app.route("/marketplace/order/<order_ref>/confirmation")
    def marketplace_order_confirmation(order_ref):
        if session.get("marketplace_order_ref") != order_ref:
            abort(404)
        return render_template("marketplace_order_confirmation.html", order_ref=order_ref)

    @app.route("/admin/marketplace")
    @admin_required
    def admin_marketplace():
        c = h.get_db_connection()
        try:
            products = c.execute("SELECT id, title, category, price_kes, is_published, created_at FROM marketplace_products ORDER BY id DESC").fetchall()
            orders = c.execute("SELECT id, order_ref, product_title, customer_name, customer_email, customer_phone, amount_kes, status, payment_status, created_at FROM marketplace_orders ORDER BY id DESC LIMIT 50").fetchall()
        finally:
            c.close()
        return render_template("admin_marketplace.html", products=products, orders=orders, categories=CATEGORIES, statuses=ORDER_STATUSES)

    @app.route("/admin/marketplace/products/new", methods=["GET", "POST"])
    @admin_required
    def admin_marketplace_new():
        form = request.form if request.method == "POST" else {}
        if request.method == "POST":
            h.validate_csrf()
            title = h.clean_text(form.get("title", ""), 140)
            description = (form.get("description", "") or "").strip()[:5000]
            category = form.get("category", "")
            price = money(form.get("price", ""))
            image_url = clean_image_url(form.get("image_url", ""))
            if len(title) < 3 or len(description) < 10 or category not in CATEGORIES or price is None or image_url is None:
                flash("Check the title, description, category, price and HTTPS image URL.", "error")
            else:
                c = h.get_db_connection()
                try:
                    base = slugify(title)
                    slug = base
                    n = 2
                    while c.execute("SELECT 1 FROM marketplace_products WHERE slug = %s", (slug,)).fetchone():
                        slug = f"{base}-{n}"; n += 1
                    c.execute("INSERT INTO marketplace_products (title, slug, description, category, price_kes, image_url) VALUES (%s,%s,%s,%s,%s,%s)",
                              (title, slug, description, category, price, image_url))
                    c.commit()
                finally:
                    c.close()
                flash("Product added as a draft. Publish it when it is ready.", "success")
                return redirect(url_for("admin_marketplace"))
        return render_template("admin_marketplace_form.html", product=None, categories=CATEGORIES, form=form)

    @app.route("/admin/marketplace/products/<int:product_id>/edit", methods=["GET", "POST"])
    @admin_required
    def admin_marketplace_edit(product_id):
        c = h.get_db_connection()
        try:
            product = get_product(c, product_id)
        finally:
            c.close()
        if not product:
            abort(404)
        form = request.form if request.method == "POST" else {"title": product[1], "description": product[3], "category": product[4], "price": str(product[5]), "image_url": product[6]}
        if request.method == "POST":
            h.validate_csrf()
            title = h.clean_text(form.get("title", ""), 140)
            description = (form.get("description", "") or "").strip()[:5000]
            category = form.get("category", "")
            price = money(form.get("price", ""))
            image_url = clean_image_url(form.get("image_url", ""))
            if len(title) < 3 or len(description) < 10 or category not in CATEGORIES or price is None or image_url is None:
                flash("Check the title, description, category, price and HTTPS image URL.", "error")
            else:
                c = h.get_db_connection()
                try:
                    c.execute("UPDATE marketplace_products SET title=%s, description=%s, category=%s, price_kes=%s, image_url=%s, updated_at=NOW() WHERE id=%s",
                              (title, description, category, price, image_url, product_id))
                    c.commit()
                finally:
                    c.close()
                flash("Product updated.", "success")
                return redirect(url_for("admin_marketplace"))
        return render_template("admin_marketplace_form.html", product=product, categories=CATEGORIES, form=form)

    @app.route("/admin/marketplace/products/<int:product_id>/publish", methods=["POST"])
    @admin_required
    def admin_marketplace_publish(product_id):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            row = c.execute("UPDATE marketplace_products SET is_published = NOT is_published, updated_at=NOW() WHERE id=%s RETURNING is_published", (product_id,)).fetchone()
            c.commit()
        finally:
            c.close()
        if not row: abort(404)
        flash("Product published." if row[0] else "Product unpublished.", "success")
        return redirect(url_for("admin_marketplace"))

    @app.route("/admin/marketplace/products/<int:product_id>/delete", methods=["POST"])
    @admin_required
    def admin_marketplace_delete(product_id):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            # Existing orders keep their product title and price snapshot; product_id becomes NULL.
            row = c.execute("DELETE FROM marketplace_products WHERE id=%s RETURNING id", (product_id,)).fetchone()
            c.commit()
        finally:
            c.close()
        if not row: abort(404)
        flash("Product deleted. Existing order records were preserved.", "success")
        return redirect(url_for("admin_marketplace"))

    @app.route("/admin/marketplace/orders/<int:order_id>/status", methods=["POST"])
    @admin_required
    def admin_marketplace_order_status(order_id):
        h.validate_csrf()
        status = request.form.get("status", "")
        if status not in ORDER_STATUSES:
            abort(400)
        c = h.get_db_connection()
        try:
            row = c.execute("UPDATE marketplace_orders SET status=%s, updated_at=NOW() WHERE id=%s RETURNING id", (status, order_id)).fetchone()
            c.commit()
        finally:
            c.close()
        if not row: abort(404)
        flash("Order status updated. Payment status remains unchanged until a verified payment integration is added.", "success")
        return redirect(url_for("admin_marketplace") + "#orders")
