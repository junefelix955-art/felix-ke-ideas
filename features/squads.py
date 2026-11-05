"""Phase 4: Squads - academic groups with privacy, roles, invitations, join requests and permissions."""
from flask import render_template, request, redirect, url_for, session, flash, abort, Response

from . import common as fc

CATEGORIES = ["Mathematics", "Programming", "Statistics", "Computer Science", "Research", "Engineering", "Physics", "Other"]
SQUAD_COLS = ("id, name, description, category, privacy, created_by, avatar_v, archived, allow_member_invites, "
              "allow_member_quizzes, allow_member_sessions, created_at")
KEYS = ("id", "name", "description", "category", "privacy", "created_by", "avatar_v", "archived",
        "allow_member_invites", "allow_member_quizzes", "allow_member_sessions", "created_at")
AVATAR_MAX = 300_000


def load_squad(c, sid):
    r = c.execute("SELECT " + SQUAD_COLS + " FROM squads WHERE id = %s", (sid,)).fetchone()
    return dict(zip(KEYS, r)) if r else None


def require_member(c, sid, uid):
    """Return (squad, role) or 404 - private squads must look non-existent to outsiders."""
    sq = load_squad(c, sid)
    role = fc.squad_role(c, sid, uid) if sq else None
    if not sq or not role:
        abort(404)
    return sq, role


def register(app, h):
    uid_ = lambda: session["user_id"]

    def back(default):
        return redirect(h.safe_next(request.form.get("next"), default))

    def read_avatar():
        """(data, mime, error) from the optional 'avatar' upload field."""
        f = request.files.get("avatar")
        if not f or not f.filename:
            return None, None, None
        data = f.stream.read(AVATAR_MAX + 1)
        if len(data) > AVATAR_MAX:
            return None, None, "That picture is too large (max 300 KB)."
        r = fc.sniff_file(data, f.filename, ("image",))
        if not r:
            return None, None, "Use a PNG, JPEG or WebP picture."
        return data, r["mime"], None

    def staff_ids(c, sid, exclude=None):
        return [r[0] for r in c.execute("SELECT user_id FROM squad_members WHERE squad_id = %s AND role IN ('admin','moderator')", (sid,)).fetchall()
                if r[0] != exclude]

    def clean_fields(f):
        name, desc = h.clean_text(f.get("name"), 60), h.clean_text(f.get("description"), 500)
        cat = f.get("category") if f.get("category") in CATEGORIES else "Other"
        privacy = "private" if f.get("privacy") == "private" else "public"
        err = None if len(name) >= 3 else "Give your squad a name (at least 3 characters)."
        return name, desc, cat, privacy, err

    # ------------------------------------------------------------ list
    @app.route("/squads")
    @h.user_required
    def squads():
        uid = uid_()
        q = request.args.get("q", "").strip()[:40]
        cat = request.args.get("category", "")
        c = h.get_db_connection()
        try:
            online_cut = fc.utcnow() - fc.timedelta(seconds=fc.ONLINE_SECONDS)
            rows = c.execute(
                "SELECT s.id, s.name, s.description, s.category, s.privacy, s.avatar_v, s.archived, sm.role, "
                "(SELECT COUNT(*) FROM squad_members x WHERE x.squad_id = s.id), "
                "(SELECT COUNT(*) FROM squad_members x JOIN users u ON u.id = x.user_id WHERE x.squad_id = s.id AND u.last_active > %s), "
                "t.id, "
                "(SELECT COUNT(*) FROM thread_messages m WHERE m.thread_id = t.id AND m.id > tm.last_read_id AND m.user_id <> %s "
                "AND NOT m.deleted AND m.kind <> 'system'), "
                "(SELECT MAX(m.created_at) FROM thread_messages m WHERE m.thread_id = t.id) AS last_act "
                "FROM squad_members sm JOIN squads s ON s.id = sm.squad_id JOIN threads t ON t.squad_id = s.id "
                "JOIN thread_members tm ON tm.thread_id = t.id AND tm.user_id = sm.user_id "
                "WHERE sm.user_id = %s ORDER BY s.archived, last_act DESC NULLS LAST, s.id DESC",
                (online_cut, uid, uid)).fetchall()
            mine = [dict(zip(("id", "name", "description", "category", "privacy", "avatar_v", "archived", "role", "members",
                              "online", "thread_id", "unread", "last_activity"), r)) for r in rows]
            ids = [m["id"] for m in mine]
            faces = {}
            if ids:
                for sid, u_id, un, fn, av, la in c.execute(
                        "SELECT sm.squad_id, u.id, u.username, u.full_name, u.avatar_v, u.last_active FROM squad_members sm "
                        "JOIN users u ON u.id = sm.user_id WHERE sm.squad_id = ANY(%s) "
                        "ORDER BY CASE sm.role WHEN 'admin' THEN 0 WHEN 'moderator' THEN 1 ELSE 2 END, sm.joined_at", (ids,)).fetchall():
                    if len(faces.setdefault(sid, [])) < 4:
                        faces[sid].append({"id": u_id, "username": un, "full_name": fn, "avatar_v": av})
            for m in mine:
                m["faces"] = faces.get(m["id"], [])
            invites = [dict(zip(("req", "id", "name", "avatar_v", "by"), r)) for r in c.execute(
                "SELECT r.id, s.id, s.name, s.avatar_v, (SELECT username FROM users WHERE id = r.invited_by) FROM squad_requests r "
                "JOIN squads s ON s.id = r.squad_id WHERE r.user_id = %s AND r.kind = 'invite' AND NOT s.archived ORDER BY r.id DESC", (uid,)).fetchall()]
            sql = ("SELECT s.id, s.name, s.description, s.category, s.avatar_v, (SELECT COUNT(*) FROM squad_members x WHERE x.squad_id = s.id), "
                   "(SELECT 1 FROM squad_requests r WHERE r.squad_id = s.id AND r.user_id = %s AND r.kind = 'request') FROM squads s "
                   "WHERE s.privacy = 'public' AND NOT s.archived AND NOT EXISTS "
                   "(SELECT 1 FROM squad_members x WHERE x.squad_id = s.id AND x.user_id = %s)")
            params = [uid, uid]
            if q:
                sql += " AND (lower(s.name) LIKE lower(%s) OR lower(s.description) LIKE lower(%s))"
                params += ["%" + q.replace("%", "") + "%"] * 2
            if cat in CATEGORIES:
                sql += " AND s.category = %s"
                params.append(cat)
            discover = [dict(zip(("id", "name", "description", "category", "avatar_v", "members", "requested"), r))
                        for r in c.execute(sql + " ORDER BY s.id DESC LIMIT 24", params).fetchall()]
        finally:
            c.close()
        return render_template("squads.html", mine=mine, invites=invites, discover=discover, q=q, cat=cat, categories=CATEGORIES)

    # ------------------------------------------------------------ create
    @app.route("/squads/new", methods=["GET", "POST"])
    @h.user_required
    @h.limiter.limit("5 per minute;20 per day", methods=["POST"])
    def squad_new():
        uid = uid_()
        c = h.get_db_connection()
        try:
            buddies = c.execute(
                "SELECT u.id, u.username, u.full_name, u.avatar_v FROM friendships f JOIN users u ON u.id = "
                "CASE WHEN f.user_a = %s THEN f.user_b ELSE f.user_a END WHERE f.status = 'accepted' AND (f.user_a = %s OR f.user_b = %s) "
                "ORDER BY u.username", (uid, uid, uid)).fetchall()
            buddies = [dict(zip(("id", "username", "full_name", "avatar_v"), r)) for r in buddies]
            if request.method == "POST":
                h.validate_csrf()
                name, desc, cat, privacy, err = clean_fields(request.form)
                data, mime, aerr = read_avatar()
                err = err or aerr
                if err:
                    flash(err, "error")
                    return render_template("squad_form.html", mode="new", squad=request.form, categories=CATEGORIES, buddies=buddies), 400
                sid = c.execute("INSERT INTO squads (name, description, category, privacy, created_by, avatar, avatar_mime, avatar_v) "
                                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                                (name, desc, cat, privacy, uid, data, mime, 1 if data else 0)).fetchone()[0]
                tid = c.execute("INSERT INTO threads (kind, squad_id) VALUES ('squad', %s) RETURNING id", (sid,)).fetchone()[0]
                fc.add_squad_member(c, sid, uid, "admin", announce=False)
                fc.post_system_message(c, tid, uid, "Squad created by " + session["username"])
                invited = 0
                ok_ids = {b["id"] for b in buddies}
                for raw in request.form.getlist("invite"):
                    if raw.isdigit() and int(raw) in ok_ids:
                        c.execute("INSERT INTO squad_requests (squad_id, user_id, kind, invited_by) VALUES (%s, %s, 'invite', %s) ON CONFLICT DO NOTHING",
                                  (sid, int(raw), uid))
                        h.notify(c, int(raw), "squad", session["username"] + " invited you to " + name, "Open the invitation to join.", "/squads/%d" % sid)
                        invited += 1
                c.commit()
                flash("Squad created." + (" %d invitation%s sent." % (invited, "" if invited == 1 else "s") if invited else ""), "success")
                return redirect(url_for("squad_detail", sid=sid))
        finally:
            c.close()
        return render_template("squad_form.html", mode="new", squad={}, categories=CATEGORIES, buddies=buddies)

    # ------------------------------------------------------------ detail
    @app.route("/squads/<int:sid>")
    @h.user_required
    def squad_detail(sid):
        uid = uid_()
        c = h.get_db_connection()
        try:
            sq = load_squad(c, sid)
            if not sq:
                abort(404)
            role = fc.squad_role(c, sid, uid)
            req = c.execute("SELECT id, kind, invited_by FROM squad_requests WHERE squad_id = %s AND user_id = %s", (sid, uid)).fetchone()
            invited = bool(req and req[1] == "invite")
            if not role and sq["privacy"] == "private" and not invited:
                abort(404)  # private squads are invisible to outsiders
            count = c.execute("SELECT COUNT(*) FROM squad_members WHERE squad_id = %s", (sid,)).fetchone()[0]
            ctx = {"squad": sq, "role": role, "count": count, "request": req, "invited": invited}
            if role:
                ctx["members"] = [dict(zip(("id", "username", "full_name", "avatar_v", "last_active", "role"), r),
                                        online=fc.is_online(r[4])) for r in c.execute(
                    "SELECT u.id, u.username, u.full_name, u.avatar_v, u.last_active, sm.role FROM squad_members sm "
                    "JOIN users u ON u.id = sm.user_id WHERE sm.squad_id = %s "
                    "ORDER BY CASE sm.role WHEN 'admin' THEN 0 WHEN 'moderator' THEN 1 ELSE 2 END, u.username", (sid,)).fetchall()]
                ctx["thread_id"] = c.execute("SELECT id FROM threads WHERE squad_id = %s", (sid,)).fetchone()[0]
                if fc.is_staff(role):
                    ctx["pending"] = [dict(zip(("req", "id", "username", "full_name", "avatar_v"), r)) for r in c.execute(
                        "SELECT r.id, u.id, u.username, u.full_name, u.avatar_v FROM squad_requests r JOIN users u ON u.id = r.user_id "
                        "WHERE r.squad_id = %s AND r.kind = 'request' ORDER BY r.id", (sid,)).fetchall()]
                    ctx["invites_out"] = c.execute("SELECT COUNT(*) FROM squad_requests WHERE squad_id = %s AND kind = 'invite'", (sid,)).fetchone()[0]
                ctx["questions"] = [dict(zip(("id", "body", "category", "difficulty", "user", "answers"), r)) for r in c.execute(
                    "SELECT q.id, q.body, q.category, q.difficulty, u.username, (SELECT COUNT(*) FROM question_replies r WHERE r.question_id = q.id AND r.is_answer) "
                    "FROM math_questions q JOIN users u ON u.id = q.user_id WHERE q.squad_id = %s ORDER BY q.id DESC LIMIT 4", (sid,)).fetchall()]
                ctx["quiz_count"] = c.execute("SELECT COUNT(*) FROM quizzes WHERE squad_id = %s AND published", (sid,)).fetchone()[0]
                ctx["session_count"] = c.execute("SELECT COUNT(*) FROM study_sessions WHERE squad_id = %s AND NOT cancelled AND starts_at > %s",
                                                 (sid, fc.utcnow())).fetchone()[0]
                ctx["can_invite"] = fc.is_staff(role) or sq["allow_member_invites"]
            else:
                ctx["admin_name"] = (c.execute("SELECT username FROM users WHERE id = %s", (sq["created_by"],)).fetchone() or [""])[0]
        finally:
            c.close()
        return render_template("squad_detail.html", categories=CATEGORIES, **ctx)

    @app.route("/squads/<int:sid>/avatar")
    def squad_avatar(sid):
        if not session.get("user_id"):
            abort(404)
        c = h.get_db_connection()
        try:
            r = c.execute("SELECT avatar, avatar_mime, privacy FROM squads WHERE id = %s", (sid,)).fetchone()
            ok = r and r[0] and (r[2] == "public" or fc.squad_role(c, sid, session["user_id"]) or
                                 c.execute("SELECT 1 FROM squad_requests WHERE squad_id = %s AND user_id = %s", (sid, session["user_id"])).fetchone())
        finally:
            c.close()
        if not ok:
            abort(404)
        resp = Response(bytes(r[0]), mimetype=r[1])
        resp.headers["Cache-Control"] = "private, max-age=86400"
        resp.headers["Content-Security-Policy"] = "default-src 'none'"
        return resp

    # ------------------------------------------------------------ membership actions
    @app.route("/squads/<int:sid>/join", methods=["POST"])
    @h.user_required
    @h.limiter.limit("20 per minute;60 per day")
    def squad_join(sid):
        h.validate_csrf()
        uid = uid_()
        c = h.get_db_connection()
        try:
            sq = load_squad(c, sid)
            if not sq or sq["archived"]:
                abort(404)
            if fc.squad_role(c, sid, uid):
                return redirect(url_for("squad_detail", sid=sid))
            req = c.execute("SELECT id, kind FROM squad_requests WHERE squad_id = %s AND user_id = %s", (sid, uid)).fetchone()
            if req and req[1] == "invite":  # accepting the invitation by pressing join
                c.execute("DELETE FROM squad_requests WHERE id = %s", (req[0],))
                fc.add_squad_member(c, sid, uid)
                c.commit()
                flash("Welcome to " + sq["name"] + "!", "success")
            elif sq["privacy"] == "public":
                if not req:
                    c.execute("INSERT INTO squad_requests (squad_id, user_id, kind) VALUES (%s, %s, 'request') ON CONFLICT DO NOTHING", (sid, uid))
                    for s_id in staff_ids(c, sid):
                        h.notify(c, s_id, "squad", session["username"] + " wants to join " + sq["name"], "Approve or decline the request.", "/squads/%d#requests" % sid)
                    c.commit()
                flash("Request sent. A squad admin will review it.", "success")
            else:
                abort(404)
        finally:
            c.close()
        return redirect(url_for("squads") if request.form.get("from") == "list" else url_for("squad_detail", sid=sid))

    @app.route("/squads/<int:sid>/cancel", methods=["POST"])
    @h.user_required
    def squad_cancel(sid):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            c.execute("DELETE FROM squad_requests WHERE squad_id = %s AND user_id = %s AND kind = 'request'", (sid, uid_()))
            c.commit()
        finally:
            c.close()
        flash("Join request cancelled.", "success")
        return back(url_for("squads"))

    @app.route("/squads/<int:sid>/invitation/<action>", methods=["POST"])
    @h.user_required
    def squad_invitation(sid, action):
        h.validate_csrf()
        if action not in ("accept", "decline"):
            abort(404)
        uid = uid_()
        c = h.get_db_connection()
        try:
            sq = load_squad(c, sid)
            req = c.execute("SELECT id FROM squad_requests WHERE squad_id = %s AND user_id = %s AND kind = 'invite'", (sid, uid)).fetchone()
            if not sq or not req:
                flash("That invitation is no longer available.", "error")
                return back(url_for("squads"))
            c.execute("DELETE FROM squad_requests WHERE id = %s", (req[0],))
            if action == "accept" and not sq["archived"]:
                fc.add_squad_member(c, sid, uid)
                flash("Welcome to " + sq["name"] + "!", "success")
            else:
                flash("Invitation declined.", "success")
            c.commit()
        finally:
            c.close()
        return redirect(url_for("squad_detail", sid=sid) if action == "accept" else url_for("squads"))

    @app.route("/squads/<int:sid>/request/<int:rid>/<action>", methods=["POST"])
    @h.user_required
    def squad_review(sid, rid, action):
        h.validate_csrf()
        if action not in ("approve", "decline"):
            abort(404)
        c = h.get_db_connection()
        try:
            sq, role = require_member(c, sid, uid_())
            if not fc.is_staff(role):
                abort(403)
            req = c.execute("SELECT user_id FROM squad_requests WHERE id = %s AND squad_id = %s AND kind = 'request'", (rid, sid)).fetchone()
            if req:
                c.execute("DELETE FROM squad_requests WHERE id = %s", (rid,))
                if action == "approve":
                    fc.add_squad_member(c, sid, req[0])
                    h.notify(c, req[0], "squad", "You were accepted into " + sq["name"], "Open the squad to say hello.", "/squads/%d" % sid)
                c.commit()
                flash("Request approved." if action == "approve" else "Request declined.", "success")
        finally:
            c.close()
        return redirect(url_for("squad_detail", sid=sid) + "#requests")

    @app.route("/squads/<int:sid>/invite", methods=["POST"])
    @h.user_required
    @h.limiter.limit("20 per minute;100 per day")
    def squad_invite(sid):
        h.validate_csrf()
        uid = uid_()
        uname = request.form.get("username", "").strip().lstrip("@")
        c = h.get_db_connection()
        try:
            sq, role = require_member(c, sid, uid)
            if not (fc.is_staff(role) or sq["allow_member_invites"]) or sq["archived"]:
                abort(403)
            target = c.execute("SELECT id, username FROM users WHERE lower(username) = lower(%s)", (uname,)).fetchone() if h.USERNAME_RE.match(uname) else None
            if not target:
                flash("No member called '%s'." % uname[:30], "error")
            elif fc.squad_role(c, sid, target[0]):
                flash(target[1] + " is already in this squad.", "error")
            else:
                existing = c.execute("SELECT id, kind FROM squad_requests WHERE squad_id = %s AND user_id = %s", (sid, target[0])).fetchone()
                if existing and existing[1] == "request":  # they had asked to join: inviting = approving
                    c.execute("DELETE FROM squad_requests WHERE id = %s", (existing[0],))
                    fc.add_squad_member(c, sid, target[0])
                    flash(target[1] + " has joined the squad.", "success")
                elif existing:
                    flash(target[1] + " already has an invitation.", "success")
                else:
                    c.execute("INSERT INTO squad_requests (squad_id, user_id, kind, invited_by) VALUES (%s, %s, 'invite', %s)", (sid, target[0], uid))
                    h.notify(c, target[0], "squad", session["username"] + " invited you to " + sq["name"], "Open the invitation to join.", "/squads/%d" % sid)
                    flash("Invitation sent to " + target[1] + ".", "success")
                c.commit()
        finally:
            c.close()
        return redirect(url_for("squad_detail", sid=sid) + "#members")

    @app.route("/squads/<int:sid>/leave", methods=["POST"])
    @h.user_required
    def squad_leave(sid):
        h.validate_csrf()
        uid = uid_()
        c = h.get_db_connection()
        try:
            sq, role = require_member(c, sid, uid)
            others = c.execute("SELECT COUNT(*) FROM squad_members WHERE squad_id = %s AND user_id <> %s", (sid, uid)).fetchone()[0]
            admins = c.execute("SELECT COUNT(*) FROM squad_members WHERE squad_id = %s AND role = 'admin' AND user_id <> %s", (sid, uid)).fetchone()[0]
            if role == "admin" and admins == 0:
                flash("You are the only admin. Make another member an admin first" + (", or delete the squad." if not others else "."), "error")
                return redirect(url_for("squad_detail", sid=sid))
            fc.remove_squad_member(c, sid, uid, session["username"] + " left the squad")
            c.commit()
        finally:
            c.close()
        flash("You left " + sq["name"] + ".", "success")
        return redirect(url_for("squads"))

    @app.route("/squads/<int:sid>/members/<int:target>/role", methods=["POST"])
    @h.user_required
    def squad_role_change(sid, target):
        h.validate_csrf()
        new = request.form.get("role")
        c = h.get_db_connection()
        try:
            sq, role = require_member(c, sid, uid_())
            if role != "admin" or new not in ("admin", "moderator", "member") or target == uid_():
                abort(403)
            if fc.squad_role(c, sid, target):
                c.execute("UPDATE squad_members SET role = %s WHERE squad_id = %s AND user_id = %s", (new, sid, target))
                if new != "member":
                    h.notify(c, target, "squad", "You are now %s of %s" % ("an admin" if new == "admin" else "a moderator", sq["name"]), "", "/squads/%d" % sid)
                c.commit()
                flash("Role updated.", "success")
        finally:
            c.close()
        return redirect(url_for("squad_detail", sid=sid) + "#members")

    @app.route("/squads/<int:sid>/members/<int:target>/remove", methods=["POST"])
    @h.user_required
    def squad_remove_member(sid, target):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            sq, role = require_member(c, sid, uid_())
            trole = fc.squad_role(c, sid, target)
            if not fc.is_staff(role) or not trole or target == uid_() or (role == "moderator" and trole != "member"):
                abort(403)
            name = c.execute("SELECT username FROM users WHERE id = %s", (target,)).fetchone()[0]
            fc.remove_squad_member(c, sid, target, name + " was removed from the squad")
            h.notify(c, target, "squad", "You were removed from " + sq["name"], "", "/squads")
            c.commit()
            flash(name + " was removed.", "success")
        finally:
            c.close()
        return redirect(url_for("squad_detail", sid=sid) + "#members")

    # ------------------------------------------------------------ admin: edit, archive, delete
    @app.route("/squads/<int:sid>/edit", methods=["GET", "POST"])
    @h.user_required
    @h.limiter.limit("20 per minute", methods=["POST"])
    def squad_edit(sid):
        c = h.get_db_connection()
        try:
            sq, role = require_member(c, sid, uid_())
            if role != "admin":
                abort(403)
            if request.method == "POST":
                h.validate_csrf()
                name, desc, cat, privacy, err = clean_fields(request.form)
                data, mime, aerr = read_avatar()
                err = err or aerr
                if err:
                    flash(err, "error")
                    return render_template("squad_form.html", mode="edit", squad=dict(sq, **request.form.to_dict()), categories=CATEGORIES, buddies=[]), 400
                c.execute("UPDATE squads SET name = %s, description = %s, category = %s, privacy = %s, allow_member_invites = %s, "
                          "allow_member_quizzes = %s, allow_member_sessions = %s WHERE id = %s",
                          (name, desc, cat, privacy, "allow_member_invites" in request.form, "allow_member_quizzes" in request.form,
                           "allow_member_sessions" in request.form, sid))
                if data:
                    c.execute("UPDATE squads SET avatar = %s, avatar_mime = %s, avatar_v = avatar_v + 1 WHERE id = %s", (data, mime, sid))
                elif "remove_avatar" in request.form:
                    c.execute("UPDATE squads SET avatar = NULL, avatar_mime = NULL, avatar_v = 0 WHERE id = %s", (sid,))
                c.commit()
                flash("Squad updated.", "success")
                return redirect(url_for("squad_detail", sid=sid))
        finally:
            c.close()
        return render_template("squad_form.html", mode="edit", squad=sq, categories=CATEGORIES, buddies=[])

    @app.route("/squads/<int:sid>/archive", methods=["POST"])
    @h.user_required
    def squad_archive(sid):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            sq, role = require_member(c, sid, uid_())
            if role != "admin":
                abort(403)
            c.execute("UPDATE squads SET archived = %s WHERE id = %s", (not sq["archived"], sid))
            c.commit()
            flash("Squad archived. It is now read-only." if not sq["archived"] else "Squad restored.", "success")
        finally:
            c.close()
        return redirect(url_for("squad_detail", sid=sid))

    @app.route("/squads/<int:sid>/delete", methods=["POST"])
    @h.user_required
    def squad_delete(sid):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            sq, role = require_member(c, sid, uid_())
            if role != "admin":
                abort(403)
            c.execute("DELETE FROM squads WHERE id = %s", (sid,))
            c.commit()
        finally:
            c.close()
        flash("Squad deleted.", "success")
        return redirect(url_for("squads"))
