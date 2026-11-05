"""Phase 3: Buddies (friends) - search, requests, accept/reject, remove, online status."""
from flask import render_template, request, redirect, url_for, session, flash, abort

from . import common as fc

TABS = ("friends", "requests", "find")
USER_COLS = "u.id, u.username, u.full_name, u.university, u.course, u.avatar_v, u.last_active, u.show_university, u.show_course"


def card(r):
    """Row -> dict. University/course only appear if the member chose to show them."""
    return {"id": r[0], "username": r[1], "full_name": r[2], "avatar_v": r[5], "last_active": r[6],
            "online": fc.is_online(r[6]), "university": r[3] if r[7] else "", "course": r[4] if r[8] else ""}


def register(app, h):
    def back(default=None):
        return redirect(h.safe_next(request.form.get("next"), default or url_for("buddies")))

    @app.route("/buddies")
    @h.user_required
    def buddies():
        uid = session["user_id"]
        tab = request.args.get("tab") if request.args.get("tab") in TABS else "friends"
        q = request.args.get("q", "").strip()[:40]
        c = h.get_db_connection()
        try:
            rels = {}
            for fid, status, req, a, b in c.execute(
                    "SELECT id, status, requester_id, user_a, user_b FROM friendships WHERE user_a = %s OR user_b = %s",
                    (uid, uid)).fetchall():
                rels[b if a == uid else a] = {"id": fid, "status": status,
                                              "state": "friends" if status == "accepted" else ("sent" if req == uid else "received")}
            friends = [dict(card(r), rel=rels[r[0]]) for r in c.execute(
                "SELECT " + USER_COLS + " FROM friendships f JOIN users u ON u.id = CASE WHEN f.user_a = %s THEN f.user_b ELSE f.user_a END "
                "WHERE f.status = 'accepted' AND (f.user_a = %s OR f.user_b = %s) "
                "ORDER BY u.last_active DESC NULLS LAST, u.username", (uid, uid, uid)).fetchall()]
            incoming = [dict(card(r), rel=rels[r[0]]) for r in c.execute(
                "SELECT " + USER_COLS + " FROM friendships f JOIN users u ON u.id = f.requester_id "
                "WHERE f.status = 'pending' AND f.requester_id <> %s AND (f.user_a = %s OR f.user_b = %s) ORDER BY f.id DESC",
                (uid, uid, uid)).fetchall()]
            outgoing = [dict(card(r), rel=rels[r[0]]) for r in c.execute(
                "SELECT " + USER_COLS + " FROM friendships f JOIN users u ON u.id = CASE WHEN f.user_a = %s THEN f.user_b ELSE f.user_a END "
                "WHERE f.status = 'pending' AND f.requester_id = %s ORDER BY f.id DESC", (uid, uid)).fetchall()]
            results, newest = [], []
            if tab == "find":
                if len(q) >= 2:
                    pat = "%" + q.replace("%", "").replace("_", "") + "%"
                    rows = c.execute("SELECT " + USER_COLS + " FROM users u WHERE u.id <> %s AND "
                                     "(lower(u.username) LIKE lower(%s) OR lower(u.full_name) LIKE lower(%s)) "
                                     "ORDER BY u.username LIMIT 20", (uid, pat, pat)).fetchall()
                    results = [dict(card(r), rel=rels.get(r[0])) for r in rows]
                else:
                    rows = c.execute("SELECT " + USER_COLS + " FROM users u WHERE u.id <> %s ORDER BY u.id DESC LIMIT 8", (uid,)).fetchall()
                    newest = [dict(card(r), rel=rels.get(r[0])) for r in rows]
        finally:
            c.close()
        recent = [f for f in friends if f["last_active"] and (fc.utcnow() - f["last_active"]).total_seconds() < 86400][:5]
        return render_template("buddies.html", tab=tab, q=q, friends=friends, incoming=incoming, outgoing=outgoing,
                               results=results, newest=newest, recent=recent)

    @app.route("/buddies/request/<int:other>", methods=["POST"])
    @h.user_required
    @h.limiter.limit("20 per minute;100 per day")
    def buddy_request(other):
        h.validate_csrf()
        uid = session["user_id"]
        c = h.get_db_connection()
        try:
            target = c.execute("SELECT username FROM users WHERE id = %s", (other,)).fetchone()
            if other == uid or not target:
                flash("You cannot send a buddy request to that account.", "error")
                return back()
            f = fc.friendship(c, uid, other)
            if f and f[1] == "accepted":
                flash("You are already buddies.", "success")
            elif f and f[2] == uid:
                flash("Your request is already waiting for a reply.", "success")
            elif f:  # they already asked us: sending a request back simply accepts theirs
                c.execute("UPDATE friendships SET status = 'accepted', responded_at = NOW() WHERE id = %s", (f[0],))
                h.notify(c, other, "friend", session["username"] + " accepted your buddy request", "", "/u/" + target[0])
                c.commit()
                flash("You are now buddies with " + target[0] + ".", "success")
            else:
                a, b = fc.pair(uid, other)
                c.execute("INSERT INTO friendships (user_a, user_b, requester_id) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (a, b, uid))
                h.notify(c, other, "friend", session["username"] + " sent you a buddy request",
                         "Open Buddies to accept or decline.", "/buddies?tab=requests")
                c.commit()
                flash("Buddy request sent to " + target[0] + ".", "success")
        finally:
            c.close()
        return back()

    @app.route("/buddies/respond/<int:fid>/<action>", methods=["POST"])
    @h.user_required
    @h.limiter.limit("60 per minute")
    def buddy_respond(fid, action):
        h.validate_csrf()
        if action not in ("accept", "reject", "cancel"):
            abort(404)
        uid = session["user_id"]
        c = h.get_db_connection()
        try:
            f = c.execute("SELECT user_a, user_b, requester_id, status FROM friendships WHERE id = %s", (fid,)).fetchone()
            if not f or uid not in (f[0], f[1]) or f[3] != "pending":
                flash("That request is no longer available.", "error")
                return back()
            if action == "cancel" and f[2] == uid:
                c.execute("DELETE FROM friendships WHERE id = %s", (fid,))
            elif action in ("accept", "reject") and f[2] != uid:
                if action == "accept":
                    c.execute("UPDATE friendships SET status = 'accepted', responded_at = NOW() WHERE id = %s", (fid,))
                    h.notify(c, f[2], "friend", session["username"] + " accepted your buddy request", "",
                             "/u/" + session["username"])
                    flash("You are now buddies.", "success")
                else:
                    c.execute("DELETE FROM friendships WHERE id = %s", (fid,))
                    flash("Request declined.", "success")
            else:
                abort(403)
            c.commit()
        finally:
            c.close()
        return back()

    @app.route("/buddies/remove/<int:other>", methods=["POST"])
    @h.user_required
    def buddy_remove(other):
        h.validate_csrf()
        a, b = fc.pair(session["user_id"], other)
        c = h.get_db_connection()
        try:
            c.execute("DELETE FROM friendships WHERE user_a = %s AND user_b = %s AND status = 'accepted'", (a, b))
            c.commit()
        finally:
            c.close()
        flash("Buddy removed.", "success")
        return back()
