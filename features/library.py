"""Phase 7: academic library - upload, search/filter, preview, download."""
from flask import render_template, request, redirect, url_for, session, flash, abort

from . import common as fc

DOC_TYPES = ["Lecture Notes", "Past Paper", "Assignment", "Study Guide", "Textbook / Book", "Slides", "Lab / Project", "Code", "Other"]
SEMESTERS = ["1", "2", "3", "Summer"]
ALLOWED = {"pdf", "docx", "doc", "pptx", "text", "image"}
PER_PAGE = 12
COLS = "r.id, r.user_id, r.title, r.description, r.doc_type, r.university, r.course, r.acad_year, r.semester, r.subject, r.filename, r.kind, r.size, r.downloads, r.created_at, u.username"
KEYS = ("id", "user_id", "title", "description", "doc_type", "university", "course", "year", "semester", "subject", "filename", "kind", "size", "downloads", "created_at", "username")


def register(app, h):
    def like(s):
        return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    def item(r):
        d = dict(zip(KEYS, r))
        d["size_h"] = fc.human_size(d["size"])
        return d

    @app.route("/library")
    @h.user_required
    def library():
        a = request.args
        where, args = [], []
        q = a.get("q", "").strip()[:60]
        if q:
            where.append("(r.title ILIKE %s OR r.description ILIKE %s OR r.subject ILIKE %s OR r.course ILIKE %s)")
            args += [like(q)] * 4
        for field, col in (("type", "r.doc_type"), ("year", "r.acad_year"), ("semester", "r.semester")):
            if a.get(field):
                where.append(col + " = %s")
                args.append(a[field][:30])
        for field, col in (("university", "r.university"), ("course", "r.course"), ("subject", "r.subject")):
            if a.get(field, "").strip():
                where.append(col + " ILIKE %s")
                args.append(like(a[field].strip()[:80]))
        sql_where = (" WHERE " + " AND ".join(where)) if where else ""
        order = "r.downloads DESC, r.id DESC" if a.get("sort") == "popular" else "r.id DESC"
        try:
            page = max(1, int(a.get("page", "1")))
        except ValueError:
            page = 1
        c = h.get_db_connection()
        try:
            total = c.execute("SELECT COUNT(*) FROM resources r" + sql_where, tuple(args)).fetchone()[0]
            rows = c.execute("SELECT " + COLS + " FROM resources r JOIN users u ON u.id = r.user_id" + sql_where +
                             " ORDER BY " + order + " LIMIT %s OFFSET %s", tuple(args) + (PER_PAGE, (page - 1) * PER_PAGE)).fetchall()
        finally:
            c.close()
        return render_template("library.html", items=[item(r) for r in rows], total=total, page=page, pages=max(1, -(-total // PER_PAGE)),
                               doc_types=DOC_TYPES, semesters=SEMESTERS, f=a)

    @app.route("/library/upload", methods=["GET", "POST"])
    @h.user_required
    @h.limiter.limit("10 per hour;30 per day", methods=["POST"])
    def library_upload():
        uid = session["user_id"]
        form = request.form
        if request.method == "POST":
            h.validate_csrf()
            title = form.get("title", "").strip()[:120]
            up = request.files.get("file")
            err = None
            if len(title) < 3:
                err = "Please give the resource a title (at least 3 characters)."
            elif form.get("doc_type") not in DOC_TYPES:
                err = "Choose a document type."
            elif not up or not up.filename:
                err = "Choose a file to upload."
            data = b""
            info = None
            if not err:
                data = up.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    err = "Files can be up to 8 MB."
                else:
                    info = fc.sniff_file(data, up.filename, ALLOWED)
                    if not info:
                        err = "That file type is not allowed. Use PDF, Word, PowerPoint, an image or a text/code file."
            if err:
                flash(err, "error")
            else:
                c = h.get_db_connection()
                try:
                    name = fc.clean_filename(up.filename, info["kind"] if info["kind"] != "text" else None)
                    c.execute("INSERT INTO resources (user_id, title, description, doc_type, university, course, acad_year, semester, subject, "
                              "filename, mime, kind, size, data) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                              (uid, title, h.clean_text(form.get("description", ""), 500), form["doc_type"], h.clean_text(form.get("university", ""), 80),
                               h.clean_text(form.get("course", ""), 80), form.get("year", "").strip()[:12],
                               form.get("semester") if form.get("semester") in SEMESTERS else "", h.clean_text(form.get("subject", ""), 60),
                               name, info["mime"], info["kind"], len(data), data))
                    rid = c.execute("SELECT MAX(id) FROM resources WHERE user_id = %s", (uid,)).fetchone()[0]
                    pts = fc.award(c, uid, "resource", ref=rid)
                    c.commit()
                finally:
                    c.close()
                flash("Resource shared with the library." + (" +%d points" % pts if pts else ""), "success")
                return redirect(url_for("library_item", rid=rid))
        me = None
        if request.method == "GET":
            c = h.get_db_connection()
            try:
                me = c.execute("SELECT university, course FROM users WHERE id = %s", (uid,)).fetchone()
            finally:
                c.close()
            form = {"university": me[0] or "", "course": me[1] or ""}
        return render_template("library_upload.html", doc_types=DOC_TYPES, semesters=SEMESTERS, f=form)

    def load(c, rid):
        r = c.execute("SELECT " + COLS + " FROM resources r JOIN users u ON u.id = r.user_id WHERE r.id = %s", (rid,)).fetchone()
        if not r:
            abort(404)
        return item(r)

    @app.route("/library/<int:rid>")
    @h.user_required
    def library_item(rid):
        c = h.get_db_connection()
        try:
            it = load(c, rid)
        finally:
            c.close()
        return render_template("library_item.html", r=it)

    @app.route("/library/<int:rid>/file")
    @h.user_required
    def library_file(rid):
        download = bool(request.args.get("download"))
        c = h.get_db_connection()
        try:
            r = c.execute("SELECT data, mime, filename, kind FROM resources WHERE id = %s", (rid,)).fetchone()
            if not r:
                abort(404)
            if download:
                c.execute("UPDATE resources SET downloads = downloads + 1 WHERE id = %s", (rid,))
                c.commit()
        finally:
            c.close()
        return fc.file_response(r[0], r[1], r[2], r[3], download=download)

    @app.route("/library/<int:rid>/delete", methods=["POST"])
    @h.user_required
    def library_delete(rid):
        h.validate_csrf()
        c = h.get_db_connection()
        try:
            r = c.execute("SELECT user_id FROM resources WHERE id = %s", (rid,)).fetchone()
            if not r:
                abort(404)
            if r[0] != session["user_id"]:
                abort(403)
            c.execute("DELETE FROM resources WHERE id = %s", (rid,))
            c.commit()
        finally:
            c.close()
        flash("Resource deleted.", "success")
        return redirect(url_for("library"))
