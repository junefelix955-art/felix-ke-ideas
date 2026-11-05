r"""Read-only health and security check for your site. It only LOADS pages (never logs in, never posts anything).

Usage (from your project folder):
    py tools\check_site.py http://127.0.0.1:5000
    py tools\check_site.py https://felix-ke-ideas.onrender.com

The free Render plan sleeps when idle, so the first request can take up to a minute. That is normal.
"""
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5000").rstrip("/")
PUBLIC = ["/", "/about", "/contact", "/privacy", "/community", "/chat", "/play", "/signin", "/register", "/api/stats"]
PRIVATE = ["/dashboard", "/buddies", "/squads", "/squads/1", "/messages", "/messages/1", "/library", "/library/1", "/solver", "/leaderboard",
           "/sessions", "/sessions/1", "/notifications", "/settings", "/questions/1", "/quizzes/1", "/files/1", "/admin", "/admin/export.csv"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


opener = urllib.request.build_opener(NoRedirect)
problems, checked = [], 0


def get(path):
    try:
        r = opener.open(urllib.request.Request(BASE + path, headers={"User-Agent": "site-check"}), timeout=90)
        return r.status, r.headers, r.read(200000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read(200000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        print("\nCould not reach", BASE + path, "->", getattr(e, "reason", e))
        print("Is the site running? Is the address right? (Free Render sites can need a minute to wake up; try again.)")
        sys.exit(2)


def ok(name, cond, extra=""):
    global checked
    checked += 1
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else "  " + str(extra)))
    if not cond:
        problems.append(name)


print("Checking", BASE)
print("Public pages load:")
for p in PUBLIC:
    code, h, body = get(p)
    ok(p + " -> 200", code == 200, code)
print("Private pages are closed to visitors who are not signed in:")
for p in PRIVATE:
    code, h, body = get(p)
    ok(p + " -> redirect/denied (not 200)", code in (301, 302, 303, 307, 308, 401, 403, 404), code)
    if code == 200:
        ok(p + " shows no private content", False, "page opened without signing in")
print("Security headers on the home page:")
code, h, body = get("/")
for name in ("X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy", "Content-Security-Policy", "Permissions-Policy"):
    ok(name, bool(h.get(name)))
if BASE.startswith("https://"):
    ok("Strict-Transport-Security (HTTPS only)", bool(h.get("Strict-Transport-Security")))
    cookies = " ".join(h.get_all("Set-Cookie") or [])
    if cookies:
        ok("session cookie is Secure + HttpOnly", "Secure" in cookies and "HttpOnly" in cookies, cookies[:120])
print("Error pages do not leak details:")
code, h, body = get("/this-page-does-not-exist")
ok("unknown page -> friendly 404", code == 404 and "Traceback" not in body)
code, h, body = get("/files/999999")
ok("unknown file -> 404 (or sign-in)", code in (302, 303, 404))
print("")
print("RESULT:", "ALL %d CHECKS PASSED" % checked if not problems else "%d of %d checks FAILED: %s" % (len(problems), checked, "; ".join(problems)))
sys.exit(1 if problems else 0)
