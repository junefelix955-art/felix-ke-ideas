"""Phase 8: math problem scanner/solver page. OCR runs in the browser (Tesseract.js); solving runs on the server."""
from flask import render_template, request, redirect, url_for, session, flash, abort

from . import common as fc
from . import mathsolver as ms
from .questions import share_question


def register(app, h):
    def history(c, uid, saved=False):
        sql = ("SELECT id, question, category, steps, answer, saved, created_at FROM math_solves WHERE user_id = %s"
               + (" AND saved" if saved else "") + " ORDER BY id DESC LIMIT 20")
        return [dict(zip(("id", "question", "category", "steps", "answer", "saved", "created_at"), r)) for r in c.execute(sql, (uid,)).fetchall()]

    @app.route("/solver", methods=["GET", "POST"])
    @h.user_required
    @h.limiter.limit("30 per minute;300 per day", methods=["POST"])
    def problem_solver():
        uid = session["user_id"]
        result, text = None, ""
        c = h.get_db_connection()
        try:
            if request.method == "POST":
                h.validate_csrf()
                text = request.form.get("problem", "").replace("\r", "").strip()[:ms.MAX_LEN + 20]
                try:
                    r = ms.solve(text)
                    steps = "\n".join(r["steps"])
                    c.execute("INSERT INTO math_solves (user_id, question, category, steps, answer) VALUES (%s, %s, %s, %s, %s)",
                              (uid, text[:500], r["category"], steps[:6000], r["answer"][:300]))
                    sid = c.execute("SELECT MAX(id) FROM math_solves WHERE user_id = %s", (uid,)).fetchone()[0]
                    c.commit()
                    result = {"id": sid, "question": text, "category": r["category"], "steps": r["steps"], "answer": r["answer"]}
                except ms.SolveError as e:
                    flash(str(e), "error")
                except (RecursionError, OverflowError, ValueError, ZeroDivisionError, ArithmeticError):
                    flash("I could not work that one out. Check the notation, or try a simpler form.", "error")
            tab = "saved" if request.args.get("tab") == "saved" else "recent"
            hist = history(c, uid, saved=(tab == "saved"))
            squads = c.execute("SELECT s.id, s.name FROM squad_members sm JOIN squads s ON s.id = sm.squad_id "
                               "WHERE sm.user_id = %s AND NOT s.archived ORDER BY s.name", (uid,)).fetchall()
        finally:
            c.close()
        return render_template("solver.html", result=result, text=text, hist=hist, tab=tab, squads=squads)

    def own(c, sid, uid):
        r = c.execute("SELECT id, question, category, steps, answer, saved FROM math_solves WHERE id = %s AND user_id = %s", (sid, uid)).fetchone()
        if not r:
            abort(404)
        return dict(zip(("id", "question", "category", "steps", "answer", "saved"), r))

    @app.route("/solver/<int:sid>")
    @h.user_required
    def solve_view(sid):
        c = h.get_db_connection()
        try:
            r = own(c, sid, session["user_id"])
            squads = c.execute("SELECT s.id, s.name FROM squad_members sm JOIN squads s ON s.id = sm.squad_id "
                               "WHERE sm.user_id = %s AND NOT s.archived ORDER BY s.name", (session["user_id"],)).fetchall()
            hist = history(c, session["user_id"])
        finally:
            c.close()
        r["steps"] = r["steps"].split("\n")
        return render_template("solver.html", result=r, text=r["question"], hist=hist, tab="recent", squads=squads)

    @app.route("/solver/<int:sid>/save", methods=["POST"])
    @h.user_required
    def solve_save(sid):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            r = own(c, sid, session["user_id"])
            c.execute("UPDATE math_solves SET saved = %s WHERE id = %s", (not r["saved"], sid))
            c.commit()
        finally:
            c.close()
        return redirect(url_for("solve_view", sid=sid))

    @app.route("/solver/<int:sid>/delete", methods=["POST"])
    @h.user_required
    def solve_delete(sid):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            own(c, sid, session["user_id"])
            c.execute("DELETE FROM math_solves WHERE id = %s", (sid,))
            c.commit()
        finally:
            c.close()
        flash("Removed from your history.", "success")
        return redirect(url_for("problem_solver"))

    @app.route("/solver/<int:sid>/share", methods=["POST"])
    @h.user_required
    @h.limiter.limit("10 per minute")
    def solve_share(sid):
        h.validate_csrf()
        uid = session["user_id"]
        try:
            squad_id = int(request.form.get("squad_id", ""))
        except ValueError:
            abort(400)
        c = h.get_db_connection()
        try:
            r = own(c, sid, uid)
            sq = c.execute("SELECT s.archived FROM squads s JOIN squad_members sm ON sm.squad_id = s.id AND sm.user_id = %s WHERE s.id = %s", (uid, squad_id)).fetchone()
            if not sq or sq[0]:
                abort(404)
            cat = r["category"] if r["category"] in ("Algebra", "Calculus", "Statistics") else "Other"
            qid = share_question(c, h, squad_id, uid, r["question"], cat, "Medium", r["steps"] + "\nAnswer: " + r["answer"])
            c.commit()
        finally:
            c.close()
        flash("Shared with your squad.", "success")
        return redirect(url_for("question_detail", qid=qid))

    # older links (/problem-solver) keep working
    app.add_url_rule("/problem-solver", "problem_solver_alias", lambda: redirect(url_for("problem_solver")))
