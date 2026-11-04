import os
import re
import csv
import io
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

import psycopg
from dotenv import load_dotenv
from flask import (
    jsonify, Flask, render_template, request, redirect, url_for, session, flash, abort, Response,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
import psycopg.errors

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
    MAX_CONTENT_LENGTH=3 * 1024 * 1024,  # Phase 7 (library) will raise this for PDF/Word uploads
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
        connection.execute("""CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY, username VARCHAR(30) UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        connection.execute("""CREATE TABLE IF NOT EXISTS posts (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title VARCHAR(120) NOT NULL, body VARCHAR(3000) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        connection.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ")
        connection.execute("""CREATE TABLE IF NOT EXISTS comments (
            id BIGSERIAL PRIMARY KEY, post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            body VARCHAR(1000) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        connection.execute("""CREATE TABLE IF NOT EXISTS likes (
            post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            PRIMARY KEY (post_id, user_id))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS chat_messages (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            body VARCHAR(500) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        connection.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS category VARCHAR(20) NOT NULL DEFAULT 'Other'")
        connection.execute("""CREATE TABLE IF NOT EXISTS reports (
            id BIGSERIAL PRIMARY KEY, kind VARCHAR(10) NOT NULL, target_id BIGINT NOT NULL,
            reporter_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            reason VARCHAR(300) NOT NULL, resolved BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE (kind, target_id, reporter_id))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS game_scores (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            score INT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        # ---- Phase 1-2: student profiles and notifications (safe on existing data) ----
        for col in (
            "full_name VARCHAR(60) NOT NULL DEFAULT ''",
            "university VARCHAR(80) NOT NULL DEFAULT ''",
            "course VARCHAR(80) NOT NULL DEFAULT ''",
            "interests VARCHAR(200) NOT NULL DEFAULT ''",
            "bio VARCHAR(300) NOT NULL DEFAULT ''",
            "avatar BYTEA",
            "avatar_mime VARCHAR(20)",
            "avatar_v INT NOT NULL DEFAULT 0",
            "show_university BOOLEAN NOT NULL DEFAULT TRUE",
            "show_course BOOLEAN NOT NULL DEFAULT TRUE",
            "show_interests BOOLEAN NOT NULL DEFAULT TRUE",
            "show_bio BOOLEAN NOT NULL DEFAULT TRUE",
        ):
            connection.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS " + col)
        connection.execute("""CREATE TABLE IF NOT EXISTS notifications (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind VARCHAR(20) NOT NULL, title VARCHAR(120) NOT NULL, body VARCHAR(300) NOT NULL DEFAULT '',
            link VARCHAR(200) NOT NULL DEFAULT '', is_read BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        connection.execute("CREATE INDEX IF NOT EXISTS notifications_user_idx "
                           "ON notifications (user_id, is_read, id DESC)")
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
    submitted = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
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
    if request.endpoint in ("admin", "login", "logout", "delete_message", "export_csv", "signin", "register", "api_chat", "api_scores", "admin_reports",
                            "dashboard", "notifications", "api_notifications", "settings", "profile", "my_profile"):
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


@app.errorhandler(413)
def too_large(e):
    return error_page(413, "That file is too large.")


@app.errorhandler(429)
def too_many(e):
    return error_page(429, "Too many attempts. Please wait a minute and try again.")


@app.errorhandler(500)
def server_error(e):
    return error_page(500, "Something went wrong on our side. Please try again later.")


# ---------- Public pages ----------
@app.route("/")
def index():
    return render_template("index.html", stats=get_stats())


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



# ---------- Community, accounts, live stats ----------
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,30}$")


@app.context_processor
def who():
    return {"current_user": session.get("username"), "is_admin": bool(session.get("admin_logged_in"))}


def user_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please sign in to do that.", "error")
            return redirect(url_for("signin"))
        return view(*args, **kwargs)
    return wrapped


def get_stats():
    try:
        c = get_db_connection()
        try:
            members = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            posts = c.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            rows = c.execute(
                "SELECT p.id, p.title, u.username FROM posts p JOIN users u ON u.id = p.user_id "
                "ORDER BY p.id DESC LIMIT 4").fetchall()
        finally:
            c.close()
        return {"members": members, "posts": posts,
                "latest": [{"id": r[0], "title": r[1], "author": r[2]} for r in rows]}
    except Exception:
        return {"members": 0, "posts": 0, "latest": []}


@app.route("/api/stats")
def api_stats():
    return jsonify(get_stats())


def start_session(user_id, username):
    session.pop("admin_logged_in", None)
    session["user_id"], session["username"] = user_id, username
    session["_csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute;20 per hour", methods=["POST"])
def register():
    if request.method == "POST":
        validate_csrf()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        error = None
        if not USERNAME_RE.match(username):
            error = "Username must be 3-30 letters, numbers or underscores."
        elif len(password) < 8 or len(password) > 128:
            error = "Password must be 8-128 characters."
        if not error:
            c = get_db_connection()
            try:
                row = c.execute(
                    "INSERT INTO users (username, password_hash) VALUES (%s, %s) RETURNING id",
                    (username, generate_password_hash(password))).fetchone()
                notify(c, row[0], "system", "Welcome to Felix KE Ideas!",
                       "Complete your profile to get started.", "/settings")
                c.commit()
                start_session(row[0], username)
                flash("Welcome to the community, " + username + "!", "success")
                return redirect(url_for("dashboard"))
            except psycopg.errors.UniqueViolation:
                error = "That username is taken. Try another."
            finally:
                c.close()
        flash(error, "error")
        return render_template("account.html", mode="register", username=username), 400
    return render_template("account.html", mode="register", username="")


@app.route("/signin", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def signin():
    if request.method == "POST":
        validate_csrf()
        username = request.form.get("username", "").strip()
        c = get_db_connection()
        try:
            row = c.execute("SELECT id, username, password_hash FROM users WHERE lower(username) = lower(%s)",
                            (username,)).fetchone()
        finally:
            c.close()
        if row and check_password_hash(row[2], request.form.get("password", "")):
            start_session(row[0], row[1])
            return redirect(url_for("dashboard"))
        flash("Wrong username or password.", "error")
        return render_template("account.html", mode="signin", username=username), 400
    return render_template("account.html", mode="signin", username="")


@app.route("/signout", methods=["POST"])
def signout():
    validate_csrf()
    session.clear()
    return redirect(url_for("index"))


CATEGORIES = ["Maths", "Python", "Data", "Web", "Other"]
REPORT_TABLES = {"post": "posts", "comment": "comments", "chat": "chat_messages"}


@app.route("/community")
def community():
    uid = session.get("user_id") or 0
    cat = request.args.get("category", "")
    q = request.args.get("q", "").strip()[:60]
    where, params = [], [uid]
    if cat in CATEGORIES:
        where.append("p.category = %s")
        params.append(cat)
    if q:
        where.append("(p.title ILIKE %s OR p.body ILIKE %s)")
        params += ["%" + q + "%"] * 2
    sql = ("SELECT p.id, p.title, p.body, u.username, p.created_at, p.user_id, "
           "(SELECT COUNT(*) FROM likes l WHERE l.post_id = p.id), "
           "EXISTS(SELECT 1 FROM likes l WHERE l.post_id = p.id AND l.user_id = %s), p.category "
           "FROM posts p JOIN users u ON u.id = p.user_id"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY p.id DESC LIMIT 50")
    c = get_db_connection()
    try:
        posts = c.execute(sql, params).fetchall()
        comments = {}
        ids = [p[0] for p in posts]
        if ids:
            rows = c.execute(
                "SELECT c.id, c.post_id, u.username, c.body, c.created_at, c.user_id FROM comments c "
                "JOIN users u ON u.id = c.user_id WHERE c.post_id = ANY(%s) ORDER BY c.id", (ids,)).fetchall()
            for r in rows:
                comments.setdefault(r[1], []).append(r)
    finally:
        c.close()
    return render_template("community.html", posts=posts, comments=comments, user_id=session.get("user_id"),
                           categories=CATEGORIES, active=cat, q=q)


@app.route("/community/like/<int:post_id>", methods=["POST"])
@user_required
@limiter.limit("60 per minute")
def like_post(post_id):
    validate_csrf()
    c = get_db_connection()
    try:
        removed = c.execute("DELETE FROM likes WHERE post_id = %s AND user_id = %s",
                            (post_id, session["user_id"])).rowcount
        if not removed:
            c.execute("INSERT INTO likes (post_id, user_id) SELECT id, %s FROM posts WHERE id = %s "
                      "ON CONFLICT DO NOTHING", (session["user_id"], post_id))
        c.commit()
    finally:
        c.close()
    return redirect(url_for("community") + "#post-" + str(post_id))


@app.route("/community/comment/<int:post_id>", methods=["POST"])
@user_required
@limiter.limit("10 per minute;100 per day")
def add_comment(post_id):
    validate_csrf()
    body = request.form.get("body", "").strip()
    if not body or len(body) > 1000:
        flash("A reply must be 1-1000 characters.", "error")
    else:
        c = get_db_connection()
        try:
            c.execute("INSERT INTO comments (post_id, user_id, body) SELECT id, %s, %s FROM posts WHERE id = %s",
                      (session["user_id"], body, post_id))
            owner = c.execute("SELECT user_id, title FROM posts WHERE id = %s", (post_id,)).fetchone()
            if owner and owner[0] != session["user_id"]:
                notify(c, owner[0], "reply", session["username"] + " replied to your post", owner[1],
                       "/community#post-" + str(post_id))
            c.commit()
        finally:
            c.close()
    return redirect(url_for("community") + "#post-" + str(post_id))


@app.route("/community/comment/delete/<int:comment_id>", methods=["POST"])
def delete_comment(comment_id):
    validate_csrf()
    c = get_db_connection()
    try:
        row = c.execute("SELECT user_id, post_id FROM comments WHERE id = %s", (comment_id,)).fetchone()
        if row and (session.get("admin_logged_in") or row[0] == session.get("user_id")):
            c.execute("DELETE FROM comments WHERE id = %s", (comment_id,))
            c.commit()
            return redirect(url_for("community") + "#post-" + str(row[1]))
    finally:
        c.close()
    flash("You can only delete your own replies.", "error")
    return redirect(url_for("community"))


@app.route("/chat")
def chat():
    return render_template("chat.html")


@app.route("/api/chat", methods=["GET", "POST"])
@limiter.limit("30 per minute", methods=["POST"])
def api_chat():
    uid = session.get("user_id")
    c = get_db_connection()
    try:
        if request.method == "POST":
            if not uid:
                return jsonify(error="Sign in to chat."), 401
            validate_csrf()
            body = str((request.get_json(silent=True) or {}).get("body", "")).strip()
            if not body or len(body) > 500:
                return jsonify(error="Messages must be 1-500 characters."), 400
            c.execute("INSERT INTO chat_messages (user_id, body) VALUES (%s, %s)", (uid, body))
        if uid:
            c.execute("UPDATE users SET last_seen = NOW() WHERE id = %s", (uid,))
        c.commit()
        after = request.args.get("after", type=int)
        cols = "m.id, u.username, m.body, m.created_at"
        if after:
            rows = c.execute("SELECT " + cols + " FROM chat_messages m JOIN users u ON u.id = m.user_id "
                             "WHERE m.id > %s ORDER BY m.id LIMIT 100", (after,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM (SELECT " + cols + " FROM chat_messages m JOIN users u ON u.id = m.user_id "
                             "ORDER BY m.id DESC LIMIT 50) t ORDER BY id").fetchall()
        online = [r[0] for r in c.execute(
            "SELECT username FROM users WHERE last_seen > NOW() - INTERVAL '45 seconds' ORDER BY username").fetchall()]
    finally:
        c.close()
    return jsonify(messages=[{"id": r[0], "user": r[1], "body": r[2], "time": r[3].isoformat()} for r in rows],
                   online=online, me=session.get("username"))


@app.route("/community/new", methods=["POST"])
@user_required
@limiter.limit("6 per minute;40 per day")
def new_post():
    validate_csrf()
    title, body = request.form.get("title", "").strip(), request.form.get("body", "").strip()
    if not title or not body or len(title) > 120 or len(body) > 3000:
        flash("Add a title (up to 120 characters) and a post (up to 3000).", "error")
        return redirect(url_for("community"))
    c = get_db_connection()
    try:
        category = request.form.get("category", "Other")
        c.execute("INSERT INTO posts (user_id, title, body, category) VALUES (%s, %s, %s, %s)",
                  (session["user_id"], title, body, category if category in CATEGORIES else "Other"))
        c.commit()
    finally:
        c.close()
    flash("Posted. Thanks for sharing!", "success")
    return redirect(url_for("community"))


@app.route("/community/delete/<int:post_id>", methods=["POST"])
def delete_post(post_id):
    validate_csrf()
    c = get_db_connection()
    try:
        row = c.execute("SELECT user_id FROM posts WHERE id = %s", (post_id,)).fetchone()
        if row and (session.get("admin_logged_in") or row[0] == session.get("user_id")):
            c.execute("DELETE FROM posts WHERE id = %s", (post_id,))
            c.commit()
            flash("Post deleted.", "success")
        else:
            flash("You can only delete your own posts.", "error")
    finally:
        c.close()
    return redirect(url_for("community"))


@app.route("/report", methods=["POST"])
@user_required
@limiter.limit("10 per minute;50 per day")
def report():
    validate_csrf()
    kind = request.form.get("kind", "")
    try:
        target = int(request.form.get("target_id", ""))
    except ValueError:
        target = 0
    reason = request.form.get("reason", "").strip()[:300] or "No reason given"
    if kind in REPORT_TABLES and target:
        c = get_db_connection()
        try:
            c.execute("INSERT INTO reports (kind, target_id, reporter_id, reason) VALUES (%s, %s, %s, %s) "
                      "ON CONFLICT DO NOTHING", (kind, target, session["user_id"], reason))
            c.commit()
        finally:
            c.close()
    if request.headers.get("X-CSRF-Token"):
        return jsonify(ok=True)
    flash("Thanks. A moderator will review it.", "success")
    nxt = request.form.get("next", "/community")
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/community")


@app.route("/admin/reports")
@login_required
def admin_reports():
    c = get_db_connection()
    try:
        rows = c.execute(
            "SELECT r.id, r.kind, r.target_id, r.reason, ru.username, r.created_at, "
            "CASE r.kind WHEN 'post' THEN (SELECT title || ': ' || body FROM posts WHERE id = r.target_id) "
            "WHEN 'comment' THEN (SELECT body FROM comments WHERE id = r.target_id) "
            "ELSE (SELECT body FROM chat_messages WHERE id = r.target_id) END "
            "FROM reports r JOIN users ru ON ru.id = r.reporter_id WHERE NOT r.resolved ORDER BY r.id DESC").fetchall()
    finally:
        c.close()
    return render_template("reports.html", reports=rows)


@app.route("/admin/reports/<int:report_id>/<action>", methods=["POST"])
@login_required
def resolve_report(report_id, action):
    validate_csrf()
    if action not in ("remove", "dismiss"):
        abort(404)
    c = get_db_connection()
    try:
        row = c.execute("SELECT kind, target_id FROM reports WHERE id = %s", (report_id,)).fetchone()
        if row:
            if action == "remove":
                c.execute("DELETE FROM " + REPORT_TABLES[row[0]] + " WHERE id = %s", (row[1],))
            c.execute("UPDATE reports SET resolved = TRUE WHERE kind = %s AND target_id = %s", row)
            c.commit()
            flash("Content removed." if action == "remove" else "Report dismissed.", "success")
    finally:
        c.close()
    return redirect(url_for("admin_reports"))


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/api/scores", methods=["GET", "POST"])
@limiter.limit("20 per minute", methods=["POST"])
def api_scores():
    uid = session.get("user_id")
    c = get_db_connection()
    try:
        if request.method == "POST":
            if not uid:
                return jsonify(error="Sign in to save scores."), 401
            validate_csrf()
            try:
                score = int((request.get_json(silent=True) or {}).get("score", 0))
            except (TypeError, ValueError):
                score = 0
            if 0 < score <= 100:
                c.execute("INSERT INTO game_scores (user_id, score) VALUES (%s, %s)", (uid, score))
                c.commit()
        q = ("SELECT u.username, MAX(s.score) FROM game_scores s JOIN users u ON u.id = s.user_id{} "
             "GROUP BY u.username ORDER BY 2 DESC, u.username LIMIT 10")
        week = c.execute(q.format(" WHERE s.created_at > NOW() - INTERVAL '7 days'")).fetchall()
        alltime = c.execute(q.format("")).fetchall()
    finally:
        c.close()
    pack = lambda rows: [{"user": r[0], "score": r[1]} for r in rows]
    return jsonify(week=pack(week), all=pack(alltime), signed_in=bool(uid))


@app.route("/play")
def play():
    return render_template("play.html")


# ======================================================================
# STUDENT PLATFORM - Phase 1 (profiles) and Phase 2 (dashboard, notifications)
# ======================================================================
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
AVATAR_MAX_BYTES = 300_000  # the settings page shrinks pictures to ~256px before upload
USER_COLS = ("id", "username", "full_name", "university", "course", "interests", "bio", "avatar_v",
             "created_at", "show_university", "show_course", "show_interests", "show_bio")


def clean_text(value, limit):
    """Turn control characters/newlines into spaces, trim, and cap the length."""
    return CONTROL_RE.sub(" ", value or "").strip()[:limit]


def safe_next(target, default):
    """Only allow redirects to paths on this site."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return default


def load_user(c, key, value):
    """Return one user's profile as a dict (never loads the avatar bytes). key is 'id' or 'username'."""
    where = "id = %s" if key == "id" else "lower(username) = lower(%s)"
    row = c.execute("SELECT " + ", ".join(USER_COLS) + " FROM users WHERE " + where, (value,)).fetchone()
    return dict(zip(USER_COLS, row)) if row else None


def profile_complete(u):
    return bool(u["full_name"] and u["university"] and u["course"])


def user_stats(c, uid):
    r = c.execute(
        "SELECT (SELECT COUNT(*) FROM posts WHERE user_id = %s), "
        "(SELECT COUNT(*) FROM comments WHERE user_id = %s), "
        "(SELECT COUNT(*) FROM chat_messages WHERE user_id = %s), "
        "(SELECT COALESCE(MAX(score), 0) FROM game_scores WHERE user_id = %s)", (uid, uid, uid, uid)).fetchone()
    # buddies and squads arrive in Phases 3 and 4; keep the keys so templates do not change later.
    return {"posts": r[0], "replies": r[1], "messages": r[2], "best_score": r[3], "buddies": 0, "squads": 0}


def notify(conn, user_id, kind, title, body="", link=""):
    """Add a notification for a user. Uses the caller's connection; the CALLER commits.
    kind: message | friend | squad | squad_message | answer | session | reply | system
    Later phases call this, e.g. notify(c, friend_id, "friend", "Ann sent you a buddy request", link="/buddies")."""
    conn.execute("INSERT INTO notifications (user_id, kind, title, body, link) VALUES (%s, %s, %s, %s, %s)",
                 (user_id, kind, title[:120], body[:300], link[:200]))


@app.template_filter("ago")
def ago(dt):
    if not dt:
        return ""
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return str(secs // 60) + " min ago"
    if secs < 86400:
        return str(secs // 3600) + " h ago"
    if secs < 7 * 86400:
        return str(secs // 86400) + " d ago"
    return dt.strftime("%d %b %Y")


@app.context_processor
def notification_badge():
    """Unread count for the bell in the header (0 when signed out or if the database hiccups)."""
    uid = session.get("user_id")
    if not uid:
        return {"unread_count": 0}
    try:
        c = get_db_connection()
        try:
            n = c.execute("SELECT COUNT(*) FROM notifications WHERE user_id = %s AND NOT is_read", (uid,)).fetchone()[0]
        finally:
            c.close()
        return {"unread_count": n}
    except Exception:
        return {"unread_count": 0}


# ---------- Placeholder pages for phases that are not built yet ----------
# Replace an entry's view with the real feature when its phase is done (same endpoint name keeps the menu working).
COMING_SOON = [
    ("buddies", "/buddies", "Buddies", 3, "Find students, send friend requests and start private chats.", None),
    ("squads", "/squads", "My Squads", 4, "Create or join academic groups, each with its own chat.", None),
    ("messages", "/messages", "Messages", 5, "Private conversations with your buddies.", ("Open the group chat", "chat")),
    ("problem_solver", "/problem-solver", "Problem Solver", 8,
     "Scan or type a maths problem and get step-by-step solutions.", None),
    ("library", "/library", "Academic Library", 7, "Share and download notes, past papers and revision material.", None),
    ("leaderboard", "/leaderboard", "Leaderboard", 9,
     "Rankings that reward real academic help, not message counts.", ("See the Maths Sprint leaderboard", "play")),
    ("study_sessions", "/study-sessions", "Study Sessions", 10,
     "Plan group study sessions with a calendar and reminders.", None),
]


def _register_coming_soon(endpoint, path, title, phase, blurb, alt):
    @user_required
    def view():
        return render_template("coming_soon.html", title=title, phase=phase, blurb=blurb, icon_name=endpoint,
                               alt=(alt[0], url_for(alt[1])) if alt else None)
    app.add_url_rule(path, endpoint, view)


for _spec in COMING_SOON:
    _register_coming_soon(*_spec)


# ---------- Getting started checklist ----------
def onboarding_steps(me, stats):
    """`available` False = that phase is not built yet, so it shows 'Coming soon' and is left out of the progress bar."""
    return [
        {"title": "Complete your profile", "desc": "Add your name, university and course.",
         "done": profile_complete(me), "available": True, "url": url_for("settings")},
        {"title": "Add friends", "desc": "Find students and send buddy requests.",
         "done": False, "available": False, "url": url_for("buddies")},
        {"title": "Join your first squad", "desc": "Discover a public squad that fits your subjects.",
         "done": False, "available": False, "url": url_for("squads")},
        {"title": "Create your first squad", "desc": "Start a study group for your course.",
         "done": False, "available": False, "url": url_for("squads")},
        {"title": "Send your first message", "desc": "Say hello in the group chat.",
         "done": stats["messages"] > 0, "available": True, "url": url_for("chat")},
        {"title": "Try the maths problem scanner", "desc": "Scan a question and see the steps.",
         "done": False, "available": False, "url": url_for("problem_solver")},
    ]


# ---------- Dashboard ----------
@app.route("/dashboard")
@user_required
def dashboard():
    uid = session["user_id"]
    c = get_db_connection()
    try:
        me = load_user(c, "id", uid)
        if not me:  # account removed while the session was still open
            session.clear()
            return redirect(url_for("signin"))
        stats = user_stats(c, uid)
        notes = c.execute("SELECT id, kind, title, body, link, is_read, created_at FROM notifications "
                          "WHERE user_id = %s ORDER BY id DESC LIMIT 5", (uid,)).fetchall()
    finally:
        c.close()
    steps = onboarding_steps(me, stats)
    available = [s for s in steps if s["available"]]
    done = sum(1 for s in available if s["done"])
    percent = int(100 * done / len(available)) if available else 0
    return render_template("dashboard.html", me=me, stats=stats, notes=notes, steps=steps,
                           done=done, total=len(available), percent=percent)


# ---------- Notifications ----------
@app.route("/notifications")
@user_required
def notifications():
    only_unread = request.args.get("filter") == "unread"
    c = get_db_connection()
    try:
        rows = c.execute("SELECT id, kind, title, body, link, is_read, created_at FROM notifications "
                         "WHERE user_id = %s" + (" AND NOT is_read" if only_unread else "") +
                         " ORDER BY id DESC LIMIT 100", (session["user_id"],)).fetchall()
    finally:
        c.close()
    return render_template("notifications.html", notes=rows, only_unread=only_unread)


@app.route("/notifications/<int:nid>/read", methods=["POST"])
@user_required
def notification_read(nid):
    validate_csrf()
    c = get_db_connection()
    try:
        row = c.execute("UPDATE notifications SET is_read = TRUE WHERE id = %s AND user_id = %s RETURNING link",
                        (nid, session["user_id"])).fetchone()
        c.commit()
    finally:
        c.close()
    if row and row[0] and request.form.get("go"):
        return redirect(safe_next(row[0], url_for("notifications")))
    return redirect(safe_next(request.form.get("next"), url_for("notifications")))


@app.route("/notifications/read-all", methods=["POST"])
@user_required
def notifications_read_all():
    validate_csrf()
    c = get_db_connection()
    try:
        c.execute("UPDATE notifications SET is_read = TRUE WHERE user_id = %s AND NOT is_read", (session["user_id"],))
        c.commit()
    finally:
        c.close()
    flash("All notifications marked as read.", "success")
    return redirect(safe_next(request.form.get("next"), url_for("notifications")))


@app.route("/api/notifications")
def api_notifications():
    uid = session.get("user_id")
    if not uid:
        return jsonify(unread=0, items=[])
    c = get_db_connection()
    try:
        unread = c.execute("SELECT COUNT(*) FROM notifications WHERE user_id = %s AND NOT is_read", (uid,)).fetchone()[0]
        rows = []
        if not request.args.get("count"):
            rows = c.execute("SELECT id, kind, title, body, link, is_read, created_at FROM notifications "
                             "WHERE user_id = %s ORDER BY id DESC LIMIT 10", (uid,)).fetchall()
    finally:
        c.close()
    return jsonify(unread=unread, items=[
        {"id": r[0], "kind": r[1], "title": r[2], "body": r[3], "link": r[4], "read": r[5], "time": r[6].isoformat()}
        for r in rows])


# ---------- Settings: profile details, privacy, picture, password ----------
@app.route("/settings", methods=["GET", "POST"])
@user_required
@limiter.limit("20 per minute", methods=["POST"])
def settings():
    uid = session["user_id"]
    c = get_db_connection()
    try:
        if request.method == "POST":
            validate_csrf()
            f = request.form
            c.execute(
                "UPDATE users SET full_name = %s, university = %s, course = %s, interests = %s, bio = %s, "
                "show_university = %s, show_course = %s, show_interests = %s, show_bio = %s WHERE id = %s",
                (clean_text(f.get("full_name"), 60), clean_text(f.get("university"), 80),
                 clean_text(f.get("course"), 80), clean_text(f.get("interests"), 200), clean_text(f.get("bio"), 300),
                 "show_university" in f, "show_course" in f, "show_interests" in f, "show_bio" in f, uid))
            c.commit()
            flash("Profile saved.", "success")
            return redirect(url_for("settings"))
        me = load_user(c, "id", uid)
    finally:
        c.close()
    if not me:
        session.clear()
        return redirect(url_for("signin"))
    return render_template("settings.html", me=me, avatar_max_kb=AVATAR_MAX_BYTES // 1000)


def sniff_image(data):
    """Identify the real image type from its first bytes (never trust the filename or browser-supplied type)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@app.route("/settings/avatar", methods=["POST"])
@user_required
@limiter.limit("10 per minute;40 per day")
def settings_avatar():
    validate_csrf()
    f = request.files.get("avatar")
    data = f.stream.read(AVATAR_MAX_BYTES + 1) if f else b""
    mime = sniff_image(data)
    if not data:
        flash("Choose a picture first.", "error")
    elif len(data) > AVATAR_MAX_BYTES:
        flash("That picture is too large. Use one under %d KB." % (AVATAR_MAX_BYTES // 1000), "error")
    elif not mime:
        flash("Use a PNG, JPEG or WebP picture.", "error")
    else:
        c = get_db_connection()
        try:
            # avatar_v is a timestamp: it changes the picture's URL so browsers never show an old one.
            c.execute("UPDATE users SET avatar = %s, avatar_mime = %s, avatar_v = EXTRACT(EPOCH FROM NOW())::int "
                      "WHERE id = %s", (data, mime, session["user_id"]))
            c.commit()
        finally:
            c.close()
        flash("Profile picture updated.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/avatar/remove", methods=["POST"])
@user_required
def settings_avatar_remove():
    validate_csrf()
    c = get_db_connection()
    try:
        c.execute("UPDATE users SET avatar = NULL, avatar_mime = NULL, avatar_v = 0 WHERE id = %s",
                  (session["user_id"],))
        c.commit()
    finally:
        c.close()
    flash("Profile picture removed.", "success")
    return redirect(url_for("settings"))


@app.route("/avatar/<int:uid>")
def avatar(uid):
    # Only signed-in members (or the admin) can load pictures, so they cannot be scraped by outsiders.
    if not session.get("user_id") and not session.get("admin_logged_in"):
        abort(404)
    c = get_db_connection()
    try:
        row = c.execute("SELECT avatar, avatar_mime FROM users WHERE id = %s", (uid,)).fetchone()
    finally:
        c.close()
    if not row or not row[0]:
        abort(404)
    resp = Response(bytes(row[0]), mimetype=row[1])
    resp.headers["Cache-Control"] = "private, max-age=86400"  # URL carries ?v=, so a new picture busts the cache
    resp.headers["Content-Security-Policy"] = "default-src 'none'"
    return resp


@app.route("/settings/password", methods=["POST"])
@user_required
@limiter.limit("5 per minute;20 per hour")
def settings_password():
    validate_csrf()
    current, new = request.form.get("current_password", ""), request.form.get("new_password", "")
    c = get_db_connection()
    try:
        row = c.execute("SELECT password_hash FROM users WHERE id = %s", (session["user_id"],)).fetchone()
        if not row or not check_password_hash(row[0], current):
            flash("Your current password is wrong.", "error")
        elif len(new) < 8 or len(new) > 128:
            flash("New password must be 8-128 characters.", "error")
        elif new == current:
            flash("Choose a password different from your current one.", "error")
        else:
            c.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                      (generate_password_hash(new), session["user_id"]))
            c.commit()
            session["_csrf_token"] = secrets.token_urlsafe(32)
            flash("Password changed.", "success")
    finally:
        c.close()
    return redirect(url_for("settings"))


# ---------- Profiles ----------
@app.route("/profile")
@user_required
def my_profile():
    return redirect(url_for("profile", username=session["username"]))


@app.route("/u/<username>")
@user_required
def profile(username):
    if not USERNAME_RE.match(username):
        abort(404)
    c = get_db_connection()
    try:
        u = load_user(c, "username", username)
        if not u:
            abort(404)
        stats = user_stats(c, u["id"])
        posts = c.execute("SELECT id, title, category, created_at FROM posts WHERE user_id = %s "
                          "ORDER BY id DESC LIMIT 5", (u["id"],)).fetchall()
    finally:
        c.close()
    own = u["id"] == session["user_id"]
    # Owners see everything; other members only see what the owner chose to show.
    show = {k: own or u["show_" + k] for k in ("university", "course", "interests", "bio")}
    tags = [t.strip() for t in u["interests"].split(",") if t.strip()][:12] if show["interests"] else []
    return render_template("profile.html", u=u, stats=stats, posts=posts, own=own, show=show, tags=tags)


if __name__ == "__main__":
    # Local development only. Set FLASK_DEBUG=1 in your local .env, never on Render.
    app.run(host="127.0.0.1", port=5000, debug=DEBUG)
