"""Helpers shared by all student-platform features.
This module never imports app.py, so there are no circular imports."""
import io
import os
import re
import zipfile
from datetime import datetime, timedelta, timezone

from flask import Response

# Kenya (EAT) has no daylight saving, so a fixed offset is exact. Override with SITE_UTC_OFFSET_HOURS if needed.
SITE_TZ = timezone(timedelta(hours=float(os.getenv("SITE_UTC_OFFSET_HOURS", "3"))))
ONLINE_SECONDS = 120


def utcnow():
    return datetime.now(timezone.utc)


def is_online(dt):
    return bool(dt) and (utcnow() - dt).total_seconds() < ONLINE_SECONDS


def site_time(dt, fmt="%d %b %Y, %H:%M"):
    return dt.astimezone(SITE_TZ).strftime(fmt) if dt else ""


def parse_local(value):
    """'2026-11-05T16:30' typed in the site's timezone -> aware UTC datetime (or None)."""
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%dT%H:%M").replace(tzinfo=SITE_TZ).astimezone(timezone.utc)
    except ValueError:
        return None


def local_input(dt):
    """aware datetime -> value for <input type=datetime-local> in site time."""
    return dt.astimezone(SITE_TZ).strftime("%Y-%m-%dT%H:%M") if dt else ""


def pair(a, b):
    return (a, b) if a < b else (b, a)


# ---------------------------------------------------------------- files
TEXT_EXTS = {"txt", "md", "py", "js", "java", "c", "cpp", "h", "cs", "r", "sql", "csv", "json", "css", "sh", "m"}
KIND_EXT = {"pdf": ".pdf", "docx": ".docx", "doc": ".doc", "pptx": ".pptx", "png": ".png", "jpeg": ".jpg", "webp": ".webp"}
KIND_MIME = {"pdf": "application/pdf", "doc": "application/msword", "png": "image/png", "jpeg": "image/jpeg",
             "webp": "image/webp", "text": "text/plain; charset=utf-8",
             "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}


def sniff_file(data, filename, allowed):
    """Work out what a file REALLY is from its bytes (never from the browser-supplied type or name).
    Returns {"kind", "mime", "preview"} or None when it is not an allowed type."""
    kind = None
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if data.startswith(b"%PDF-"):
        kind = "pdf"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "png"
    elif data.startswith(b"\xff\xd8\xff"):
        kind = "jpeg"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = "webp"
    elif data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                infos = z.infolist()
                if len(infos) > 3000 or sum(i.file_size for i in infos) > 80 * 1024 * 1024:
                    return None  # zip-bomb guard
                names = set(z.namelist())
            if "word/document.xml" in names:
                kind = "docx"
            elif "ppt/presentation.xml" in names:
                kind = "pptx"
        except zipfile.BadZipFile:
            return None
    elif data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") and ext == "doc":
        kind = "doc"
    elif ext in TEXT_EXTS and b"\x00" not in data:
        try:
            data.decode("utf-8")
            kind = "text"
        except UnicodeDecodeError:
            return None
    if kind is None:
        return None
    group = "image" if kind in ("png", "jpeg", "webp") else kind
    if group not in allowed:
        return None
    return {"kind": kind, "mime": KIND_MIME[kind], "preview": kind in ("pdf", "png", "jpeg", "webp")}


def clean_filename(name, kind):
    base = re.sub(r"[^A-Za-z0-9._() -]+", "_", (name or "file").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]).strip(" ._") or "file"
    base = base[:80]
    ext = KIND_EXT.get(kind) or ("." + base.rsplit(".", 1)[-1].lower() if "." in base else ".txt")
    stem = base.rsplit(".", 1)[0] if "." in base else base
    return (stem or "file")[:70] + ext


def file_response(data, mime, filename, kind, download=False):
    resp = Response(bytes(data), mimetype=mime.split(";")[0])
    if mime.startswith("text/"):
        resp.headers["Content-Type"] = mime
    inline = kind in ("pdf", "png", "jpeg", "webp") and not download
    resp.headers.set("Content-Disposition", "inline" if inline else "attachment", filename=filename)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    if kind in ("png", "jpeg", "webp"):
        resp.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self' data:"
    elif kind == "pdf" and inline:  # the library page previews PDFs in a same-site frame
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
    return resp


def human_size(n):
    return "%d KB" % max(1, round(n / 1024)) if n < 1024 * 1024 else "%.1f MB" % (n / 1024 / 1024)


# ---------------------------------------------------------------- points
# Points reward meaningful contributions. Chat messages earn NOTHING, and every rule has a daily cap.
POINT_RULES = {
    "answer": (5, 10),      # answered a classmate's maths question (not your own) - max 10/day
    "helpful": (10, 30),    # a classmate marked your explanation as helpful
    "resource": (15, 3),    # shared a library resource - max 3/day
    "question": (2, 5),     # shared a question with a squad - max 5/day
    "quiz": (None, 20),     # finished a squad quiz: 2 points per correct answer
}


def award(c, uid, kind, amount=None, squad_id=None, ref=None):
    """Give points once per (user, kind, ref) and never beyond the daily cap. Returns points given."""
    per, cap = POINT_RULES[kind]
    amount = per if amount is None else amount
    if amount <= 0:
        return 0
    if ref is not None and c.execute("SELECT 1 FROM points WHERE user_id = %s AND kind = %s AND ref = %s",
                                     (uid, kind, ref)).fetchone():
        return 0
    since = utcnow() - timedelta(days=1)
    if c.execute("SELECT COUNT(*) FROM points WHERE user_id = %s AND kind = %s AND created_at > %s",
                 (uid, kind, since)).fetchone()[0] >= cap:
        return 0
    c.execute("INSERT INTO points (user_id, squad_id, kind, amount, ref) VALUES (%s, %s, %s, %s, %s)",
              (uid, squad_id, kind, amount, ref))
    return amount


def total_points(c, uid, days=None):
    if days:
        return c.execute("SELECT COALESCE(SUM(amount), 0) FROM points WHERE user_id = %s AND created_at > %s",
                         (uid, utcnow() - timedelta(days=days))).fetchone()[0]
    return c.execute("SELECT COALESCE(SUM(amount), 0) FROM points WHERE user_id = %s", (uid,)).fetchone()[0]


# ---------------------------------------------------------------- relationships and squads
def friendship(c, a, b):
    """(id, status, requester_id) or None."""
    x, y = pair(a, b)
    return c.execute("SELECT id, status, requester_id FROM friendships WHERE user_a = %s AND user_b = %s", (x, y)).fetchone()


def are_friends(c, a, b):
    f = friendship(c, a, b)
    return bool(f and f[1] == "accepted")


def squad_role(c, sid, uid):
    r = c.execute("SELECT role FROM squad_members WHERE squad_id = %s AND user_id = %s", (sid, uid)).fetchone()
    return r[0] if r else None


def is_staff(role):
    return role in ("admin", "moderator")


def post_system_message(c, thread_id, uid, text):
    c.execute("INSERT INTO thread_messages (thread_id, user_id, body, kind) VALUES (%s, %s, %s, 'system')",
              (thread_id, uid, text[:200]))


def add_squad_member(c, sid, uid, role="member", announce=True):
    c.execute("INSERT INTO squad_members (squad_id, user_id, role) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (sid, uid, role))
    t = c.execute("SELECT id FROM threads WHERE squad_id = %s", (sid,)).fetchone()
    if t:
        base = c.execute("SELECT COALESCE(MAX(id), 0) FROM thread_messages WHERE thread_id = %s", (t[0],)).fetchone()[0]
        # new members start "caught up" so old history does not show as unread
        c.execute("INSERT INTO thread_members (thread_id, user_id, last_delivered_id, last_read_id) VALUES (%s, %s, %s, %s) "
                  "ON CONFLICT DO NOTHING", (t[0], uid, base, base))
        if announce:
            name = c.execute("SELECT username FROM users WHERE id = %s", (uid,)).fetchone()[0]
            post_system_message(c, t[0], uid, name + " joined the squad")


def remove_squad_member(c, sid, uid, text=None):
    c.execute("DELETE FROM squad_members WHERE squad_id = %s AND user_id = %s", (sid, uid))
    t = c.execute("SELECT id FROM threads WHERE squad_id = %s", (sid,)).fetchone()
    if t:
        c.execute("DELETE FROM thread_members WHERE thread_id = %s AND user_id = %s", (t[0], uid))
        if text:
            post_system_message(c, t[0], uid, text)


def unread_messages(c, uid):
    return c.execute(
        "SELECT COUNT(*) FROM thread_messages m JOIN thread_members tm ON tm.thread_id = m.thread_id AND tm.user_id = %s "
        "WHERE m.id > tm.last_read_id AND m.user_id <> %s AND NOT m.deleted AND m.kind <> 'system'", (uid, uid)).fetchone()[0]


def display(u_full, u_name):
    return u_full or u_name


# ---------------------------------------------------------------- shared maths questions
CATEGORIES_MATH = ["Algebra", "Calculus", "Geometry", "Trigonometry", "Statistics", "Linear Algebra", "Arithmetic", "Other"]
DIFFICULTIES = ["Easy", "Medium", "Hard"]


def post_question(c, sid, uid, body, category, difficulty, solution=""):
    """Create a shared question card in the squad's chat. Caller checks membership and commits. Returns question id."""
    category = category if category in CATEGORIES_MATH else "Other"
    difficulty = difficulty if difficulty in DIFFICULTIES else "Medium"
    c.execute("INSERT INTO math_questions (squad_id, user_id, body, category, difficulty, solution) VALUES (%s, %s, %s, %s, %s, %s)",
              (sid, uid, body[:1500], category, difficulty, solution[:3000]))
    qid = c.execute("SELECT MAX(id) FROM math_questions WHERE user_id = %s", (uid,)).fetchone()[0]
    t = c.execute("SELECT id FROM threads WHERE squad_id = %s", (sid,)).fetchone()
    if t:
        c.execute("INSERT INTO thread_messages (thread_id, user_id, body, kind, ref_id) VALUES (%s, %s, '', 'question', %s)", (t[0], uid, qid))
    award(c, uid, "question", squad_id=sid, ref=qid)
    return qid
