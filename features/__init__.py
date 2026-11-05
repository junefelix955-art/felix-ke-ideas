"""Student-platform features, one module per phase. app.py calls init_schema(), register_all() and dashboard_extras()."""
import importlib

from . import schema
from . import common as fc

MODULES = ("buddies", "squads", "chat", "questions", "library", "solver")


def init_schema(connection):
    for sql in schema.STATEMENTS:
        connection.execute(sql)


def register_all(app, h):
    """Register every feature module that exists. Pages of phases not built yet get a friendly 'coming soon' page (soon.py)."""
    for name in MODULES:
        try:
            mod = importlib.import_module("." + name, __name__)
        except ModuleNotFoundError as exc:
            if exc.name != __name__ + "." + name:
                raise  # a real missing dependency inside an existing module must not be hidden
            continue
        mod.register(app, h)
    from . import soon
    soon.register(app, h)


def dashboard_extras(c, uid):
    """Small extra panels on the dashboard: squads, online buddies, points and upcoming sessions."""
    squads = [{"id": r[0], "name": r[1], "thread_id": r[2], "unread": r[3]} for r in c.execute(
        "SELECT s.id, s.name, t.id, (SELECT COUNT(*) FROM thread_messages m WHERE m.thread_id = t.id AND m.id > tm.last_read_id "
        "AND m.user_id <> %s AND NOT m.deleted AND m.kind <> 'system') FROM squad_members sm JOIN squads s ON s.id = sm.squad_id "
        "JOIN threads t ON t.squad_id = s.id JOIN thread_members tm ON tm.thread_id = t.id AND tm.user_id = sm.user_id "
        "WHERE sm.user_id = %s AND NOT s.archived ORDER BY s.name LIMIT 4", (uid, uid)).fetchall()]
    cutoff = fc.utcnow() - fc.timedelta(seconds=fc.ONLINE_SECONDS)
    buddies = [{"id": r[0], "username": r[1], "full_name": r[2], "avatar_v": r[3]} for r in c.execute(
        "SELECT u.id, u.username, u.full_name, u.avatar_v FROM friendships f "
        "JOIN users u ON u.id = CASE WHEN f.user_a = %s THEN f.user_b ELSE f.user_a END "
        "WHERE f.status = 'accepted' AND (f.user_a = %s OR f.user_b = %s) AND u.last_active > %s "
        "ORDER BY u.username LIMIT 8", (uid, uid, uid, cutoff)).fetchall()]
    sessions = []
    if c.execute("SELECT 1 FROM study_sessions LIMIT 1").fetchone():
        sessions = [{"id": r[0], "topic": r[1], "starts_at": r[2], "squad": r[3]} for r in c.execute(
            "SELECT ss.id, ss.topic, ss.starts_at, s.name FROM study_sessions ss JOIN squads s ON s.id = ss.squad_id "
            "JOIN squad_members sm ON sm.squad_id = ss.squad_id AND sm.user_id = %s "
            "WHERE NOT ss.cancelled AND ss.starts_at > %s ORDER BY ss.starts_at LIMIT 3", (uid, fc.utcnow())).fetchall()]
    return {"squads": squads, "buddies": buddies, "sessions": sessions,
            "points": fc.total_points(c, uid), "week_points": fc.total_points(c, uid, days=7)}
