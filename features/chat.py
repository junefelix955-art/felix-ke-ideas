"""Phase 5: squad group chat and private messages (one unified thread system).
Real time is done by polling every few seconds - no extra servers or packages needed."""
import re

from flask import render_template, request, redirect, url_for, session, flash, abort, jsonify

from . import common as fc

REACTIONS = ("👍", "❤️", "😂", "🎉", "🤔", "👏")
CHAT_FILES = {"pdf", "image", "docx", "pptx", "doc", "text"}
MAX_FILE = 5 * 1024 * 1024
MAX_BODY = 1500
MENTION = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{3,30})")


def register(app, h):
    def uid_():
        return session["user_id"]

    def open_thread(c, tid, uid):
        """(kind, squad_id, archived) when the user belongs to the thread, else 404 (never reveals existence)."""
        r = c.execute("SELECT t.kind, t.squad_id, COALESCE(s.archived, FALSE) FROM threads t "
                      "JOIN thread_members tm ON tm.thread_id = t.id AND tm.user_id = %s "
                      "LEFT JOIN squads s ON s.id = t.squad_id WHERE t.id = %s", (uid, tid)).fetchone()
        if not r:
            abort(404)
        return {"kind": r[0], "squad_id": r[1], "archived": bool(r[2])}

    def dm_other(c, tid, uid):
        r = c.execute("SELECT u.id, u.username, u.full_name, u.avatar_v, u.last_active FROM thread_members tm "
                      "JOIN users u ON u.id = tm.user_id WHERE tm.thread_id = %s AND tm.user_id <> %s", (tid, uid)).fetchone()
        return dict(zip(("id", "username", "full_name", "avatar_v", "last_active"), r), online=fc.is_online(r[4])) if r else None

    def mark_delivered(c, uid):
        c.execute("UPDATE thread_members SET last_delivered_id = (SELECT COALESCE(MAX(id), 0) FROM thread_messages m "
                  "WHERE m.thread_id = thread_members.thread_id) WHERE user_id = %s", (uid,))

    # ---------------------------------------------------------------- message loading
    def load_messages(c, tid, uid, after=0, limit=60):
        """Messages oldest->newest with reply preview, file, question card, reactions and read status."""
        if after:
            rows = c.execute(
                "SELECT m.id, m.user_id, u.username, u.full_name, u.avatar_v, m.body, m.kind, m.ref_id, m.reply_to, m.deleted, m.created_at "
                "FROM thread_messages m JOIN users u ON u.id = m.user_id WHERE m.thread_id = %s AND m.id > %s ORDER BY m.id LIMIT 100",
                (tid, after)).fetchall()
        else:
            rows = list(reversed(c.execute(
                "SELECT m.id, m.user_id, u.username, u.full_name, u.avatar_v, m.body, m.kind, m.ref_id, m.reply_to, m.deleted, m.created_at "
                "FROM thread_messages m JOIN users u ON u.id = m.user_id WHERE m.thread_id = %s ORDER BY m.id DESC LIMIT %s",
                (tid, limit)).fetchall()))
        msgs = []
        for r in rows:
            m = dict(zip(("id", "user_id", "username", "full_name", "avatar_v", "body", "kind", "ref_id", "reply_to", "deleted", "created_at"), r))
            if m["deleted"]:
                m["body"], m["kind"] = "", "deleted"
            msgs.append(m)
        for m in msgs:
            m["reply"] = None
            if m["reply_to"]:
                p = c.execute("SELECT m.id, u.username, m.body, m.kind, m.deleted FROM thread_messages m JOIN users u ON u.id = m.user_id "
                              "WHERE m.id = %s AND m.thread_id = %s", (m["reply_to"], tid)).fetchone()
                if p:
                    m["reply"] = {"id": p[0], "username": p[1], "text": "Message deleted" if p[4] else (p[2][:80] or "[" + p[3] + "]")}
            if m["kind"] == "file" and m["ref_id"]:
                f = c.execute("SELECT id, filename, kind, size FROM files WHERE id = %s AND thread_id = %s", (m["ref_id"], tid)).fetchone()
                m["file"] = {"id": f[0], "filename": f[1], "kind": f[2], "size": fc.human_size(f[3]),
                             "image": f[2] in ("png", "jpeg", "webp")} if f else None
            if m["kind"] == "question" and m["ref_id"]:
                q = c.execute("SELECT q.id, q.body, q.category, q.difficulty, u.username, "
                              "(SELECT COUNT(*) FROM question_replies r WHERE r.question_id = q.id AND r.is_answer) "
                              "FROM math_questions q JOIN users u ON u.id = q.user_id WHERE q.id = %s", (m["ref_id"],)).fetchone()
                m["question"] = dict(zip(("id", "body", "category", "difficulty", "user", "answers"), q)) if q else None
        return msgs

    def decorate(c, tid, uid, msgs, kind):
        """Add reactions and a delivery status (sent / delivered / seen) to each message."""
        if not msgs:
            return msgs
        ids = [m["id"] for m in msgs]
        grouped = {}
        for mid, emoji, who in c.execute("SELECT message_id, emoji, user_id FROM message_reactions WHERE message_id = ANY(%s)", (ids,)).fetchall():
            g = grouped.setdefault(mid, {}).setdefault(emoji, [0, False])
            g[0] += 1
            g[1] = g[1] or who == uid
        others = c.execute("SELECT last_delivered_id, last_read_id FROM thread_members WHERE thread_id = %s AND user_id <> %s", (tid, uid)).fetchall()
        for m in msgs:
            m["reactions"] = [{"emoji": e, "count": v[0], "mine": v[1]} for e, v in grouped.get(m["id"], {}).items()]
            m["mine"] = m["user_id"] == uid
            if m["mine"]:
                seen = sum(1 for o in others if o[1] >= m["id"])
                if kind == "dm":
                    m["status"] = "seen" if seen else ("delivered" if any(o[0] >= m["id"] for o in others) else "sent")
                else:
                    m["status"] = "seen" if seen else "sent"
                m["seen_by"] = seen
        return msgs

    def mine_flag(m):
        return m

    # ---------------------------------------------------------------- inbox
    @app.route("/messages")
    @h.user_required
    def messages():
        uid = uid_()
        c = h.get_db_connection()
        try:
            mark_delivered(c, uid)
            c.commit()
            rows = c.execute(
                "SELECT t.id, t.kind, t.squad_id, s.name, s.avatar_v, tm.last_read_id, "
                "(SELECT MAX(id) FROM thread_messages x WHERE x.thread_id = t.id) AS last_id "
                "FROM thread_members tm JOIN threads t ON t.id = tm.thread_id LEFT JOIN squads s ON s.id = t.squad_id "
                "WHERE tm.user_id = %s", (uid,)).fetchall()
            items = []
            for tid, kind, sid, sname, savatar, last_read, last_id in rows:
                last = None
                if last_id:
                    lr = c.execute("SELECT m.body, m.kind, m.deleted, m.created_at, u.username FROM thread_messages m JOIN users u ON u.id = m.user_id WHERE m.id = %s", (last_id,)).fetchone()
                    text = "Message deleted" if lr[2] else (lr[0][:70] if lr[0] else {"file": "Sent a file", "question": "Shared a question"}.get(lr[1], ""))
                    last = {"text": text, "time": lr[3], "who": lr[4]}
                unread = c.execute("SELECT COUNT(*) FROM thread_messages WHERE thread_id = %s AND id > %s AND user_id <> %s AND NOT deleted AND kind <> 'system'",
                                   (tid, last_read, uid)).fetchone()[0]
                it = {"id": tid, "kind": kind, "last": last, "unread": unread}
                if kind == "squad":
                    it.update(name=sname, squad={"id": sid, "name": sname, "avatar_v": savatar})
                else:
                    o = dm_other(c, tid, uid)
                    if not o:
                        continue
                    it.update(name=o["full_name"] or o["username"], other=o)
                items.append(it)
            items.sort(key=lambda i: i["last"]["time"] if i["last"] else fc.utcnow().replace(year=2000), reverse=True)
            buddies = [dict(zip(("id", "username", "full_name", "avatar_v", "last_active"), r), online=fc.is_online(r[4])) for r in c.execute(
                "SELECT u.id, u.username, u.full_name, u.avatar_v, u.last_active FROM friendships f "
                "JOIN users u ON u.id = CASE WHEN f.user_a = %s THEN f.user_b ELSE f.user_a END "
                "WHERE f.status = 'accepted' AND (f.user_a = %s OR f.user_b = %s) ORDER BY u.username", (uid, uid, uid)).fetchall()]
        finally:
            c.close()
        return render_template("messages.html", items=items, buddies=buddies)

    @app.route("/messages/start/<int:other>", methods=["POST"])
    @h.user_required
    @h.limiter.limit("30 per minute")
    def dm_start(other):
        h.validate_csrf()
        uid = uid_()
        if other == uid:
            abort(404)
        c = h.get_db_connection()
        try:
            if not c.execute("SELECT 1 FROM users WHERE id = %s", (other,)).fetchone():
                abort(404)
            if not fc.are_friends(c, uid, other):
                flash("You can message people once you are buddies.", "error")
                return redirect(url_for("buddies", tab="find"))
            a, b = fc.pair(uid, other)
            r = c.execute("SELECT thread_id FROM dm_pairs WHERE user_a = %s AND user_b = %s", (a, b)).fetchone()
            if r:
                tid = r[0]
            else:
                c.execute("INSERT INTO threads (kind) VALUES ('dm')")
                tid = c.execute("SELECT MAX(id) FROM threads").fetchone()[0]
                c.execute("INSERT INTO dm_pairs (user_a, user_b, thread_id) VALUES (%s, %s, %s)", (a, b, tid))
                for u in (uid, other):
                    c.execute("INSERT INTO thread_members (thread_id, user_id) VALUES (%s, %s)", (tid, u))
                c.commit()
        finally:
            c.close()
        return redirect(url_for("thread", tid=tid))

    # ---------------------------------------------------------------- one conversation
    @app.route("/messages/<int:tid>")
    @h.user_required
    def thread(tid):
        uid = uid_()
        c = h.get_db_connection()
        try:
            t = open_thread(c, tid, uid)
            msgs = decorate(c, tid, uid, load_messages(c, tid, uid), t["kind"])
            last = msgs[-1]["id"] if msgs else 0
            c.execute("UPDATE thread_members SET last_read_id = (SELECT COALESCE(MAX(id), 0) FROM thread_messages WHERE thread_id = %s), "
                      "last_delivered_id = (SELECT COALESCE(MAX(id), 0) FROM thread_messages WHERE thread_id = %s) "
                      "WHERE thread_id = %s AND user_id = %s", (tid, tid, tid, uid))
            c.commit()
            ctx = {"tid": tid, "t": t, "kind": t["kind"], "messages": msgs, "last_id": last, "reactions": REACTIONS, "can_send": True}
            if t["kind"] == "squad":
                sq = c.execute("SELECT id, name, avatar_v, category FROM squads WHERE id = %s", (t["squad_id"],)).fetchone()
                ctx["squad"] = dict(zip(("id", "name", "avatar_v", "category"), sq))
                ctx["members"] = c.execute("SELECT COUNT(*) FROM squad_members WHERE squad_id = %s", (t["squad_id"],)).fetchone()[0]
                ctx["online"] = c.execute("SELECT COUNT(*) FROM squad_members sm JOIN users u ON u.id = sm.user_id WHERE sm.squad_id = %s AND u.last_active > %s",
                                          (t["squad_id"], fc.utcnow() - fc.timedelta(seconds=fc.ONLINE_SECONDS))).fetchone()[0]
                ctx["role"] = fc.squad_role(c, t["squad_id"], uid)
                ctx["can_send"] = not t["archived"]
            else:
                o = dm_other(c, tid, uid)
                ctx["other"] = o
                ctx["can_send"] = bool(o and fc.are_friends(c, uid, o["id"]))
        finally:
            c.close()
        return render_template("thread.html", **ctx)

    def message_json(m):
        return {"id": m["id"], "mine": m["mine"], "status": m.get("status"), "deleted": m["deleted"],
                "reactions": m["reactions"], "seen_by": m.get("seen_by", 0)}

    @app.route("/messages/<int:tid>/poll")
    @h.user_required
    @h.limiter.limit("60 per minute")
    def thread_poll(tid):
        uid = uid_()
        try:
            after = max(0, int(request.args.get("after", "0")))
        except ValueError:
            after = 0
        c = h.get_db_connection()
        try:
            t = open_thread(c, tid, uid)
            new = decorate(c, tid, uid, load_messages(c, tid, uid, after=after), t["kind"])
            if new:
                c.execute("UPDATE thread_members SET last_read_id = %s, last_delivered_id = %s WHERE thread_id = %s AND user_id = %s",
                          (new[-1]["id"], new[-1]["id"], tid, uid))
                c.commit()
            html = "".join(render_template("_message.html", m=m, tid=tid, kind=t["kind"], reactions=REACTIONS) for m in new)
            recent = decorate(c, tid, uid, load_messages(c, tid, uid, limit=40), t["kind"])
            online = None
            if t["kind"] == "dm":
                o = dm_other(c, tid, uid)
                online = bool(o and o["online"])
        finally:
            c.close()
        return jsonify(html=html, last=new[-1]["id"] if new else after, updates=[message_json(m) for m in recent if m["id"] <= after],
                       online=online)

    @app.route("/messages/<int:tid>/send", methods=["POST"])
    @h.user_required
    @h.limiter.limit("40 per minute;1000 per day")
    def thread_send(tid):
        h.validate_csrf()
        uid = uid_()
        body = request.form.get("body", "").replace("\r", "").strip()[:MAX_BODY]
        upload = request.files.get("file")
        ajax = bool(request.headers.get("X-CSRF-Token"))

        def fail(msg, code=400):
            if ajax:
                return jsonify(error=msg), code
            flash(msg, "error")
            return redirect(url_for("thread", tid=tid))

        c = h.get_db_connection()
        try:
            t = open_thread(c, tid, uid)
            if t["archived"]:
                return fail("This squad is archived and read-only.", 403)
            other = None
            if t["kind"] == "dm":
                other = dm_other(c, tid, uid)
                if not other or not fc.are_friends(c, uid, other["id"]):
                    return fail("You can only message buddies.", 403)
            reply_to = None
            try:
                rt = int(request.form.get("reply_to") or 0)
            except ValueError:
                rt = 0
            if rt and c.execute("SELECT 1 FROM thread_messages WHERE id = %s AND thread_id = %s", (rt, tid)).fetchone():
                reply_to = rt
            kind, ref = "text", None
            if upload and upload.filename:
                data = upload.read(MAX_FILE + 1)
                if len(data) > MAX_FILE:
                    return fail("Files can be up to 5 MB.", 413)
                info = fc.sniff_file(data, upload.filename, CHAT_FILES)
                if not info:
                    return fail("That file type is not allowed. Use PDF, Word, PowerPoint, an image or a text/code file.", 400)
                name = fc.clean_filename(upload.filename, info["kind"] if info["kind"] != "text" else None)
                c.execute("INSERT INTO files (owner_id, thread_id, filename, mime, kind, size, data) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                          (uid, tid, name, info["mime"], info["kind"], len(data), data))
                ref = c.execute("SELECT MAX(id) FROM files WHERE owner_id = %s", (uid,)).fetchone()[0]
                kind = "file"
            if not body and kind == "text":
                return fail("Type a message first.")
            c.execute("INSERT INTO thread_messages (thread_id, user_id, body, kind, ref_id, reply_to) VALUES (%s, %s, %s, %s, %s, %s)",
                      (tid, uid, body, kind, ref, reply_to))
            mid = c.execute("SELECT MAX(id) FROM thread_messages WHERE thread_id = %s", (tid,)).fetchone()[0]
            c.execute("UPDATE thread_members SET last_read_id = %s, last_delivered_id = %s WHERE thread_id = %s AND user_id = %s", (mid, mid, tid, uid))
            me = c.execute("SELECT username, full_name FROM users WHERE id = %s", (uid,)).fetchone()
            who = me[1] or me[0]
            link = url_for("thread", tid=tid)
            if other:  # one notification per conversation until it is read
                if not c.execute("SELECT 1 FROM notifications WHERE user_id = %s AND kind = 'message' AND link = %s AND NOT is_read", (other["id"], link)).fetchone():
                    h.notify(c, other["id"], "message", who + " sent you a message", body[:100] or "Sent a file", link)
            else:
                sq = c.execute("SELECT name FROM squads WHERE id = %s", (t["squad_id"],)).fetchone()
                for name in set(MENTION.findall(body)):
                    u = c.execute("SELECT u.id FROM users u JOIN squad_members sm ON sm.user_id = u.id AND sm.squad_id = %s "
                                  "WHERE LOWER(u.username) = LOWER(%s) AND u.id <> %s", (t["squad_id"], name, uid)).fetchone()
                    if u:
                        h.notify(c, u[0], "squad_message", who + " mentioned you in " + sq[0], body[:100], link)
            c.commit()
        finally:
            c.close()
        if ajax:
            return jsonify(ok=True, id=mid)
        return redirect(url_for("thread", tid=tid) + "#end")

    def own_or_staff(c, t, tid, mid, uid):
        r = c.execute("SELECT user_id, kind, ref_id FROM thread_messages WHERE id = %s AND thread_id = %s", (mid, tid)).fetchone()
        if not r:
            abort(404)
        ok = r[0] == uid or (t["kind"] == "squad" and fc.is_staff(fc.squad_role(c, t["squad_id"], uid)))
        return r, ok

    @app.route("/messages/<int:tid>/delete/<int:mid>", methods=["POST"])
    @h.user_required
    @h.limiter.limit("60 per minute")
    def message_delete(tid, mid):
        h.validate_csrf()
        uid = uid_()
        c = h.get_db_connection()
        try:
            t = open_thread(c, tid, uid)
            r, ok = own_or_staff(c, t, tid, mid, uid)
            if not ok or r[1] == "system":
                abort(403)
            c.execute("UPDATE thread_messages SET deleted = TRUE, body = '' WHERE id = %s", (mid,))
            if r[1] == "file" and r[2]:
                c.execute("DELETE FROM files WHERE id = %s AND thread_id = %s", (r[2], tid))
            c.commit()
        finally:
            c.close()
        if request.headers.get("X-CSRF-Token"):
            return jsonify(ok=True)
        return redirect(url_for("thread", tid=tid))

    @app.route("/messages/<int:tid>/react/<int:mid>", methods=["POST"])
    @h.user_required
    @h.limiter.limit("120 per minute")
    def message_react(tid, mid):
        h.validate_csrf()
        uid = uid_()
        emoji = request.form.get("emoji", "")
        if emoji not in REACTIONS:
            abort(400)
        c = h.get_db_connection()
        try:
            open_thread(c, tid, uid)
            m = c.execute("SELECT deleted FROM thread_messages WHERE id = %s AND thread_id = %s", (mid, tid)).fetchone()
            if not m or m[0]:
                abort(404)
            if c.execute("SELECT 1 FROM message_reactions WHERE message_id = %s AND user_id = %s AND emoji = %s", (mid, uid, emoji)).fetchone():
                c.execute("DELETE FROM message_reactions WHERE message_id = %s AND user_id = %s AND emoji = %s", (mid, uid, emoji))
            else:
                c.execute("INSERT INTO message_reactions (message_id, user_id, emoji) VALUES (%s, %s, %s)", (mid, uid, emoji))
            c.commit()
        finally:
            c.close()
        if request.headers.get("X-CSRF-Token"):
            return jsonify(ok=True)
        return redirect(url_for("thread", tid=tid))

    @app.route("/files/<int:fid>")
    @h.user_required
    def chat_file(fid):
        uid = uid_()
        c = h.get_db_connection()
        try:
            r = c.execute("SELECT f.data, f.mime, f.filename, f.kind FROM files f "
                          "JOIN thread_members tm ON tm.thread_id = f.thread_id AND tm.user_id = %s WHERE f.id = %s", (uid, fid)).fetchone()
        finally:
            c.close()
        if not r:
            abort(404)
        return fc.file_response(r[0], r[1], r[2], r[3], download=bool(request.args.get("download")))
