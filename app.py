
import os
import hmac
import secrets
import sqlite3
from functools import wraps

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    abort,
)
from werkzeug.security import check_password_hash
from dotenv import load_dotenv


# Load settings from the existing .env file
load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH")

DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "website.db")


# Check that important configuration values exist
if not app.config["SECRET_KEY"]:
    raise RuntimeError("SECRET_KEY is missing from your .env file.")

if not ADMIN_USERNAME or not ADMIN_PASSWORD_HASH:
    raise RuntimeError(
        "ADMIN_USERNAME or ADMIN_PASSWORD_HASH is missing from your .env file."
    )


# Database connection
def get_db_connection():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


# CSRF protection for forms that change data
def get_csrf_token():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_urlsafe(32)
    return session["_csrf_token"]


app.jinja_env.globals["csrf_token"] = get_csrf_token


def validate_csrf():
    submitted_token = request.form.get("csrf_token", "")
    session_token = session.get("_csrf_token", "")

    if not submitted_token or not session_token:
        abort(400, description="Invalid or missing security token.")

    if not hmac.compare_digest(submitted_token, session_token):
        abort(400, description="Invalid security token.")


# Protect administrator pages
def login_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("login"))
        return view_function(*args, **kwargs)

    return wrapped_view


# Home page
@app.route("/")
def index():
    return render_template("index.html")


# About page
@app.route("/about")
def about():
    return render_template("about.html")


# Contact page
@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        validate_csrf()

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        message = request.form.get("message", "").strip()

        if not name or not email or not message:
            flash("Please fill in all fields.")
            return render_template("contact.html"), 400

        if len(name) > 100 or len(email) > 254 or len(message) > 5000:
            flash("One or more fields are too long.")
            return render_template("contact.html"), 400

        if "\r" in email or "\n" in email or "@" not in email:
            flash("Please enter a valid email address.")
            return render_template("contact.html"), 400

        connection = get_db_connection()
        try:
            connection.execute(
                """
                INSERT INTO messages (name, email, message)
                VALUES (?, ?, ?)
                """,
                (name, email, message),
            )
            connection.commit()
        finally:
            connection.close()

        return redirect(url_for("success"))

    return render_template("contact.html")


# Contact form success page
@app.route("/success")
def success():
    return render_template("success.html")


# Administrator login
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        validate_csrf()

        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if (
            hmac.compare_digest(username, ADMIN_USERNAME)
            and check_password_hash(ADMIN_PASSWORD_HASH, password)
        ):
            session.clear()
            session["admin_logged_in"] = True
            session["_csrf_token"] = secrets.token_urlsafe(32)
            return redirect(url_for("admin"))

        flash("Invalid username or password.")

    return render_template("login.html")


# Administrator dashboard
@app.route("/admin")
@login_required
def admin():
    search = request.args.get("search", "").strip()

    connection = get_db_connection()

    try:
        total_messages = connection.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()[0]

        if search:
            pattern = f"%{search}%"
            messages = connection.execute(
                """
                SELECT id, name, email, message
                FROM messages
                WHERE name LIKE ? OR email LIKE ?
                ORDER BY id DESC
                """,
                (pattern, pattern),
            ).fetchall()
        else:
            messages = connection.execute(
                """
                SELECT id, name, email, message
                FROM messages
                ORDER BY id DESC
                """
            ).fetchall()

        total_search_results = len(messages)

    finally:
        connection.close()

    return render_template(
        "admin.html",
        messages=messages,
        total_messages=total_messages,
        total_search_results=total_search_results,
        search=search,
    )


# Delete a contact message
@app.route("/delete/<int:message_id>", methods=["POST"])
@login_required
def delete_message(message_id):
    validate_csrf()

    connection = get_db_connection()

    try:
        connection.execute(
            "DELETE FROM messages WHERE id = ?",
            (message_id,),
        )
        connection.commit()
    finally:
        connection.close()

    flash("Message deleted successfully.")
    return redirect(url_for("admin"))


# Administrator logout
@app.route("/logout", methods=["POST"])
@login_required
def logout():
    validate_csrf()
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    # Local development only. Do not deploy with debug enabled.
    app.run(host="0.0.0.0", port=5000, debug=True)