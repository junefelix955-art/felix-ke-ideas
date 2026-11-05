"""Phase 6: maths questions shared with a squad - answers, comments, "helpful" votes and points."""
from flask import render_template, request, redirect, url_for, session, flash, abort

from . import common as fc

CATEGORIES = ["Algebra", "Calculus", "Geometry", "Trigonometry", "Statistics", "Linear Algebra", "Number Theory", "Other"]
LEVELS = ["Easy", "Medium", "Hard"]


def share_question(c, h, sid, uid, body, category="Algebra", difficulty="Medium", solution=""):
    """Create a question in a squad and post its card in the squad chat. Caller commits. Returns question id."""
    category = category if category in CATEGORIES else "Other"
    difficulty = difficulty if difficulty in LEVELS else "Medium"
    c.execute("INSERT INTO math_questions (squad_id, user_id, body, category, difficulty, solution) VALUES (%s, %s, %s, %s, %s, %s)",
              (sid, uid, body[:1500], category, difficulty, solution[:4000]))
    qid = c.execute("SELECT MAX(id) FROM math_questions WHERE user_id = %s", (uid,)).fetchone()[0]
    t = c.execute("SELECT id FROM threads WHERE squad_id = %s", (sid,)).fetchone()
    c.execute("INSERT INTO thread_messages (thread_id, user_id, body, kind, ref_id) VALUES (%s, %s, '', 'question', %s)", (t[0], uid, qid))
    mid = c.execute("SELECT MAX(id) FROM thread_messages WHERE thread_id = %s", (t[0],)).fetchone()[0]
    c.execute("UPDATE thread_members SET last_read_id = %s, last_delivered_id = %s WHERE thread_id = %s AND user_id = %s", (mid, mid, t[0], uid))
    fc.award(c, uid, "question", squad_id=sid, ref=qid)
    return qid


def register(app, h):
    def member(c, sid, uid):
        sq = c.execute("SELECT id, name, archived FROM squads WHERE id = %s", (sid,)).fetchone()
        role = fc.squad_role(c, sid, uid) if sq else None
        if not sq or not role:
            abort(404)
        return {"id": sq[0], "name": sq[1], "archived": sq[2]}, role

    def load_q(c, qid, uid):
        r = c.execute("SELECT q.id, q.squad_id, q.user_id, q.body, q.category, q.difficulty, q.solution, q.created_at, u.username, u.full_name, u.avatar_v "
                      "FROM math_questions q JOIN users u ON u.id = q.user_id "
                      "JOIN squad_members sm ON sm.squad_id = q.squad_id AND sm.user_id = %s WHERE q.id = %s", (uid, qid)).fetchone()
        if not r:
            abort(404)
        return dict(zip(("id", "squad_id", "user_id", "body", "category", "difficulty", "solution", "created_at", "username", "full_name", "avatar_v"), r))

    @app.route("/squads/<int:sid>/questions", methods=["GET", "POST"])
    @h.user_required
    @h.limiter.limit("20 per minute", methods=["POST"])
    def squad_questions(sid):
        uid = session["user_id"]
        c = h.get_db_connection()
        try:
            sq, role = member(c, sid, uid)
            if request.method == "POST":
                h.validate_csrf()
                body = request.form.get("body", "").replace("\r", "").strip()
                if sq["archived"]:
                    flash("This squad is archived.", "error")
                elif len(body) < 3:
                    flash("Please type the question.", "error")
                else:
                    qid = share_question(c, h, sid, uid, body, request.form.get("category", ""), request.form.get("difficulty", ""),
                                         request.form.get("solution", "").strip())
                    c.commit()
                    flash("Question shared with the squad.", "success")
                    return redirect(url_for("question_detail", qid=qid))
            cat = request.args.get("category", "")
            sql = ("SELECT q.id, q.body, q.category, q.difficulty, u.username, q.created_at, "
                   "(SELECT COUNT(*) FROM question_replies r WHERE r.question_id = q.id AND r.is_answer) "
                   "FROM math_questions q JOIN users u ON u.id = q.user_id WHERE q.squad_id = %s")
            args = [sid]
            if cat in CATEGORIES:
                sql += " AND q.category = %s"
                args.append(cat)
            rows = c.execute(sql + " ORDER BY q.id DESC LIMIT 100", tuple(args)).fetchall()
        finally:
            c.close()
        qs = [dict(zip(("id", "body", "category", "difficulty", "user", "created_at", "answers"), r)) for r in rows]
        return render_template("squad_questions.html", squad=sq, role=role, questions=qs, categories=CATEGORIES, levels=LEVELS,
                               cat=cat, prefill=request.form if request.method == "POST" else request.args)

    @app.route("/questions/<int:qid>")
    @h.user_required
    def question_detail(qid):
        uid = session["user_id"]
        c = h.get_db_connection()
        try:
            q = load_q(c, qid, uid)
            sq = c.execute("SELECT id, name, archived FROM squads WHERE id = %s", (q["squad_id"],)).fetchone()
            role = fc.squad_role(c, q["squad_id"], uid)
            rows = c.execute(
                "SELECT r.id, r.user_id, r.body, r.is_answer, r.created_at, u.username, u.full_name, u.avatar_v, "
                "(SELECT COUNT(*) FROM reply_helpful x WHERE x.reply_id = r.id), "
                "(SELECT COUNT(*) FROM reply_helpful x WHERE x.reply_id = r.id AND x.user_id = %s) "
                "FROM question_replies r JOIN users u ON u.id = r.user_id WHERE r.question_id = %s "
                "ORDER BY r.is_answer DESC, 9 DESC, r.id", (uid, qid)).fetchall()
        finally:
            c.close()
        replies = [dict(zip(("id", "user_id", "body", "is_answer", "created_at", "username", "full_name", "avatar_v", "helpful", "voted"), r)) for r in rows]
        return render_template("question.html", q=q, squad={"id": sq[0], "name": sq[1], "archived": sq[2]}, role=role, replies=replies)

    @app.route("/questions/<int:qid>/reply", methods=["POST"])
    @h.user_required
    @h.limiter.limit("20 per minute;200 per day")
    def question_reply(qid):
        h.validate_csrf()
        uid = session["user_id"]
        body = request.form.get("body", "").replace("\r", "").strip()[:2000]
        c = h.get_db_connection()
        try:
            q = load_q(c, qid, uid)
            if c.execute("SELECT archived FROM squads WHERE id = %s", (q["squad_id"],)).fetchone()[0]:
                flash("This squad is archived.", "error")
            elif len(body) < 2:
                flash("Please write your reply.", "error")
            else:
                is_answer = bool(request.form.get("is_answer"))
                c.execute("INSERT INTO question_replies (question_id, user_id, body, is_answer) VALUES (%s, %s, %s, %s)", (qid, uid, body, is_answer))
                rid = c.execute("SELECT MAX(id) FROM question_replies WHERE user_id = %s", (uid,)).fetchone()[0]
                pts = 0
                if is_answer and uid != q["user_id"]:
                    pts = fc.award(c, uid, "answer", squad_id=q["squad_id"], ref=qid)  # one reward per question
                if uid != q["user_id"]:
                    who = c.execute("SELECT COALESCE(NULLIF(full_name, ''), username) FROM users WHERE id = %s", (uid,)).fetchone()[0]
                    h.notify(c, q["user_id"], "answer", who + (" answered" if is_answer else " replied to") + " your question", body[:100],
                             url_for("question_detail", qid=qid) + "#r%d" % rid)
                c.commit()
                flash("Reply posted." + (" +%d points" % pts if pts else ""), "success")
        finally:
            c.close()
        return redirect(url_for("question_detail", qid=qid))

    @app.route("/replies/<int:rid>/helpful", methods=["POST"])
    @h.user_required
    @h.limiter.limit("60 per minute")
    def reply_helpful(rid):
        h.validate_csrf()
        uid = session["user_id"]
        c = h.get_db_connection()
        try:
            r = c.execute("SELECT question_id, user_id FROM question_replies WHERE id = %s", (rid,)).fetchone()
            if not r:
                abort(404)
            q = load_q(c, r[0], uid)  # 404 unless the voter belongs to the squad
            if r[1] == uid:
                flash("You cannot mark your own reply as helpful.", "error")
            elif c.execute("SELECT 1 FROM reply_helpful WHERE reply_id = %s AND user_id = %s", (rid, uid)).fetchone():
                c.execute("DELETE FROM reply_helpful WHERE reply_id = %s AND user_id = %s", (rid, uid))
            else:
                c.execute("INSERT INTO reply_helpful (reply_id, user_id) VALUES (%s, %s)", (rid, uid))
                if fc.award(c, r[1], "helpful", squad_id=q["squad_id"], ref=rid * 1000000 + uid):
                    h.notify(c, r[1], "answer", "Someone found your reply helpful", "+10 points", url_for("question_detail", qid=r[0]) + "#r%d" % rid)
            c.commit()
        finally:
            c.close()
        return redirect(url_for("question_detail", qid=r[0]) + "#r%d" % rid)

    @app.route("/questions/<int:qid>/delete", methods=["POST"])
    @h.user_required
    def question_delete(qid):
        h.validate_csrf()
        uid = session["user_id"]
        c = h.get_db_connection()
        try:
            q = load_q(c, qid, uid)
            if q["user_id"] != uid and not fc.is_staff(fc.squad_role(c, q["squad_id"], uid)):
                abort(403)
            c.execute("UPDATE thread_messages SET deleted = TRUE WHERE kind = 'question' AND ref_id = %s", (qid,))
            c.execute("DELETE FROM math_questions WHERE id = %s", (qid,))
            c.commit()
        finally:
            c.close()
        flash("Question deleted.", "success")
        return redirect(url_for("squad_questions", sid=q["squad_id"]))

    @app.route("/replies/<int:rid>/delete", methods=["POST"])
    @h.user_required
    def reply_delete(rid):
        h.validate_csrf()
        uid = session["user_id"]
        c = h.get_db_connection()
        try:
            r = c.execute("SELECT question_id, user_id FROM question_replies WHERE id = %s", (rid,)).fetchone()
            if not r:
                abort(404)
            q = load_q(c, r[0], uid)
            if r[1] != uid and not fc.is_staff(fc.squad_role(c, q["squad_id"], uid)):
                abort(403)
            c.execute("DELETE FROM question_replies WHERE id = %s", (rid,))
            c.commit()
        finally:
            c.close()
        return redirect(url_for("question_detail", qid=r[0]))
