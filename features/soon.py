"""Friendly 'coming soon' pages for sections whose phase is not installed yet.
Only endpoints that no real module has registered are added, so installing a later phase replaces them automatically."""
from flask import render_template, redirect, url_for, flash, request

# endpoint: (path, title, icon, phase, text)
PAGES = {
    "messages": ("/messages", "Messages", "messages", 5, "Private chats with your buddies and group chats for every squad."),
    "thread": ("/messages/<int:tid>", "Chat", "messages", 5, "Group chat, private messages, file sharing and reactions."),
    "problem_solver": ("/problem-solver", "Problem Solver", "problem_solver", 8, "Scan or type a maths problem and get step-by-step help."),
    "library": ("/library", "Academic Library", "library", 7, "Share and find notes, past papers and study material."),
    "leaderboard": ("/leaderboard", "Leaderboard", "leaderboard", 9, "Weekly and all-time rankings, plus group quizzes."),
    "study_sessions": ("/study-sessions", "Study Sessions", "study_sessions", 10, "Plan study sessions with your squads and get reminders."),
    "squad_questions": ("/squads/<int:sid>/questions", "Squad questions", "problem_solver", 6, "Share maths problems with your squad and answer each other."),
    "question_detail": ("/questions/<int:qid>", "Question", "problem_solver", 6, "Share maths problems with your squad and answer each other."),
    "squad_quizzes": ("/squads/<int:sid>/quizzes", "Squad quizzes", "leaderboard", 9, "Create group quizzes and compete with your squad."),
    "squad_sessions": ("/squads/<int:sid>/sessions", "Squad study sessions", "study_sessions", 10, "Plan study sessions with your squad."),
    "session_detail": ("/study-sessions/<int:sid>", "Study session", "study_sessions", 10, "Plan study sessions with your squad."),
}

# Existing parts of the site that stand in until the new page is installed
ALT = {"messages": ("Open the group chat", "chat"), "thread": ("Open the group chat", "chat"),
       "leaderboard": ("See the Maths Sprint leaderboard", "play")}


def register(app, h):
    def make(ep, title, icon_name, phase, blurb):
        @h.user_required
        def view(**kwargs):
            alt = ALT.get(ep)
            alt = (alt[0], url_for(alt[1])) if alt else ("Back to dashboard", url_for("dashboard"))
            return render_template("coming_soon.html", title=title, icon_name=icon_name, phase=phase, blurb=blurb, alt=alt)
        view.__name__ = "soon_" + ep
        return view

    for ep, (path, title, icon_name, phase, blurb) in PAGES.items():
        if ep not in app.view_functions:
            app.add_url_rule(path, ep, make(ep, title, icon_name, phase, blurb))

    if "dm_start" not in app.view_functions:
        @app.route("/messages/start/<int:other>", methods=["POST"], endpoint="dm_start")
        @h.user_required
        def dm_start_soon(other):
            h.validate_csrf()
            flash("Private messages arrive in the next update.", "success")
            return redirect(h.safe_next(request.form.get("next"), url_for("buddies")))
