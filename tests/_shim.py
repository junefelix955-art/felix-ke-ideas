"""Shared test setup: runs the real app against a throwaway SQLite database (no PostgreSQL needed)."""
import io, os, re, sys, sqlite3, tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
for _name in ("dotenv", "flask_limiter", "psycopg"):
    try:
        __import__(_name)
    except ImportError:
        sys.path.insert(0, os.path.join(HERE, "tests", "stubs"))
        break
from werkzeug.security import generate_password_hash

os.environ.update(FLASK_SECRET_KEY="test-secret-" + "x" * 32, ADMIN_USERNAME="admin", FLASK_DEBUG="1",
                  ADMIN_PASSWORD_HASH=generate_password_hash("correct horse"), WHATSAPP_NUMBER="254700111222")
for k in ("DATABASE_URL", "MPESA_CONSUMER_KEY", "MPESA_CONSUMER_SECRET", "MPESA_SHORTCODE", "MPESA_PASSKEY", "MPESA_CALLBACK_TOKEN", "SITE_URL", "MPESA_TILL_NUMBER", "WHATSAPP_CHANNEL_URL", "FACEBOOK_URL", "INSTAGRAM_URL", "X_URL", "YOUTUBE_URL", "TIKTOK_URL", "LINKEDIN_URL", "GITHUB_URL"):
    os.environ.pop(k, None)
import app as A
from features import schema

DB = os.path.join(tempfile.mkdtemp(), "t.db")
sqlite3.register_adapter(datetime, lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
sqlite3.register_converter("TIMESTAMPTZ", lambda b: datetime.strptime(b.decode()[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc))


def translate(sql):
    sql = sql.replace("%s", "?").replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
    sql = sql.replace("BIGINT", "INTEGER").replace("BYTEA", "BLOB").replace("NOW()", "CURRENT_TIMESTAMP")
    return re.sub(r"\bILIKE\b", "LIKE", sql)


class Conn:
    def __init__(self):
        self.c = sqlite3.connect(DB, detect_types=sqlite3.PARSE_DECLTYPES)
    def execute(self, sql, params=()):
        return self.c.execute(translate(sql), tuple(params))
    def commit(self): self.c.commit()
    def rollback(self): self.c.rollback()
    def close(self): self.c.close()


def raw():
    return sqlite3.connect(DB, detect_types=sqlite3.PARSE_DECLTYPES)


setup = Conn()
setup.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)")
setup.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT, message TEXT, created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP, is_read BOOLEAN NOT NULL DEFAULT 0)")
setup.execute("CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT, title TEXT, body TEXT, link TEXT, is_read BOOLEAN DEFAULT 0, created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)")
for st in schema.STATEMENTS:
    if st.lstrip().startswith("CREATE") and any(t in st for t in ("products", "orders", "service_inquiries", "payments", "download_log", "password_resets", "tutoring_bookings", "ad_inquiries", "newsletter_subscribers", "announcements")):
        setup.execute(st)
setup.execute("ALTER TABLE products ADD COLUMN sponsored BOOLEAN NOT NULL DEFAULT 0")  # the PostgreSQL schema adds this with ALTER TABLE
setup.commit(); setup.close()

import psycopg
psycopg.connect = lambda *a, **k: Conn()
os.environ["DATABASE_URL"] = "sqlite-stand-in"  # after import, so the PostgreSQL table setup is not attempted

app = A.app
app.config["TESTING"] = True
passed, failed = 0, []


def ok(name, cond, extra=""):
    global passed
    if cond:
        passed += 1
    else:
        failed.append(name)
        print("  FAIL", name, extra)


def fresh():
    cl = app.test_client()
    with cl.session_transaction() as s:
        s["_csrf_token"] = "tok"
    return cl


def post(cl, url, data=None, **kw):
    return cl.post(url, data=dict(data or {}, csrf_token="tok"), **kw)


def finish():
    print()
    print("RESULT:", "ALL %d CHECKS PASSED" % passed if not failed else "%d passed, %d FAILED: %s" % (passed, len(failed), "; ".join(failed)))
    sys.exit(1 if failed else 0)
