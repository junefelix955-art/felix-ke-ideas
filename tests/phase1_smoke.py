r"""Route-level smoke test for the phase 1 business features. Needs only Flask (no PostgreSQL, no internet).

    py tests\phase1_smoke.py

It runs the real app against a throwaway SQLite database (the SQL used by the new features is portable), so it checks the
page logic, forms, uploads, permissions and CSRF. It is NOT a substitute for trying the live site once after deploying.
"""
import io, os, re, sys, sqlite3, tempfile, types
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
# Use tiny stand-ins only when the real packages are not installed (e.g. on a bare test machine).
for name in ("dotenv", "flask_limiter", "psycopg"):
    try:
        __import__(name)
    except ImportError:
        sys.path.insert(0, os.path.join(HERE, "tests", "stubs"))
        break

from werkzeug.security import generate_password_hash

os.environ.update(FLASK_SECRET_KEY="test-secret-" + "x" * 32, ADMIN_USERNAME="admin", FLASK_DEBUG="1",
                  ADMIN_PASSWORD_HASH=generate_password_hash("correct horse"), WHATSAPP_NUMBER="254712345678")
os.environ.pop("DATABASE_URL", None)
import app as A
from features import schema

# ---------------- SQLite stand-in for the PostgreSQL connection
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
    def close(self): self.c.close()


setup = Conn()
setup.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)")
setup.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT, message TEXT, created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP, is_read BOOLEAN NOT NULL DEFAULT 0)")
setup.execute("CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT, title TEXT, body TEXT, link TEXT, is_read BOOLEAN DEFAULT 0, created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)")
for st in schema.STATEMENTS:
    if ("products" in st or "orders" in st or "service_inquiries" in st) and st.lstrip().startswith("CREATE"):
        setup.execute(st)
setup.commit(); setup.close()

import psycopg
psycopg.connect = lambda *a, **k: Conn()
os.environ["DATABASE_URL"] = "sqlite-stand-in"  # set AFTER import so the PostgreSQL table setup is not attempted

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
    d = dict(data or {}, csrf_token="tok")
    return cl.post(url, data=d, **kw)


PDF = b"%PDF-1.4\n" + b"hello pdf\n" * 50
anon, admin = fresh(), fresh()

print("Public pages")
for p in ("/", "/marketplace", "/learn", "/hire", "/privacy", "/about", "/contact", "/play"):
    r = anon.get(p)
    ok(p + " loads", r.status_code == 200, r.status_code)
ok("home shows new calls to action", b"Browse the marketplace" in anon.get("/").data)
ok("nav has Marketplace / Learn / Hire Me", all(x in anon.get("/").data for x in (b">Marketplace<", b">Learn<", b">Hire Me<")))
ok("empty marketplace shows an empty state", b"being stocked" in anon.get("/marketplace").data)

print("Admin login")
r = post(fresh(), "/login", {"username": "admin\u00e9", "password": "x"})
ok("non-ASCII username no longer crashes login", r.status_code == 200 and b"Invalid" in r.data, r.status_code)
r = post(fresh(), "/login", {"username": "admin", "password": "wrong"})
ok("wrong password rejected", b"Invalid" in r.data)
r = post(admin, "/login", {"username": "admin", "password": "correct horse"})
with admin.session_transaction() as s:
    s["_csrf_token"] = "tok"
ok("admin can sign in", r.status_code == 302)
for p in ("/admin", "/admin/marketplace", "/admin/orders", "/admin/inquiries", "/admin/marketplace/new"):
    ok(p + " closed to visitors", anon.get(p).status_code == 302)
    ok(p + " opens for admin", admin.get(p).status_code == 200)
ok("admin pages are not cached", "no-store" in admin.get("/admin/orders").headers.get("Cache-Control", ""))

print("Creating listings")
def new_product(cl, **over):
    d = {"title": "Python Basics Notes", "summary": "Start here", "description": "Line one\nLine two", "kind": "resource",
         "category": "Programming resources", "topic": "Python", "price_kes": "0", "availability": "available", "published": "on"}
    d.update(over)
    files = {k: v for k, v in d.items() if isinstance(v, tuple)}
    data = {k: v for k, v in d.items() if not isinstance(v, tuple)}
    data.update(files)
    return post(cl, "/admin/marketplace/new", data, content_type="multipart/form-data")

r = new_product(admin, file=(io.BytesIO(PDF), "notes.pdf"), preview=(io.BytesIO(PDF), "sample.pdf"))
ok("free resource created", r.status_code == 302, r.status_code)
r = new_product(admin, title="Flask Guide Premium", topic="Flask", price_kes="1500", file=(io.BytesIO(PDF), "flask.pdf"), featured="on")
ok("paid resource created", r.status_code == 302)
r = new_product(admin, title="Landing Page Service", kind="service", category="Website development services", topic="", price_kes="8000")
ok("service created (no file needed)", r.status_code == 302)
r = new_product(admin, title="Secret Draft", published="", file=(io.BytesIO(PDF), "d.pdf"))
ok("draft created", r.status_code == 302)
r = new_product(admin, title="Sold Out Template", kind="product", category="Website templates", topic="", price_kes="500", availability="unavailable")
ok("unavailable item created", r.status_code == 302)

print("Upload safety")
r = new_product(admin, title="Fake", file=(io.BytesIO(b"MZ\x90\x00" + b"\x00" * 100), "evil.pdf"))
ok("executable renamed .pdf is rejected", r.status_code == 400 and b"not allowed" in r.data, r.status_code)
r = new_product(admin, title="Zip ok", kind="product", category="Website templates", topic="", price_kes="900", file=(io.BytesIO(b"PK\x03\x04" + b"\x00" * 30), "t.zip"))
ok("garbage 'zip' rejected", r.status_code == 400)
buf = io.BytesIO()
import zipfile
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("index.html", "<h1>hi</h1>")
r = new_product(admin, title="Template Pack", kind="product", category="Website templates", topic="", price_kes="900", file=(io.BytesIO(buf.getvalue()), "pack.zip"))
ok("real zip accepted", r.status_code == 302)
r = new_product(admin, title="Big", price_kes="100", file=(io.BytesIO(b"%PDF-1.4\n" + b"0" * (9 * 1024 * 1024)), "big.pdf"))
ok("9 MB file accepted (above the old 8 MB site limit)", r.status_code == 302, r.status_code)
r = new_product(admin, title="Huge", price_kes="100", file=(io.BytesIO(b"%PDF-1.4\n" + b"0" * (11 * 1024 * 1024)), "huge.pdf"))
ok("11 MB file rejected", r.status_code in (400, 413), r.status_code)
r = new_product(admin, title="Bad", price_kes="abc")
ok("bad price rejected", r.status_code == 400 and b"whole number" in r.data)
r = new_product(admin, title="No file free", file=None)
ok("free resource cannot be published without a file", r.status_code == 400)
r = post(anon, "/admin/marketplace/new", {"title": "x"}, content_type="multipart/form-data")
ok("visitor cannot create listings", r.status_code == 302)
r = admin.post("/admin/marketplace/new", data={"title": "x"})
ok("create without CSRF token refused", r.status_code == 400)

print("Public catalogue")
html = anon.get("/marketplace").data.decode()
ok("published items listed", "Python Basics Notes" in html and "Flask Guide Premium" in html and "Landing Page Service" in html)
ok("draft hidden from visitors", "Secret Draft" not in html)
ok("search works", "Flask Guide" in anon.get("/marketplace?q=flask").data.decode() and "Python Basics" not in anon.get("/marketplace?q=flask").data.decode())
ok("free filter", "Python Basics" in anon.get("/marketplace?price=free").data.decode() and "Flask Guide" not in anon.get("/marketplace?price=free").data.decode())
ok("category filter", "Landing Page" in anon.get("/marketplace?category=Website+development+services").data.decode())
ok("no-match shows empty state", b"Nothing matches" in anon.get("/marketplace?q=zzzzqq").data)
ok("weird input does not break search", anon.get("/marketplace?q=%25_%5C&page=abc&sort=bogus&category=nope").status_code == 200)
ok("learn lists only resources", "Landing Page Service" not in anon.get("/learn").data.decode() and "Flask Guide" in anon.get("/learn").data.decode())
ok("learn topic filter", "Python Basics" in anon.get("/learn?topic=Python").data.decode() and "Flask Guide" not in anon.get("/learn?topic=Python").data.decode())
ok("home page shows featured + free", b"Featured" in anon.get("/").data and b"Free to download" in anon.get("/").data)
ok("draft page 404 for visitor", anon.get("/marketplace/secret-draft").status_code == 404)
ok("draft page visible to admin", admin.get("/marketplace/secret-draft").status_code == 200)
r = anon.get("/marketplace/python-basics-notes")
ok("product page shows preview + free download", r.status_code == 200 and b"Preview" in r.data and b"Download now" in r.data)
ok("product page shows order form for paid item", b"Place order" in anon.get("/marketplace/flask-guide-premium").data)
ok("preview served inline", anon.get("/marketplace/python-basics-notes/preview").headers.get("Content-Disposition", "").startswith("inline"))
ok("missing image is 404", anon.get("/marketplace/python-basics-notes/image").status_code == 404)

print("Downloads (the money rule)")
r = anon.get("/marketplace/python-basics-notes/download")
ok("free file downloads", r.status_code == 200 and r.data == PDF and "attachment" in r.headers["Content-Disposition"])
r = anon.get("/marketplace/flask-guide-premium/download")
ok("PREMIUM file is NOT served to visitors", r.status_code == 302 and PDF not in r.data)
ok("premium file not reachable via preview", anon.get("/marketplace/flask-guide-premium/preview").status_code == 404)
ok("admin can fetch premium file", admin.get("/marketplace/flask-guide-premium/download").data == PDF)
ok("draft download blocked for visitors", anon.get("/marketplace/secret-draft/download").status_code == 404)
dl = sqlite3.connect(DB).execute("SELECT downloads FROM products WHERE slug='python-basics-notes'").fetchone()[0]
ok("download counted once (admin fetches not counted)", dl == 1, dl)

print("Orders")
def order(cl, slug, **kw):
    d = {"name": "Amina Otieno", "email": "amina@example.com", "phone": "0712 345 678", "note": "Please send PDF"}
    d.update(kw)
    return post(cl, "/marketplace/%s/order" % slug, d)
r = order(anon, "flask-guide-premium", email="nope")
ok("bad email -> form errors", r.status_code == 400 and b"valid email" in r.data)
r = order(anon, "flask-guide-premium", phone="abc")
ok("bad phone -> form errors", r.status_code == 400)
r = order(anon, "flask-guide-premium", name="A")
ok("short name -> form errors", r.status_code == 400)
r = order(anon, "sold-out-template")
ok("unavailable item cannot be ordered", r.status_code == 400 and b"not available" in r.data)
r = order(anon, "secret-draft")
ok("draft cannot be ordered", r.status_code == 404)
r = order(anon, "python-basics-notes")
ok("free file goes to download instead of an order", r.status_code == 302 and "/download" in r.headers["Location"])
r = order(anon, "flask-guide-premium")
loc = r.headers.get("Location", "")
ok("valid order redirects to confirmation", r.status_code == 302 and "/order/" in loc, loc)
page = anon.get(loc)
ref = re.search(rb"FKE-\d{6}-[A-Z2-9]{5}", page.data)
ok("confirmation shows a reference, the price and status", page.status_code == 200 and ref and b"KES 1,500" in page.data and b"Awaiting payment" in page.data)
ok("confirmation not cached/indexed", "no-store" in page.headers.get("Cache-Control", "") and b"noindex" in page.data)
r2 = order(anon, "flask-guide-premium")
ok("double submit returns the same order", r2.headers["Location"] == loc)
ok("guessing a confirmation link fails", anon.get("/order/" + "a" * 32).status_code == 404 and anon.get("/order/1").status_code == 404)
r = post(anon, "/marketplace/flask-guide-premium/order", {"name": "Bot", "email": "b@example.com", "website": "spam.com"})
ok("honeypot swallows bots without creating an order", sqlite3.connect(DB).execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1)
ok("order without CSRF token refused", anon.post("/marketplace/flask-guide-premium/order", data={"name": "x", "email": "a@b.co"}).status_code == 400)
r = order(anon, "landing-page-service", email="biz@example.com")
ok("service request creates an order too", r.status_code == 302)
# price snapshot: change the price, the old order keeps its amount
pid = sqlite3.connect(DB).execute("SELECT id FROM products WHERE slug='flask-guide-premium'").fetchone()[0]
post(admin, "/admin/marketplace/%d/edit" % pid, {"title": "Flask Guide Premium", "kind": "resource", "category": "Programming resources", "topic": "Flask", "price_kes": "2500", "availability": "available", "published": "on"}, content_type="multipart/form-data")
ok("old order keeps its price after the price changes", b"KES 1,500" in anon.get(loc).data)
ok("file kept when editing without re-uploading", admin.get("/marketplace/flask-guide-premium/download").data == PDF)

print("Admin: orders")
html = admin.get("/admin/orders").data.decode()
ok("orders listed with customer + WhatsApp link", "amina@example.com" in html and "wa.me/254712345678" in html)
ok("order search", "amina" in admin.get("/admin/orders?q=amina").data.decode() and "No orders match" in admin.get("/admin/orders?q=nobody").data.decode())
ok("date filter (today)", "amina" in admin.get("/admin/orders?from=2000-01-01&to=2100-01-01").data.decode() and "No orders match" in admin.get("/admin/orders?from=2000-01-01&to=2000-01-02").data.decode())
oid = sqlite3.connect(DB).execute("SELECT id FROM orders ORDER BY id LIMIT 1").fetchone()[0]
r = post(admin, "/admin/orders/%d/status" % oid, {"status": "contacted"})
ok("admin can mark order contacted", r.status_code == 302 and b"Customer contacted" in admin.get("/admin/orders").data)
r = post(admin, "/admin/orders/%d/status" % oid, {"status": "paid"})
ok("admin CANNOT mark an order paid by hand", r.status_code == 400)
sqlite3.connect(DB).execute("SELECT 1")
c = sqlite3.connect(DB); c.execute("UPDATE orders SET status='paid' WHERE id=%d" % oid); c.commit()
r = post(admin, "/admin/orders/%d/status" % oid, {"status": "cancelled"})
ok("a paid order cannot be overwritten", sqlite3.connect(DB).execute("SELECT status FROM orders WHERE id=%d" % oid).fetchone()[0] == "paid")
ok("visitor cannot change orders", post(anon, "/admin/orders/%d/status" % oid, {"status": "cancelled"}).status_code == 302)

print("Admin: marketplace actions")
r = post(admin, "/admin/marketplace/%d/publish" % pid)
ok("unpublish hides listing", r.status_code == 302 and anon.get("/marketplace/flask-guide-premium").status_code == 404)
post(admin, "/admin/marketplace/%d/publish" % pid)
ok("publish shows it again", anon.get("/marketplace/flask-guide-premium").status_code == 200)
ok("admin list search/filter", b"Flask Guide" in admin.get("/admin/marketplace?q=flask&status=published&kind=resource").data)
r = post(admin, "/admin/marketplace/%d/delete" % pid)
ok("delete works and the order history survives", r.status_code == 302 and anon.get("/marketplace/flask-guide-premium").status_code == 404
   and b"Flask Guide Premium" in admin.get("/admin/orders").data)
ok("visitor cannot delete", post(anon, "/admin/marketplace/1/delete").status_code == 302)
for i in range(20):
    new_product(admin, title="Bulk item %02d" % i, kind="service", category="Website development services", topic="", price_kes="100")
pg1, pg2 = admin.get("/admin/marketplace").data, admin.get("/admin/marketplace?page=2").data
ok("admin list paginates (15 per page)", b"Page 1 of 2" in pg1 and b"Page 2 of 2" in pg2 and pg1 != pg2)
ok("public list paginates (12 per page) and keeps filters", b"Page 1 of 2" in anon.get("/marketplace?kind=service").data and b"kind=service" in anon.get("/marketplace?kind=service").data and b"page=2" in anon.get("/marketplace?kind=service").data)
ok("page far past the end still renders", anon.get("/marketplace?page=999").status_code == 200)

print("Hire Me")
good = {"name": "Brian Mwangi", "email": "brian@example.com", "phone": "0722000111", "project_type": "Landing page",
        "budget": "Not sure yet", "timeline": "Within a month", "description": "I need a landing page for my bakery."}
r = post(fresh(), "/hire", dict(good, email="bad"))
ok("invalid inquiry shows errors and keeps typed values", r.status_code == 400 and b"valid email" in r.data and b"Brian Mwangi" in r.data)
ok("short description rejected", post(fresh(), "/hire", dict(good, description="short")).status_code == 400)
ok("unknown project type rejected", post(fresh(), "/hire", dict(good, project_type="Hack the NASA")).status_code == 400)
r = post(fresh(), "/hire", good)
ok("valid inquiry accepted", r.status_code == 302 and r.headers["Location"].endswith("/hire/thanks"))
ok("thanks page", b"Thank you" in anon.get("/hire/thanks").data)
ok("?type= pre-selects a service", b"selected>Landing page" in anon.get("/hire?type=Landing+page").data)
ok("inquiry without CSRF refused", fresh().post("/hire", data=good).status_code == 400)
r = admin.get("/admin/inquiries")
ok("inquiry appears in admin as new", b"Brian Mwangi" in r.data and b"New" in r.data)
ok("overview counts it", b"New website requests" in admin.get("/admin").data)
iid = sqlite3.connect(DB).execute("SELECT id FROM service_inquiries").fetchone()[0]
r = admin.get("/admin/inquiries/%d" % iid)
ok("detail page has email, WhatsApp and call links", b"mailto:brian@example.com" in r.data and b"wa.me/254722000111" in r.data)
ok("opening it marks it read", sqlite3.connect(DB).execute("SELECT is_read FROM service_inquiries").fetchone()[0] == 1)
r = post(admin, "/admin/inquiries/%d/update" % iid, {"status": "in_progress", "notes": "Called, quote sent"})
ok("status + private notes saved", r.status_code == 302 and b"In progress" in admin.get("/admin/inquiries").data and b"quote sent" in admin.get("/admin/inquiries/%d" % iid).data)
ok("bad status refused", post(admin, "/admin/inquiries/%d/update" % iid, {"status": "hacked"}).status_code == 400)
ok("filters", b"Brian" in admin.get("/admin/inquiries?status=in_progress&type=Landing+page").data and b"No requests match" in admin.get("/admin/inquiries?status=completed").data)
ok("visitor cannot open inquiries", anon.get("/admin/inquiries/%d" % iid).status_code == 302)

print("Messages inbox (existing feature + read/unread)")
c = sqlite3.connect(DB); c.execute("INSERT INTO messages (name,email,message) VALUES ('Zed','zed@example.com','Hello there')"); c.commit()
ok("message listed as unread", b"Zed" in admin.get("/admin").data and b"New" in admin.get("/admin?show=unread").data)
mid = c.execute("SELECT id FROM messages").fetchone()[0]
post(admin, "/admin/messages/%d/read" % mid)
ok("mark read", b"Zed" not in admin.get("/admin?show=unread").data and b"Zed" in admin.get("/admin?show=read").data)
ok("search by message text", b"Zed" in admin.get("/admin?search=hello").data)
post(admin, "/admin/messages/%d/delete" % mid) if False else None
ok("CSV export still works", admin.get("/admin/export.csv").status_code == 200)

print("Accounts: duplicate usernames")
a1, a2 = fresh(), fresh()
r = post(a1, "/register", {"username": "Felix_K", "password": "longenough1"})
ok("first registration works", r.status_code == 302, r.status_code)
r = post(a2, "/register", {"username": "felix_k", "password": "longenough1"})
ok("same name in different case is refused", r.status_code == 400 and b"taken" in r.data)

print()
print("RESULT:", "ALL %d CHECKS PASSED" % passed if not failed else "%d passed, %d FAILED: %s" % (passed, len(failed), "; ".join(failed)))
sys.exit(1 if failed else 0)
