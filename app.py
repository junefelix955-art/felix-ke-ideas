import os
import re
import csv
import io
import hmac
import secrets
from datetime import timedelta
from functools import wraps

import psycopg
from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for, session, flash, abort, Response,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

load_dotenv()

DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # Render sits behind a proxy
app.config.update(
    SECRET_KEY=os.getenv("FLASK_SECRET_KEY"),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=not DEBUG,  # HTTPS only in production
    PERMANENT_SESSION_LIFETIME=timedelta(hours=2),
)

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")

if not app.config["SECRET_KEY"]:
    raise RuntimeError("FLASK_SECRET_KEY is missing from your environment.")
if not ADMIN_USERNAME or not ADMIN_PASSWORD_HASH:
    raise RuntimeError("ADMIN_USERNAME or ADMIN_PASSWORD_HASH is missing.")

limiter = Limiter(get_remote_address, app=app, storage_uri="memory://")


@app.context_processor
def contact_details():
    # Set CONTACT_EMAIL and WHATSAPP_NUMBER (digits only, e.g. 2547XXXXXXXX) in .env and on Render.
    return {
        "contact_email": os.getenv("CONTACT_EMAIL", ""),
        "whatsapp_number": re.sub(r"\D", "", os.getenv("WHATSAPP_NUMBER", "")),
    }


# ---------- Database ----------
def get_db_connection():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured.")
    return psycopg.connect(database_url, sslmode="require")


def initialize_database():
    connection = get_db_connection()
    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id BIGSERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                email VARCHAR(254) NOT NULL,
                message VARCHAR(5000) NOT NULL
            )
        """)
        # Safe on existing data: older rows get the time this line first ran.
        connection.execute(
            "ALTER TABLE messages "
            "ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
        )
        connection.commit()
    finally:
        connection.close()


if os.getenv("DATABASE_URL"):
    initialize_database()


# ---------- CSRF ----------
def get_csrf_token():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_urlsafe(32)
    return session["_csrf_token"]


app.jinja_env.globals["csrf_token"] = get_csrf_token  # use {{ csrf_token() }}


def validate_csrf():
    submitted = request.form.get("csrf_token", "")
    expected = session.get("_csrf_token", "")
    if not submitted or not expected or not hmac.compare_digest(submitted, expected):
        abort(400, description="Invalid or missing security token. Reload the page and try again.")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


# ---------- Security headers and error pages ----------
@app.after_request
def add_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if request.endpoint in ("admin", "login", "logout", "delete_message", "export_csv"):
        response.headers["Cache-Control"] = "no-store"  # back button can't reopen the dashboard
    return response


def error_page(code, message):
    return render_template("error.html", code=code, message=message), code


@app.errorhandler(400)
def bad_request(e):
    return error_page(400, getattr(e, "description", "Bad request."))


@app.errorhandler(404)
def not_found(e):
    return error_page(404, "That page does not exist.")


@app.errorhandler(429)
def too_many(e):
    return error_page(429, "Too many attempts. Please wait a minute and try again.")


@app.errorhandler(500)
def server_error(e):
    return error_page(500, "Something went wrong on our side. Please try again later.")


# ---------- Public pages ----------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/contact", methods=["GET", "POST"])
@limiter.limit("5 per minute;30 per hour", methods=["POST"])
def contact():
    if request.method == "POST":
        validate_csrf()

        # Honeypot: real visitors never see or fill this field.
        if request.form.get("website"):
            return redirect(url_for("success"))

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        message = request.form.get("message", "").strip()

        error = None
        if not name or not email or not message:
            error = "Please fill in all fields."
        elif len(name) > 100 or len(email) > 254 or len(message) > 5000:
            error = "One or more fields are too long."
        elif not EMAIL_RE.match(email):
            error = "Please enter a valid email address, like you@example.com."

        if error:
            flash(error, "error")
            # `form` lets the template refill the fields so nothing typed is lost.
            return render_template("contact.html", form=request.form), 400

        connection = get_db_connection()
        try:
            connection.execute(
                "INSERT INTO messages (name, email, message) VALUES (%s, %s, %s)",
                (name, email, message),
            )
            connection.commit()
        finally:
            connection.close()

        session["last_sender"] = name
        return redirect(url_for("success"))

    return render_template("contact.html", form={})


@app.route("/success")
def success():
    return render_template("success.html", name=session.pop("last_sender", None))


# ---------- Admin ----------
@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if session.get("admin_logged_in"):
        return redirect(url_for("admin"))

    if request.method == "POST":
        validate_csrf()
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if hmac.compare_digest(username, ADMIN_USERNAME) and check_password_hash(
            ADMIN_PASSWORD_HASH, password
        ):
            session.clear()
            session.permanent = True
            session["admin_logged_in"] = True
            session["_csrf_token"] = secrets.token_urlsafe(32)
            return redirect(url_for("admin"))

        flash("Invalid username or password.", "error")

    return render_template("login.html")


@app.route("/admin")
@login_required
def admin():
    search = request.args.get("search", "").strip()
    connection = get_db_connection()
    try:
        total_messages = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        if search:
            pattern = f"%{search}%"
            messages = connection.execute(
                """SELECT id, name, email, message, created_at FROM messages
                   WHERE name ILIKE %s OR email ILIKE %s ORDER BY id DESC""",
                (pattern, pattern),
            ).fetchall()
        else:
            messages = connection.execute(
                "SELECT id, name, email, message, created_at FROM messages ORDER BY id DESC"
            ).fetchall()
    finally:
        connection.close()

    return render_template(
        "admin.html",
        messages=messages,
        total_messages=total_messages,
        total_search_results=len(messages),
        search_term=search,
    )


@app.route("/admin/export.csv")
@login_required
def export_csv():
    connection = get_db_connection()
    try:
        rows = connection.execute(
            "SELECT id, name, email, message, created_at FROM messages ORDER BY id DESC"
        ).fetchall()
    finally:
        connection.close()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "name", "email", "message", "received_at"])
    for row in rows:
        # Prefix cells that start with = + - @ so spreadsheets never run them as formulas.
        writer.writerow([f"'{c}" if isinstance(c, str) and c[:1] in "=+-@" else c for c in row])
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=messages.csv"},
    )


@app.route("/delete/<int:message_id>", methods=["POST"])
@login_required
def delete_message(message_id):
    validate_csrf()
    connection = get_db_connection()
    try:
        connection.execute("DELETE FROM messages WHERE id = %s", (message_id,))
        connection.commit()
    finally:
        connection.close()
    flash("Message deleted.", "success")
    return redirect(url_for("admin"))


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    validate_csrf()
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    # Local development only. Set FLASK_DEBUG=1 in your local .env, never on Render.
    app.run(host="127.0.0.1", port=5000, debug=DEBUG)
