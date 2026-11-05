"""Phase 9: leaderboard (points) and squad quizzes."""
import json
from datetime import timedelta

from flask import render_template, request, redirect, url_for, session, flash, abort

from . import common as fc

PERIODS = {"week": ("This week", 7), "month": ("This month", 30), "all": ("All time", None)}
MAX_Q = 20
GRACE = 45  # seconds of slack for slow networks when a quiz is timed


def norm(s):
    return " ".join((s or "").lower().split())


def is_correct(kind, given, answer):
    if kind == "short":
        g, a = norm(given), norm(answer)
        if g == a:
            return True
        try:
            return abs(float(g) - float(a)) < 1e-9
        except ValueError:
            return False
    return norm(given) == norm(answer)


def register(app, h):
    # ------------------------------------------------------------ leaderboard
    def board(c, since=None, squad_id=None, limit=25):
        sql = ("SELECT u.id, u.username, u.full_name, u.avatar_v, u.last_active, SUM(p.amount) AS pts FROM points p "
               "JOIN users u ON u.id = p.user_id WHERE 1 = 1")
        args = []
        if since:
            sql += " AND p.created_at > %s"
            args.append(since)
        if squad_id:
            sql += " AND p.user_id IN (SELECT user_id FROM squad_members WHERE squad_id = %s)"
            args.append(squad_id)
        sql += " GROUP BY u.id, u.username, u.full_name, u.avatar_v, u.last_active HAVING SUM(p.amount) > 0 ORDER BY pts DESC, u.username LIMIT %s"
        args.append(limit)
        rows = c.execute(sql, tuple(args)).fetchall()
        return [dict(zip(("id", "username", "full_name", "avatar_v", "last_active", "points"), r), rank=i + 1, online=fc.is_online(r[4]))
                for i, r in enumerate(rows)]

    @app.route("/leaderboard")
    @h.user_required
    def leaderboard():
        uid = session["user_id"]
        period = request.args.get("period") if request.args.get("period") in PERIODS else "week"
        days = PERIODS[period][1]
        since = fc.utcnow() - timedelta(days=days) if days else None
        scope = request.args.get("squad", "")
        c = h.get_db_connection()
        try:
            squads = c.execute("SELECT s.id, s.name FROM squad_members sm JOIN squads s ON s.id = sm.squad_id WHERE sm.user_id = %s ORDER BY s.name", (uid,)).fetchall()
            sid = None
            if scope.isdigit() and any(s[0] == int(scope) for s in squads):
                sid = int(scope)
            rows = board(c, since, sid)
            mine = next((r for r in rows if r["id"] == uid), None)
            my_points = fc.total_points(c, uid, days)
            if not mine and my_points:
                sql = ("SELECT COUNT(*) FROM (SELECT p.user_id FROM points p WHERE 1 = 1" + (" AND p.created_at > %s" if since else "") +
                       (" AND p.user_id IN (SELECT user_id FROM squad_members WHERE squad_id = %s)" if sid else "") +
                       " GROUP BY p.user_id HAVING SUM(p.amount) > %s) t")
                args = ([since] if since else []) + ([sid] if sid else []) + [my_points]
                mine_rank = c.execute(sql, tuple(args)).fetchone()[0] + 1
            else:
                mine_rank = mine["rank"] if mine else None
            breakdown = c.execute("SELECT kind, SUM(amount), COUNT(*) FROM points WHERE user_id = %s" + (" AND created_at > %s" if since else "") +
                                  " GROUP BY kind ORDER BY SUM(amount) DESC", (uid,) + ((since,) if since else ())).fetchall()
        finally:
            c.close()
        return render_template("leaderboard.html", rows=rows, period=period, periods=PERIODS, squads=squads, sid=sid,
                               my_points=my_points, my_rank=mine_rank, breakdown=breakdown)

    # ------------------------------------------------------------ quizzes
    def squad_ctx(c, sid, uid):
        sq = c.execute("SELECT id, name, archived, allow_member_quizzes FROM squads WHERE id = %s", (sid,)).fetchone()
        role = fc.squad_role(c, sid, uid) if sq else None
        if not sq or not role:
            abort(404)
        return {"id": sq[0], "name": sq[1], "archived": sq[2]}, role, (fc.is_staff(role) or bool(sq[3])) and not sq[2]

    def load_quiz(c, qid, uid):
        r = c.execute("SELECT z.id, z.squad_id, z.creator_id, z.title, z.description, z.duration_min, z.closes_at, z.published, z.created_at, u.username "
                      "FROM quizzes z JOIN users u ON u.id = z.creator_id JOIN squad_members sm ON sm.squad_id = z.squad_id AND sm.user_id = %s "
                      "WHERE z.id = %s", (uid, qid)).fetchone()
        if not r:
            abort(404)
        z = dict(zip(("id", "squad_id", "creator_id", "title", "description", "duration", "closes_at", "published", "created_at", "creator"), r))
        if not z["published"] and z["creator_id"] != uid and not fc.is_staff(fc.squad_role(c, z["squad_id"], uid)):
            abort(404)
        return z

    def questions_of(c, qid):
        out = []
        for r in c.execute("SELECT id, kind, prompt, options, answer, explanation FROM quiz_questions WHERE quiz_id = %s ORDER BY pos", (qid,)).fetchall():
            out.append({"id": r[0], "kind": r[1], "prompt": r[2], "options": json.loads(r[3]) if r[3] else [], "answer": r[4], "explanation": r[5]})
        return out

    @app.route("/squads/<int:sid>/quizzes")
    @h.user_required
    def squad_quizzes(sid):
        uid = session["user_id"]
        c = h.get_db_connection()
        try:
            sq, role, can_make = squad_ctx(c, sid, uid)
            rows = c.execute(
                "SELECT z.id, z.title, z.duration_min, z.closes_at, z.published, u.username, "
                "(SELECT COUNT(*) FROM quiz_questions k WHERE k.quiz_id = z.id), "
                "(SELECT a.score FROM quiz_attempts a WHERE a.quiz_id = z.id AND a.user_id = %s AND a.submitted_at IS NOT NULL), "
                "(SELECT a.max_score FROM quiz_attempts a WHERE a.quiz_id = z.id AND a.user_id = %s AND a.submitted_at IS NOT NULL), "
                "(SELECT COUNT(*) FROM quiz_attempts a WHERE a.quiz_id = z.id AND a.submitted_at IS NOT NULL) "
                "FROM quizzes z JOIN users u ON u.id = z.creator_id WHERE z.squad_id = %s AND (z.published OR z.creator_id = %s) ORDER BY z.id DESC",
                (uid, uid, sid, uid)).fetchall()
        finally:
            c.close()
        qs = [dict(zip(("id", "title", "duration", "closes_at", "published", "creator", "n", "score", "max", "takers"), r)) for r in rows]
        return render_template("squad_quizzes.html", squad=sq, quizzes=qs, can_make=can_make, now=fc.utcnow())

    def parse_form(form):
        """Read the repeated question fields. Returns (questions, error)."""
        kinds, prompts, opts, answers, expls = (form.getlist(k) for k in ("q_kind", "q_prompt", "q_opts", "q_answer", "q_expl"))
        out = []
        for i, prompt in enumerate(prompts[:MAX_Q]):
            prompt = " ".join(prompt.split())[:600]
            if not prompt:
                continue
            kind = kinds[i] if i < len(kinds) and kinds[i] in ("mcq", "tf", "short") else "mcq"
            ans = (answers[i] if i < len(answers) else "").strip()[:300]
            expl = (expls[i] if i < len(expls) else "").strip()[:600]
            n = len(out) + 1
            options = []
            if kind == "mcq":
                options = [o.strip()[:150] for o in (opts[i] if i < len(opts) else "").replace("\r", "").split("\n") if o.strip()][:6]
                if len(options) < 2:
                    return None, "Question %d needs at least two options (one per line)." % n
                if not ans.isdigit() or not 1 <= int(ans) <= len(options):
                    return None, "Question %d: type the NUMBER of the correct option (1 to %d)." % (n, len(options))
                ans = options[int(ans) - 1]
            elif kind == "tf":
                if norm(ans) not in ("true", "false"):
                    return None, "Question %d: the answer must be True or False." % n
                ans = "True" if norm(ans) == "true" else "False"
            elif not ans:
                return None, "Question %d needs an answer." % n
            out.append({"kind": kind, "prompt": prompt, "options": options, "answer": ans, "explanation": expl})
        if not out:
            return None, "Add at least one question."
        return out, None

    @app.route("/squads/<int:sid>/quizzes/new", methods=["GET", "POST"])
    @h.user_required
    @h.limiter.limit("10 per hour", methods=["POST"])
    def quiz_new(sid):
        uid = session["user_id"]
        c = h.get_db_connection()
        try:
            sq, role, can_make = squad_ctx(c, sid, uid)
            if not can_make:
                abort(403)
            if request.method == "POST":
                h.validate_csrf()
                f = request.form
                title = h.clean_text(f.get("title"), 100)
                qs, err = parse_form(f)
                try:
                    duration = min(180, max(1, int(f.get("duration", "10"))))
                except ValueError:
                    duration = 10
                closes = fc.parse_local(f.get("closes_at")) if f.get("closes_at") else None
                if len(title) < 3:
                    err = "Give the quiz a title."
                if err:
                    flash(err, "error")
                else:
                    publish = bool(f.get("publish"))
                    c.execute("INSERT INTO quizzes (squad_id, creator_id, title, description, duration_min, closes_at, published) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                              (sid, uid, title, h.clean_text(f.get("description"), 500), duration, closes, publish))
                    qid = c.execute("SELECT MAX(id) FROM quizzes WHERE creator_id = %s", (uid,)).fetchone()[0]
                    for pos, q in enumerate(qs, 1):
                        c.execute("INSERT INTO quiz_questions (quiz_id, pos, kind, prompt, options, answer, explanation) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                                  (qid, pos, q["kind"], q["prompt"], json.dumps(q["options"]), q["answer"], q["explanation"]))
                    if publish:
                        notify_members(c, sid, uid, qid, title, sq["name"])
                    c.commit()
                    flash("Quiz published." if publish else "Quiz saved as a draft.", "success")
                    return redirect(url_for("quiz_detail", qid=qid))
        finally:
            c.close()
        return render_template("quiz_form.html", squad=sq, f=request.form, nq=max(5, len(request.form.getlist("q_prompt"))), max_q=MAX_Q)

    def notify_members(c, sid, uid, qid, title, squad_name):
        for (m,) in c.execute("SELECT user_id FROM squad_members WHERE squad_id = %s AND user_id <> %s", (sid, uid)).fetchall():
            h.notify(c, m, "squad", "New quiz in " + squad_name, title, url_for("quiz_detail", qid=qid))

    def my_attempt(c, qid, uid):
        r = c.execute("SELECT id, started_at, submitted_at, score, max_score, late FROM quiz_attempts WHERE quiz_id = %s AND user_id = %s", (qid, uid)).fetchone()
        return dict(zip(("id", "started_at", "submitted_at", "score", "max", "late"), r)) if r else None

    @app.route("/quizzes/<int:qid>")
    @h.user_required
    def quiz_detail(qid):
        uid = session["user_id"]
        c = h.get_db_connection()
        try:
            z = load_quiz(c, qid, uid)
            role = fc.squad_role(c, z["squad_id"], uid)
            sq = c.execute("SELECT id, name, archived FROM squads WHERE id = %s", (z["squad_id"],)).fetchone()
            n = c.execute("SELECT COUNT(*) FROM quiz_questions WHERE quiz_id = %s", (qid,)).fetchone()[0]
            at = my_attempt(c, qid, uid)
            closed = bool(z["closes_at"] and z["closes_at"] < fc.utcnow())
            ranking = []
            if at and at["submitted_at"] or z["creator_id"] == uid or fc.is_staff(role):
                ranking = [dict(zip(("id", "username", "full_name", "avatar_v", "score", "max", "late", "started", "submitted"), r)) for r in c.execute(
                    "SELECT u.id, u.username, u.full_name, u.avatar_v, a.score, a.max_score, a.late, a.started_at, a.submitted_at "
                    "FROM quiz_attempts a JOIN users u ON u.id = a.user_id WHERE a.quiz_id = %s AND a.submitted_at IS NOT NULL", (qid,)).fetchall()]
        finally:
            c.close()
        for r in ranking:
            r["secs"] = int((r["submitted"] - r["started"]).total_seconds())
        ranking.sort(key=lambda r: (r["late"], -r["score"], r["secs"], r["username"]))
        for i, r in enumerate(ranking, 1):
            r["rank"] = i
        return render_template("quiz.html", z=z, squad={"id": sq[0], "name": sq[1], "archived": sq[2]}, role=role, n=n, at=at, closed=closed,
                               ranking=ranking, can_manage=(z["creator_id"] == uid or fc.is_staff(role)))

    @app.route("/quizzes/<int:qid>/take", methods=["GET", "POST"])
    @h.user_required
    @h.limiter.limit("30 per minute")
    def quiz_take(qid):
        uid = session["user_id"]
        c = h.get_db_connection()
        try:
            z = load_quiz(c, qid, uid)
            sq = c.execute("SELECT archived FROM squads WHERE id = %s", (z["squad_id"],)).fetchone()
            if not z["published"] or sq[0]:
                abort(404)
            at = my_attempt(c, qid, uid)
            if at and at["submitted_at"]:
                flash("You have already taken this quiz.", "info")
                return redirect(url_for("quiz_results", qid=qid))
            if not at:
                if request.method != "POST":
                    return redirect(url_for("quiz_detail", qid=qid))
                h.validate_csrf()
                if z["closes_at"] and z["closes_at"] < fc.utcnow():
                    flash("This quiz is closed.", "error")
                    return redirect(url_for("quiz_detail", qid=qid))
                c.execute("INSERT INTO quiz_attempts (quiz_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (qid, uid))
                c.commit()
                at = my_attempt(c, qid, uid)
            qs = questions_of(c, qid)
        finally:
            c.close()
        for q in qs:
            q.pop("answer"), q.pop("explanation")  # never send answers to the browser
        deadline = at["started_at"] + timedelta(minutes=z["duration"])
        left = max(0, int((deadline - fc.utcnow()).total_seconds()))
        return render_template("quiz_take.html", z=z, questions=qs, left=left)

    @app.route("/quizzes/<int:qid>/submit", methods=["POST"])
    @h.user_required
    @h.limiter.limit("20 per minute")
    def quiz_submit(qid):
        h.validate_csrf()
        uid = session["user_id"]
        c = h.get_db_connection()
        try:
            z = load_quiz(c, qid, uid)
            at = my_attempt(c, qid, uid)
            if not at or at["submitted_at"]:
                return redirect(url_for("quiz_results", qid=qid) if at else url_for("quiz_detail", qid=qid))
            now = fc.utcnow()
            late = now > at["started_at"] + timedelta(minutes=z["duration"], seconds=GRACE)
            qs = questions_of(c, qid)
            score = 0
            for q in qs:
                given = request.form.get("a_%d" % q["id"], "").strip()[:400]
                ok = bool(given) and is_correct(q["kind"], given, q["answer"])
                score += ok
                c.execute("INSERT INTO quiz_answers (attempt_id, question_id, answer, correct) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                          (at["id"], q["id"], given, ok))
            c.execute("UPDATE quiz_attempts SET submitted_at = %s, score = %s, max_score = %s, late = %s WHERE id = %s", (now, score, len(qs), late, at["id"]))
            pts = 0 if late else fc.award(c, uid, "quiz", amount=2 * score, squad_id=z["squad_id"], ref=qid)
            c.commit()
        finally:
            c.close()
        flash("Quiz submitted: %d / %d.%s" % (score, len(qs), " Submitted after time, so it is not ranked." if late else (" +%d points" % pts if pts else "")), "success")
        return redirect(url_for("quiz_results", qid=qid))

    @app.route("/quizzes/<int:qid>/results")
    @h.user_required
    def quiz_results(qid):
        uid = session["user_id"]
        c = h.get_db_connection()
        try:
            z = load_quiz(c, qid, uid)
            at = my_attempt(c, qid, uid)
            if not at or not at["submitted_at"]:
                flash("Take the quiz first to see the answers.", "info")
                return redirect(url_for("quiz_detail", qid=qid))
            given = {r[0]: (r[1], r[2]) for r in c.execute("SELECT question_id, answer, correct FROM quiz_answers WHERE attempt_id = %s", (at["id"],)).fetchall()}
            qs = questions_of(c, qid)
        finally:
            c.close()
        for q in qs:
            q["given"], q["ok"] = given.get(q["id"], ("", False))
        return render_template("quiz_results.html", z=z, at=at, questions=qs)

    @app.route("/quizzes/<int:qid>/publish", methods=["POST"])
    @h.user_required
    def quiz_publish(qid):
        h.validate_csrf()
        uid = session["user_id"]
        c = h.get_db_connection()
        try:
            z = load_quiz(c, qid, uid)
            if z["creator_id"] != uid and not fc.is_staff(fc.squad_role(c, z["squad_id"], uid)):
                abort(403)
            if not z["published"]:
                c.execute("UPDATE quizzes SET published = TRUE WHERE id = %s", (qid,))
                name = c.execute("SELECT name FROM squads WHERE id = %s", (z["squad_id"],)).fetchone()[0]
                notify_members(c, z["squad_id"], uid, qid, z["title"], name)
                c.commit()
                flash("Quiz published to the squad.", "success")
        finally:
            c.close()
        return redirect(url_for("quiz_detail", qid=qid))

    @app.route("/quizzes/<int:qid>/delete", methods=["POST"])
    @h.user_required
    def quiz_delete(qid):
        h.validate_csrf()
        uid = session["user_id"]
        c = h.get_db_connection()
        try:
            z = load_quiz(c, qid, uid)
            if z["creator_id"] != uid and not fc.is_staff(fc.squad_role(c, z["squad_id"], uid)):
                abort(403)
            c.execute("DELETE FROM quizzes WHERE id = %s", (qid,))
            c.commit()
        finally:
            c.close()
        flash("Quiz deleted.", "success")
        return redirect(url_for("squad_quizzes", sid=z["squad_id"]))
