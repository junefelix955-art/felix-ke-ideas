"""Phase 10: study sessions - schedule, calendar, RSVP, reminders."""
import calendar
import time
from datetime import date, datetime, timedelta

from flask import render_template, request, redirect, url_for, session, flash, abort, g

from . import common as fc

RSVP = ("going", "maybe", "no")
_last_reminder_run = [0.0]


def clean_location(text):
    """Free text or a meeting link. Links are kept only if they are http(s)."""
    t = " ".join((text or "").split())[:150]
    if t.lower().startswith(("javascript:", "data:", "vbscript:")):
        return ""
    return t


def link_of(text):
    t = (text or "").strip()
    return t if t.lower().startswith(("https://", "http://")) and " " not in t else ""


def register(app, h):
    def squad_ctx(c, sid, uid):
        sq = c.execute("SELECT id, name, archived, allow_member_sessions FROM squads WHERE id = %s", (sid,)).fetchone()
        role = fc.squad_role(c, sid, uid) if sq else None
        if not sq or not role:
            abort(404)
        return {"id": sq[0], "name": sq[1], "archived": sq[2]}, role, (fc.is_staff(role) or bool(sq[3])) and not sq[2]

    COLS = ("s.id, s.squad_id, s.creator_id, s.topic, s.description, s.starts_at, s.duration_min, s.location, s.cancelled, "
            "q.name, u.username, (SELECT COUNT(*) FROM session_rsvps r WHERE r.session_id = s.id AND r.status = 'going'), "
            "(SELECT r.status FROM session_rsvps r WHERE r.session_id = s.id AND r.user_id = %s)")
    KEYS = ("id", "squad_id", "creator_id", "topic", "description", "starts_at", "duration", "location", "cancelled", "squad", "creator", "going", "mine")

    def fix(d):
        d["ends_at"] = d["starts_at"] + timedelta(minutes=d["duration"])
        d["link"] = link_of(d["location"])
        return d

    # reminders run lazily (no background worker on the free Render plan): at most once a minute, triggered by site traffic
    @app.before_request
    def remind_due():
        if time.time() - _last_reminder_run[0] < 60 or request.endpoint in (None, "static"):
            return
        _last_reminder_run[0] = time.time()
        try:
            c = h.get_db_connection()
        except Exception:
            return
        try:
            now = fc.utcnow()
            due = c.execute("SELECT s.id, s.squad_id, s.creator_id, s.topic, s.starts_at FROM study_sessions s "
                            "WHERE NOT s.reminded AND NOT s.cancelled AND s.starts_at > %s AND s.starts_at < %s",
                            (now, now + timedelta(minutes=60))).fetchall()
            for sid_, squad_id, creator, topic, starts in due:
                claimed = c.execute("UPDATE study_sessions SET reminded = TRUE WHERE id = %s AND NOT reminded", (sid_,))
                if claimed.rowcount == 0:  # another worker already sent this reminder
                    continue
                who = c.execute("SELECT user_id FROM session_rsvps WHERE session_id = %s AND status IN ('going', 'maybe')", (sid_,)).fetchall()
                for (u,) in who:
                    h.notify(c, u, "session", "Starting soon: " + topic, "Starts at " + fc.site_time(starts, "%H:%M"), url_for("session_detail", sid=sid_))
            c.commit()
        except Exception:
            try:
                c.rollback()
            except Exception:
                pass
        finally:
            c.close()

    @app.route("/sessions")
    @h.user_required
    def study_sessions():
        uid = session["user_id"]
        today = fc.utcnow().astimezone(fc.SITE_TZ).date()
        try:
            y, m = (int(x) for x in request.args.get("m", "").split("-"))
            first = date(y, m, 1)
        except (ValueError, TypeError):
            first = today.replace(day=1)
        nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        prev = (first - timedelta(days=1)).replace(day=1)
        lo = datetime(first.year, first.month, 1, tzinfo=fc.SITE_TZ)
        hi = datetime(nxt.year, nxt.month, 1, tzinfo=fc.SITE_TZ)
        c = h.get_db_connection()
        try:
            rows = c.execute("SELECT " + COLS + " FROM study_sessions s JOIN squads q ON q.id = s.squad_id JOIN users u ON u.id = s.creator_id "
                             "JOIN squad_members sm ON sm.squad_id = s.squad_id AND sm.user_id = %s "
                             "WHERE s.starts_at >= %s AND s.starts_at < %s ORDER BY s.starts_at", (uid, uid, lo, hi)).fetchall()
            upcoming = c.execute("SELECT " + COLS + " FROM study_sessions s JOIN squads q ON q.id = s.squad_id JOIN users u ON u.id = s.creator_id "
                                 "JOIN squad_members sm ON sm.squad_id = s.squad_id AND sm.user_id = %s "
                                 "WHERE s.starts_at > %s AND NOT s.cancelled ORDER BY s.starts_at LIMIT 10", (uid, uid, fc.utcnow() - timedelta(hours=1))).fetchall()
            squads = c.execute("SELECT s.id, s.name FROM squad_members sm JOIN squads s ON s.id = sm.squad_id WHERE sm.user_id = %s AND NOT s.archived ORDER BY s.name", (uid,)).fetchall()
        finally:
            c.close()
        by_day = {}
        for r in rows:
            d = fix(dict(zip(KEYS, r)))
            by_day.setdefault(d["starts_at"].astimezone(fc.SITE_TZ).day, []).append(d)
        weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(first.year, first.month)
        return render_template("sessions.html", weeks=weeks, by_day=by_day, first=first, prev=prev, nxt=nxt, today=today,
                               upcoming=[fix(dict(zip(KEYS, r))) for r in upcoming], squads=squads, month_name=first.strftime("%B %Y"))

    @app.route("/squads/<int:sid>/sessions", methods=["GET", "POST"])
    @h.user_required
    @h.limiter.limit("20 per hour", methods=["POST"])
    def squad_sessions(sid):
        uid = session["user_id"]
        c = h.get_db_connection()
        try:
            sq, role, can_make = squad_ctx(c, sid, uid)
            if request.method == "POST":
                if not can_make:
                    abort(403)
                h.validate_csrf()
                f = request.form
                topic = h.clean_text(f.get("topic"), 100)
                starts = fc.parse_local(f.get("starts_at"))
                try:
                    dur = min(480, max(10, int(f.get("duration", "60"))))
                except ValueError:
                    dur = 60
                if len(topic) < 3:
                    flash("Give the session a topic.", "error")
                elif not starts:
                    flash("Choose a start date and time.", "error")
                elif starts < fc.utcnow() - timedelta(minutes=5):
                    flash("The start time is in the past.", "error")
                else:
                    c.execute("INSERT INTO study_sessions (squad_id, creator_id, topic, description, starts_at, duration_min, location) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                              (sid, uid, topic, h.clean_text(f.get("description"), 500), starts, dur, clean_location(f.get("location"))))
                    new = c.execute("SELECT MAX(id) FROM study_sessions WHERE creator_id = %s", (uid,)).fetchone()[0]
                    c.execute("INSERT INTO session_rsvps (session_id, user_id, status) VALUES (%s, %s, 'going')", (new, uid))
                    for (m,) in c.execute("SELECT user_id FROM squad_members WHERE squad_id = %s AND user_id <> %s", (sid, uid)).fetchall():
                        h.notify(c, m, "session", "New study session in " + sq["name"], topic + " - " + fc.site_time(starts), url_for("session_detail", sid=new))
                    c.commit()
                    flash("Study session scheduled.", "success")
                    return redirect(url_for("session_detail", sid=new))
            now = fc.utcnow()
            rows = c.execute("SELECT " + COLS + " FROM study_sessions s JOIN squads q ON q.id = s.squad_id JOIN users u ON u.id = s.creator_id "
                             "WHERE s.squad_id = %s ORDER BY s.starts_at DESC LIMIT 60", (uid, sid)).fetchall()
        finally:
            c.close()
        items = [fix(dict(zip(KEYS, r))) for r in rows]
        return render_template("squad_sessions.html", squad=sq, can_make=can_make,
                               upcoming=sorted([i for i in items if i["ends_at"] >= now], key=lambda i: i["starts_at"]),
                               past=[i for i in items if i["ends_at"] < now], f=request.form)

    def load(c, sid, uid):
        r = c.execute("SELECT " + COLS + " FROM study_sessions s JOIN squads q ON q.id = s.squad_id JOIN users u ON u.id = s.creator_id "
                      "JOIN squad_members sm ON sm.squad_id = s.squad_id AND sm.user_id = %s WHERE s.id = %s", (uid, uid, sid)).fetchone()
        if not r:
            abort(404)
        return fix(dict(zip(KEYS, r)))

    @app.route("/sessions/<int:sid>")
    @h.user_required
    def session_detail(sid):
        uid = session["user_id"]
        c = h.get_db_connection()
        try:
            d = load(c, sid, uid)
            role = fc.squad_role(c, d["squad_id"], uid)
            people = [dict(zip(("id", "username", "full_name", "avatar_v", "status"), r)) for r in c.execute(
                "SELECT u.id, u.username, u.full_name, u.avatar_v, r.status FROM session_rsvps r JOIN users u ON u.id = r.user_id "
                "WHERE r.session_id = %s ORDER BY r.status, u.username", (sid,)).fetchall()]
            archived = c.execute("SELECT archived FROM squads WHERE id = %s", (d["squad_id"],)).fetchone()[0]
        finally:
            c.close()
        return render_template("session.html", d=d, people=people, can_manage=(d["creator_id"] == uid or fc.is_staff(role)),
                               past=d["ends_at"] < fc.utcnow(), archived=archived, rsvps=RSVP)

    @app.route("/sessions/<int:sid>/rsvp", methods=["POST"])
    @h.user_required
    @h.limiter.limit("60 per minute")
    def session_rsvp(sid):
        h.validate_csrf()
        uid = session["user_id"]
        status = request.form.get("status")
        if status not in RSVP:
            abort(400)
        c = h.get_db_connection()
        try:
            d = load(c, sid, uid)
            if d["cancelled"]:
                flash("This session was cancelled.", "error")
            else:
                if c.execute("SELECT 1 FROM session_rsvps WHERE session_id = %s AND user_id = %s", (sid, uid)).fetchone():
                    c.execute("UPDATE session_rsvps SET status = %s WHERE session_id = %s AND user_id = %s", (status, sid, uid))
                else:
                    c.execute("INSERT INTO session_rsvps (session_id, user_id, status) VALUES (%s, %s, %s)", (sid, uid, status))
                c.commit()
        finally:
            c.close()
        return redirect(url_for("session_detail", sid=sid))

    @app.route("/sessions/<int:sid>/cancel", methods=["POST"])
    @h.user_required
    def session_cancel(sid):
        h.validate_csrf()
        uid = session["user_id"]
        c = h.get_db_connection()
        try:
            d = load(c, sid, uid)
            if d["creator_id"] != uid and not fc.is_staff(fc.squad_role(c, d["squad_id"], uid)):
                abort(403)
            if not d["cancelled"]:
                c.execute("UPDATE study_sessions SET cancelled = TRUE WHERE id = %s", (sid,))
                for (u,) in c.execute("SELECT user_id FROM session_rsvps WHERE session_id = %s AND user_id <> %s", (sid, uid)).fetchall():
                    h.notify(c, u, "session", "Session cancelled: " + d["topic"], "", url_for("session_detail", sid=sid))
                c.commit()
                flash("Session cancelled and attendees told.", "success")
        finally:
            c.close()
        return redirect(url_for("session_detail", sid=sid))

    @app.route("/sessions/<int:sid>/reschedule", methods=["POST"])
    @h.user_required
    def session_reschedule(sid):
        h.validate_csrf()
        uid = session["user_id"]
        starts = fc.parse_local(request.form.get("starts_at"))
        c = h.get_db_connection()
        try:
            d = load(c, sid, uid)
            if d["creator_id"] != uid and not fc.is_staff(fc.squad_role(c, d["squad_id"], uid)):
                abort(403)
            if not starts or starts < fc.utcnow() - timedelta(minutes=5):
                flash("Choose a valid future time.", "error")
            else:
                c.execute("UPDATE study_sessions SET starts_at = %s, reminded = FALSE, cancelled = FALSE WHERE id = %s", (starts, sid))
                for (u,) in c.execute("SELECT user_id FROM session_rsvps WHERE session_id = %s AND user_id <> %s", (sid, uid)).fetchall():
                    h.notify(c, u, "session", "New time: " + d["topic"], fc.site_time(starts), url_for("session_detail", sid=sid))
                c.commit()
                flash("Session rescheduled.", "success")
        finally:
            c.close()
        return redirect(url_for("session_detail", sid=sid))
