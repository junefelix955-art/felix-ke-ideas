r"""Phase 3 smoke test: tutoring requests, advertising enquiries, sponsored labels, newsletter, announcements,
WhatsApp/social links, share links and the Maths Sprint challenge link.   py tests\phase3_smoke.py"""
import io, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shim import *
from datetime import timedelta
from features import growth

PDF = b"%PDF-1.4\n" + b"x\n" * 30
def db(sql, *p):
    c = raw(); r = c.execute(sql, p).fetchall(); c.close(); return r
anon, admin = fresh(), fresh()
post(admin, "/login", {"username": "admin", "password": "correct horse"})
with admin.session_transaction() as s:
    s["_csrf_token"] = "tok"

print("Pages, nav, footer, WhatsApp and social links")
for p in ("/", "/tutoring", "/advertise", "/updates", "/marketplace", "/hire", "/play"):
    ok(p + " loads", anon.get(p).status_code == 200)
home = anon.get("/").data
ok("nav has the Services menu", b"<summary>Services</summary>" in home and b"Online tutoring" in home and b"Advertise with us" in home)
ok("footer shows the WhatsApp Channel link", b"https://whatsapp.com/channel/0029VbEEsiAHVvTk3uyqwg08" in home and b"Follow the WhatsApp Channel" in home)
ok("floating WhatsApp contact button on public pages", b"wa-float" in home and b"wa.me/254700111222" in home)
ok("no floating button inside the admin area", b"wa-float" not in admin.get("/admin").data)
ok("newsletter form in the footer", b'action="/newsletter"' in home)
os.environ["WHATSAPP_CHANNEL_URL"] = "https://evil.example.com/channel/x"
ok("a non-WhatsApp channel URL setting is ignored", b"evil.example.com" not in anon.get("/").data and b"Follow the WhatsApp Channel" not in anon.get("/").data)
os.environ["WHATSAPP_CHANNEL_URL"] = "https://whatsapp.com/channel/OtherChannel123"
ok("channel link can be changed by setting", b"OtherChannel123" in anon.get("/").data)
os.environ.pop("WHATSAPP_CHANNEL_URL")
os.environ.update(FACEBOOK_URL="https://facebook.com/felixke", INSTAGRAM_URL="javascript:alert(1)", GITHUB_URL="http://insecure.example.com")
h = anon.get("/").data
ok("social links: only valid https ones are shown", b"facebook.com/felixke" in h and b"javascript:alert" not in h and b"insecure.example.com" not in h and b"Instagram" not in h)
for k in ("FACEBOOK_URL", "INSTAGRAM_URL", "GITHUB_URL"): os.environ.pop(k)

print("Tutoring requests")
good = {"name": "Grace Wanjiru", "email": "grace@example.com", "phone": "0711222333", "subject": "Python programming", "level": "Beginner",
        "mode": "online", "preferred_time": "Evening", "duration": "60", "notes": "I want to learn to build websites."}
def book(data, cl=None): return post(cl or fresh(), "/tutoring", data)
future = (growth.today() + timedelta(days=10)).isoformat()
past = (growth.today() - timedelta(days=2)).isoformat()
ok("six subjects listed with objectives", all(s.encode() in anon.get("/tutoring").data for s in growth.SUBJECTS) and b"You will learn to" in anon.get("/tutoring").data)
ok("no prices are invented", b"Price on request" in anon.get("/tutoring").data and b"From KES" not in anon.get("/tutoring").data)
ok("?subject= pre-selects", b"selected>Excel" in anon.get("/tutoring?subject=Excel").data)
for label, bad, text in (("bad email", dict(good, email="nope"), b"valid email"), ("unknown subject", dict(good, subject="Hacking"), b"what you want to learn"),
                         ("bad level", dict(good, level="Wizard"), b"your level"), ("bad mode", dict(good, mode="teleport"), b"online, in person"),
                         ("bad duration", dict(good, duration="999"), b"session length"), ("past date", dict(good, preferred_date=past), b"from today"),
                         ("junk date", dict(good, preferred_date="tomorrow"), b"from today"), ("short name", dict(good, name="G"), b"your name"),
                         ("bad phone", dict(good, phone="abc"), b"digits only")):
    r = book(bad)
    ok("rejects " + label, r.status_code == 400 and text in r.data, r.status_code)
ok("typed values survive an error", b"Grace Wanjiru" in book(dict(good, email="nope")).data)
ok("no row saved for invalid requests", db("SELECT COUNT(*) FROM tutoring_bookings")[0][0] == 0)
ok("honeypot swallows bots", book(dict(good, website="spam")).status_code == 302 and db("SELECT COUNT(*) FROM tutoring_bookings")[0][0] == 0)
ok("request without CSRF token refused", fresh().post("/tutoring", data=good).status_code == 400)
r = book(dict(good, preferred_date=future))
ok("valid request accepted and saved", r.status_code == 302 and r.headers["Location"].endswith("/tutoring/thanks") and db("SELECT COUNT(*) FROM tutoring_bookings")[0][0] == 1)
ok("thanks page", b"Thank you" in anon.get("/tutoring/thanks").data and b"Nothing is charged" in anon.get("/tutoring/thanks").data)
ok("request without a date is allowed", book(dict(good, name="Peter Kamau", email="peter@example.com", subject="Mathematics")).status_code == 302)

print("Tutoring admin")
for p in ("/admin/bookings", "/admin/bookings/1", "/admin/ads", "/admin/updates", "/admin/subscribers", "/admin/subscribers.csv"):
    ok(p + " closed to visitors", anon.get(p).status_code == 302)
html = admin.get("/admin/bookings").data.decode()
ok("requests listed as new", "Grace Wanjiru" in html and "Peter Kamau" in html and "New" in html)
ok("filters", "Peter" not in admin.get("/admin/bookings?subject=Python+programming").data.decode() and "Grace" in admin.get("/admin/bookings?q=grace&status=pending").data.decode()
   and b"No requests match" in admin.get("/admin/bookings?status=confirmed").data and b"No requests match" in admin.get("/admin/bookings?from=2000-01-01&to=2000-01-02").data)
r = admin.get("/admin/bookings/1")
ok("detail has contact links (email, WhatsApp, call)", b"mailto:grace@example.com" in r.data and b"wa.me/254711222333" in r.data and b"tel:0711222333" in r.data)
ok("opening marks it read", db("SELECT is_read FROM tutoring_bookings WHERE id=1")[0][0] == 1)
r = post(admin, "/admin/bookings/1/update", {"status": "confirmed", "session_date": "", "session_time": "4pm", "notes": "n"})
ok("cannot confirm without a session date", db("SELECT status FROM tutoring_bookings WHERE id=1")[0][0] == "pending" and r.status_code == 302)
sd = (growth.today() + timedelta(days=12)).isoformat()
post(admin, "/admin/bookings/1/update", {"status": "confirmed", "session_date": sd, "session_time": "4:00 pm", "notes": "Zoom link sent"})
ok("confirm with date and time", db("SELECT status, session_time FROM tutoring_bookings WHERE id=1")[0] == ("confirmed", "4:00 pm") and b"4:00 pm" in admin.get("/admin/bookings").data)
sd2 = (growth.today() + timedelta(days=14)).isoformat()
post(admin, "/admin/bookings/1/update", {"status": "rescheduled", "session_date": sd2, "session_time": "5 pm", "notes": ""})
ok("reschedule", db("SELECT status FROM tutoring_bookings WHERE id=1")[0][0] == "rescheduled" and sd2.encode() in admin.get("/admin/bookings/1").data)
post(admin, "/admin/bookings/2/update", {"status": "cancelled", "session_date": "", "session_time": "", "notes": ""})
ok("cancel (no date needed)", db("SELECT status FROM tutoring_bookings WHERE id=2")[0][0] == "cancelled")
ok("bad status refused", post(admin, "/admin/bookings/1/update", {"status": "hacked"}).status_code == 400)
ok("visitor cannot update bookings", post(anon, "/admin/bookings/1/update", {"status": "cancelled"}).status_code == 302 and db("SELECT status FROM tutoring_bookings WHERE id=1")[0][0] == "rescheduled")
post(admin, "/admin/bookings/2/read")
ok("mark unread toggle", db("SELECT is_read FROM tutoring_bookings WHERE id=2")[0][0] == 0)

print("Advertising enquiries")
adgood = {"name": "Joy Achieng", "organisation": "BrightPath Academy", "email": "joy@brightpath.example", "website": "https://brightpath.example",
          "placement": "Sponsored educational content", "budget": growth.AD_BUDGETS[2], "description": "We would like to publish a labelled maths tutorial for secondary students."}
def adpost(d): return post(fresh(), "/advertise", d)
ok("placements and standards shown", b"Our advertising standards" in anon.get("/advertise").data and b"Sponsored</span>" in anon.get("/advertise").data
   and b"WhatsApp Channel mention" in anon.get("/advertise").data)
for label, bad, text in (("bad email", dict(adgood, email="x"), b"valid email"), ("no organisation", dict(adgood, organisation=""), b"organisation"),
                         ("javascript: website", dict(adgood, website="javascript:alert(1)"), b"https://"), ("http-less website", dict(adgood, website="example.com"), b"https://"),
                         ("unknown placement", dict(adgood, placement="Hijack"), b"kind of placement"), ("bad budget", dict(adgood, budget="1 billion"), b"budget range"),
                         ("short description", dict(adgood, description="ad"), b"at least 30")):
    r = adpost(bad)
    ok("rejects " + label, r.status_code == 400 and text in r.data, r.status_code)
ok("honeypot", adpost(dict(adgood, website_url_hp="x")).status_code == 302 and db("SELECT COUNT(*) FROM ad_inquiries")[0][0] == 0)
ok("valid enquiry saved", adpost(adgood).status_code == 302 and db("SELECT COUNT(*) FROM ad_inquiries")[0][0] == 1 and b"Thank you" in anon.get("/advertise/thanks").data)
ok("optional website can be empty", adpost(dict(adgood, website="", organisation="Other Co", email="o@x.com")).status_code == 302)
ok("?placement= pre-selects", b"selected>WhatsApp Channel mention" in anon.get("/advertise?placement=WhatsApp+Channel+mention").data)
ok("admin lists enquiries with filters", b"BrightPath Academy" in admin.get("/admin/ads").data and b"Other Co" not in admin.get("/admin/ads?q=bright").data
   and b"No enquiries match" in admin.get("/admin/ads?status=approved").data)
r = admin.get("/admin/ads/1")
ok("detail marks read, has reply link and review checklist", b"mailto:joy@brightpath.example" in r.data and b"labelled" in r.data and db("SELECT is_read FROM ad_inquiries WHERE id=1")[0][0] == 1)
ok("website shown as a nofollow external link", b'rel="noopener noreferrer nofollow"' in r.data)
post(admin, "/admin/ads/1/update", {"status": "approved", "notes": "Checked claims"})
ok("status + notes saved", db("SELECT status, admin_notes FROM ad_inquiries WHERE id=1")[0] == ("approved", "Checked claims"))
ok("bad ad status refused", post(admin, "/admin/ads/1/update", {"status": "x"}).status_code == 400)

print("Sponsored labels")
def new_product(**over):
    d = {"title": "Algebra Tutorial", "summary": "s", "description": "d", "kind": "resource", "category": "Mathematics and statistics materials", "topic": "Mathematics",
         "price_kes": "0", "availability": "available", "published": "on", "file": (io.BytesIO(PDF), "a.pdf")}
    d.update(over)
    return post(admin, "/admin/marketplace/new", d, content_type="multipart/form-data")
ok("sponsored listing created", new_product(sponsored="on").status_code == 302 and new_product(title="Plain Notes").status_code == 302)
m = anon.get("/marketplace").data.decode()
cards = m.split('<article class="shop-card">')
sp = [c for c in cards if "Algebra Tutorial" in c][0]; plain = [c for c in cards if "Plain Notes" in c][0]
ok("sponsored card is labelled, plain card is not", "Sponsored" in sp and "Sponsored" not in plain)
main_tags = lambda slug: anon.get("/marketplace/" + slug).data.decode().split('class="shop-tags"')[1].split("</p>")[0]
ok("labelled on the product page too (its own tags)", "Sponsored" in main_tags("algebra-tutorial") and "Sponsored" not in main_tags("plain-notes"))
ok("a related sponsored item keeps its label on other pages", 'badge sponsored' in anon.get("/marketplace/plain-notes").data.decode())
pid = db("SELECT id FROM products WHERE slug='algebra-tutorial'")[0][0]
post(admin, "/admin/marketplace/%d/edit" % pid, {"title": "Algebra Tutorial", "kind": "resource", "category": "Mathematics and statistics materials", "topic": "Mathematics",
     "price_kes": "0", "availability": "available", "published": "on"}, content_type="multipart/form-data")
ok("un-ticking Sponsored removes the label", "Sponsored" not in main_tags("algebra-tutorial"))
post(admin, "/admin/marketplace/%d/edit" % pid, {"title": "Algebra Tutorial", "kind": "resource", "category": "Mathematics and statistics materials", "topic": "Mathematics",
     "price_kes": "0", "availability": "available", "published": "on", "sponsored": "on"}, content_type="multipart/form-data")
ok("ticking it again restores the label", "Sponsored" in main_tags("algebra-tutorial"))

print("Share links")
pg = anon.get("/marketplace/algebra-tutorial").data.decode()
ok("product page share row has WhatsApp / Facebook / X / copy with the full address", "wa.me/?text=" in pg and "facebook.com/sharer" in pg and "twitter.com/intent/tweet" in pg
   and "data-copy=\"http" in pg and "marketplace/algebra-tutorial" in pg)
ok("Maths Sprint: challenge banner", b"A friend scored <strong>25</strong>" in anon.get("/play?challenge=25").data)
ok("Maths Sprint: challenge is clamped and cannot inject", b"<strong>100</strong>" in anon.get("/play?challenge=99999").data and b"<script>alert" not in anon.get("/play?challenge=%3Cscript%3Ealert(1)%3C/script%3E").data
   and b"A friend scored" not in anon.get("/play?challenge=abc").data and b"A friend scored" not in anon.get("/play?challenge=-5").data)
ok("Maths Sprint: share controls ready for after the game", b'id="g-share"' in anon.get("/play").data and b"?challenge=" in anon.get("/play").data)

print("Newsletter")
def sub(email, cl=None, **extra): return post(cl or fresh(), "/newsletter", dict({"email": email, "next": "/learn"}, **extra))
r = sub("not-an-email")
ok("invalid email refused with a message", r.status_code == 302 and db("SELECT COUNT(*) FROM newsletter_subscribers")[0][0] == 0)
cl = fresh(); r = sub("Reader@Example.com", cl)
ok("subscribe works and returns to the page", r.status_code == 302 and r.headers["Location"].endswith("/learn") and db("SELECT COUNT(*) FROM newsletter_subscribers")[0][0] == 1)
ok("friendly confirmation shown", b"on the list" in cl.get("/learn").data)
sub("reader@example.com"); sub("READER@EXAMPLE.COM")
ok("same address in any capitalisation is stored once", db("SELECT COUNT(*) FROM newsletter_subscribers")[0][0] == 1)
c2 = fresh(); sub("reader@example.com", c2)
ok("same message for an existing address (no way to probe the list)", b"on the list" in c2.get("/learn").data)
ok("honeypot", sub("bot@example.com", company="x").status_code == 302 and db("SELECT COUNT(*) FROM newsletter_subscribers")[0][0] == 1)
ok("open redirect blocked", sub("a@b.co", next="https://evil.example.com").headers["Location"].startswith("/") or "evil" not in sub("a2@b.co", next="https://evil.example.com").headers["Location"])
ok("subscribe without CSRF token refused", fresh().post("/newsletter", data={"email": "z@z.co"}).status_code == 400)
tok = db("SELECT token FROM newsletter_subscribers WHERE lower(email)='reader@example.com'")[0][0]
ok("unsubscribe page does not unsubscribe by merely opening it", anon.get("/newsletter/unsubscribe/" + tok).status_code == 200 and db("SELECT COUNT(*) FROM newsletter_subscribers WHERE token=?", tok)[0][0] == 1)
ok("junk unsubscribe token is a 404", anon.get("/newsletter/unsubscribe/short").status_code == 404)
sub("=cmd|' /C calc'!A0@evil.com"); sub("=HYPERLINK@x.com")
ok("subscribers page and search", b"Reader@Example.com" in admin.get("/admin/subscribers").data and b"No subscribers yet" not in admin.get("/admin/subscribers?q=reader").data)
csvd = admin.get("/admin/subscribers.csv")
ok("CSV export: attachment, with unsubscribe links", csvd.status_code == 200 and "attachment" in csvd.headers["Content-Disposition"] and b"newsletter/unsubscribe/" in csvd.data)
ok("CSV export neutralises spreadsheet formulas", b"'=HYPERLINK@x.com" in csvd.data and b"\n=HYPERLINK" not in csvd.data)
sid = db("SELECT id FROM newsletter_subscribers WHERE email LIKE '=HYPER%'")[0][0]
post(admin, "/admin/subscribers/%d/delete" % sid)
ok("admin can remove a subscriber", db("SELECT COUNT(*) FROM newsletter_subscribers WHERE id=?", sid)[0][0] == 0)
r = post(fresh(), "/newsletter/unsubscribe/" + tok)
ok("unsubscribe works (button)", b"unsubscribed" in r.data and db("SELECT COUNT(*) FROM newsletter_subscribers WHERE token=?", tok)[0][0] == 0)

print("Announcements")
def ann(**d): return post(admin, "/admin/updates/new", d)
ann(title="New Python notes", body="Free beginner notes are now available.", link="/learn")
ann(title="Hidden one", body="", link="", unpublished="1")
ann(title="<script>alert(1)</script>", body="x", link="")
ann(title="Bad link", body="", link="javascript:alert(1)"); ann(title="Protocol-relative", body="", link="//evil.example.com")
ann(title="Ok external", body="", link="https://example.com/news")
titles = [r[0] for r in db("SELECT title FROM announcements ORDER BY id")]
ok("valid announcements saved, dangerous links refused", titles == ["New Python notes", "Hidden one", "<script>alert(1)</script>", "Ok external"], titles)
u = anon.get("/updates").data
ok("public page shows published ones only, HTML escaped", b"New Python notes" in u and b"Hidden one" not in u and b"&lt;script&gt;" in u and b"<script>alert(1)" not in u)
ok("external links open safely", b'href="https://example.com/news" target="_blank" rel="noopener noreferrer"' in u)
ok("home page shows the latest updates and the channel button", b"Latest updates" in anon.get("/").data and b"Ok external" in anon.get("/").data)
uid = db("SELECT id FROM announcements WHERE title='Hidden one'")[0][0]
post(admin, "/admin/updates/%d/publish" % uid)
ok("publish toggle", b"Hidden one" in anon.get("/updates").data)
post(admin, "/admin/updates/%d/delete" % uid)
ok("delete", b"Hidden one" not in anon.get("/updates").data and db("SELECT COUNT(*) FROM announcements WHERE id=?", uid)[0][0] == 0)
ok("visitors cannot post or delete", post(anon, "/admin/updates/new", {"title": "spam"}).status_code == 302 and post(anon, "/admin/updates/1/delete").status_code == 302)

print("Dashboard")
d = admin.get("/admin").data
ok("dashboard shows the new counts", b"Tutoring requests pending" in d and b"New advertising enquiries" in d and b"Newsletter subscribers" in d)
ok("admin tabs link to the new sections", all(x in d for x in (b"/admin/bookings", b"/admin/ads", b"/admin/updates")))
finish()
