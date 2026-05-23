from functools import wraps
from flask import (
    Flask, render_template, request, redirect,
    session, flash, url_for
)
import sqlite3
import json
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "smartlearn-secret-change-in-production"


def get_db():
    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        xp INTEGER DEFAULT 0,
        streak INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS study_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        subject TEXT NOT NULL,
        study_date TEXT NOT NULL,
        topic TEXT NOT NULL,
        completed INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS planner (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        subject_name TEXT DEFAULT '',
        total_topics INTEGER NOT NULL,
        completed_topics INTEGER DEFAULT 0,
        exam_date TEXT NOT NULL,
        difficulty TEXT NOT NULL,
        confidence TEXT DEFAULT 'Medium',
        pace TEXT DEFAULT 'Steady',
        revision_days INTEGER DEFAULT 3,
        study_days_per_week INTEGER DEFAULT 6,
        free_hours REAL NOT NULL,
        study_time REAL NOT NULL,
        learning_hours REAL DEFAULT 0,
        revision_hours REAL DEFAULT 0,
        topics_per_day REAL DEFAULT 0,
        weekly_hours REAL DEFAULT 0,
        today_topics INTEGER DEFAULT 0,
        risk_status TEXT DEFAULT 'On Track',
        plan_json TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS marks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        semester TEXT NOT NULL,
        subject TEXT NOT NULL,
        marks INTEGER NOT NULL,
        total_marks INTEGER NOT NULL,
        percentage REAL NOT NULL
    );
    """)
    conn.commit()
    conn.close()


init_db()


def migrate_planner_schema():
    conn = get_db()
    existing = {row[1] for row in conn.execute("PRAGMA table_info(planner)").fetchall()}
    additions = [
        ("subject_name", "TEXT DEFAULT ''"),
        ("completed_topics", "INTEGER DEFAULT 0"),
        ("confidence", "TEXT DEFAULT 'Medium'"),
        ("pace", "TEXT DEFAULT 'Steady'"),
        ("revision_days", "INTEGER DEFAULT 3"),
        ("study_days_per_week", "INTEGER DEFAULT 6"),
        ("learning_hours", "REAL DEFAULT 0"),
        ("revision_hours", "REAL DEFAULT 0"),
        ("topics_per_day", "REAL DEFAULT 0"),
        ("weekly_hours", "REAL DEFAULT 0"),
        ("today_topics", "INTEGER DEFAULT 0"),
        ("risk_status", "TEXT DEFAULT 'On Track'"),
        ("plan_json", "TEXT"),
    ]
    for col, typedef in additions:
        if col not in existing:
            conn.execute(f"ALTER TABLE planner ADD COLUMN {col} {typedef}")
    conn.commit()
    conn.close()


def migrate_extra_schema():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS study_goals (
        student_id INTEGER PRIMARY KEY,
        daily_topics INTEGER DEFAULT 3,
        daily_hours REAL DEFAULT 2,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS subject_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        subject TEXT NOT NULL,
        note_text TEXT NOT NULL,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(student_id, subject)
    );
    CREATE TABLE IF NOT EXISTS activity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        subject TEXT DEFAULT '',
        detail TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(study_records)").fetchall()}
    if "priority" not in cols:
        conn.execute("ALTER TABLE study_records ADD COLUMN priority TEXT DEFAULT 'Medium'")
    conn.commit()
    conn.close()


migrate_planner_schema()
migrate_extra_schema()


def badge_for(xp, streak):
    if streak >= 7:
        return "7 Day Streak"
    if xp >= 100:
        return "Scholar"
    if xp >= 50:
        return "Quiz Master"
    return "Beginner"


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please login first.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def get_user(conn, user_id):
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def study_progress(conn, user_id):
    records = conn.execute(
        "SELECT completed FROM study_records WHERE student_id=?",
        (user_id,),
    ).fetchall()
    total = len(records)
    done = sum(1 for r in records if r["completed"] == 1)
    pct = int((done / total) * 100) if total else 0
    return total, done, pct


def tracker_subjects(conn, user_id):
    rows = conn.execute(
        """
        SELECT DISTINCT subject FROM study_records
        WHERE student_id=? AND subject != ''
        ORDER BY subject
        """,
        (user_id,),
    ).fetchall()
    return [r["subject"] for r in rows]


def tracker_completed_count(conn, user_id, subject=None):
    if subject:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM study_records
            WHERE student_id=? AND completed=1 AND subject=?
            """,
            (user_id, subject),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM study_records
            WHERE student_id=? AND completed=1
            """,
            (user_id,),
        ).fetchone()
    return row["cnt"] if row else 0


def build_study_plan(total_topics, completed_topics, exam_date, difficulty,
                     confidence, free_hours, revision_days, study_days_per_week, pace):
    remaining = max(total_topics - completed_topics, 0)
    today = datetime.today().date()
    exam = datetime.strptime(exam_date, "%Y-%m-%d").date()
    total_days = max((exam - today).days, 1)
    revision_days = min(max(revision_days, 0), max(total_days - 1, 0))
    learning_days = max(total_days - revision_days, 1)
    study_ratio = min(max(study_days_per_week, 1), 7) / 7.0
    effective_days = max(int(learning_days * study_ratio), 1)
    diff_h = {"Easy": 0.75, "Medium": 1.0, "Hard": 1.35}
    conf_m = {"Low": 1.25, "Medium": 1.0, "High": 0.85}
    pace_m = {"Light": 0.9, "Steady": 1.0, "Intensive": 1.15}
    hpt = diff_h.get(difficulty, 1) * conf_m.get(confidence, 1) * pace_m.get(pace, 1)
    if remaining == 0:
        return {"days_left": total_days, "revision_days": revision_days,
                "remaining_topics": 0, "learning_hours": 0, "revision_hours": 0,
                "study_time": 0, "topics_per_day": 0, "weekly_hours": 0, "today_topics": 0,
                "risk_status": "Complete", "risk_tip": "Syllabus complete.",
                "milestones": [], "weekly_plan": [],
                "phase_split": {"learn": 0, "revise": revision_days}}
    tlh = remaining * hpt
    dl = round(tlh / effective_days, 1)
    rht = round(tlh * 0.3, 1)
    dr = round(rht / revision_days, 1) if revision_days else 0
    dt = round(dl + dr, 1)
    tpd = round(remaining / effective_days, 1)
    wh = round(dt * study_days_per_week, 1)
    tt = max(1, int(tpd + 0.6))
    if dt <= free_hours * 0.8:
        risk, tip = "On Track", "Comfortable pace."
    elif dt <= free_hours:
        risk, tip = "Tight", "Stay consistent."
    else:
        risk, tip = "Critical", "Reduce topics or add hours."
    milestones = []
    for pct in (25, 50, 75, 100):
        milestones.append({
            "label": f"{pct}% syllabus",
            "topics": max(1, round(remaining * pct / 100)),
            "date": (today + timedelta(days=max(1, round(effective_days * pct / 100)))).strftime("%Y-%m-%d"),
        })
    weekly_plan, left, cur, wn = [], remaining, today, 1
    while left > 0 and cur < exam and wn <= 12:
        wt = min(max(1, round(tpd * study_days_per_week)), left)
        weekly_plan.append({"week": wn, "start": cur.strftime("%Y-%m-%d"),
            "end": min(cur + timedelta(days=6), exam - timedelta(days=1)).strftime("%Y-%m-%d"),
            "topics": wt, "hours": round(dt * study_days_per_week, 1)})
        left -= wt
        cur += timedelta(days=7)
        wn += 1
    return {"days_left": total_days, "revision_days": revision_days,
            "remaining_topics": remaining, "learning_hours": dl, "revision_hours": dr,
            "study_time": dt, "topics_per_day": tpd, "weekly_hours": wh, "today_topics": tt,
            "risk_status": risk, "risk_tip": tip, "milestones": milestones,
            "weekly_plan": weekly_plan, "phase_split": {"learn": learning_days, "revise": revision_days}}


def parse_plan_json(row):
    if not row:
        return None
    data = dict(row)
    if data.get("plan_json"):
        try:
            data["insight"] = json.loads(data["plan_json"])
        except json.JSONDecodeError:
            data["insight"] = None
    else:
        data["insight"] = None
    return data


def subject_stats(conn, user_id):
    return [dict(r) for r in conn.execute(
        "SELECT subject, COUNT(*) AS total, SUM(CASE WHEN completed=1 THEN 1 ELSE 0 END) AS done "
        "FROM study_records WHERE student_id=? GROUP BY subject ORDER BY subject", (user_id,)).fetchall()]


def marks_by_subject(conn, user_id):
    return {r["subject"]: round(r["avg_pct"], 1) for r in conn.execute(
        "SELECT subject, AVG(percentage) AS avg_pct FROM marks WHERE student_id=? GROUP BY subject",
        (user_id,)).fetchall()}


def study_activity_week(conn, user_id):
    today = datetime.today().date()
    activity, mx = [], 1
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        ds = day.strftime("%Y-%m-%d")
        c = conn.execute("SELECT COUNT(*) AS cnt FROM study_records WHERE student_id=? AND study_date=?",
                           (user_id, ds)).fetchone()["cnt"]
        mx = max(mx, c)
        activity.append({"label": day.strftime("%a"), "date": ds, "count": c})
    for a in activity:
        a["height"] = int((a["count"] / mx) * 100) if mx else 0
    return activity


def compute_subject_health(conn, user_id):
    marks_map = marks_by_subject(conn, user_id)
    health = []
    for s in subject_stats(conn, user_id):
        subj, total, done = s["subject"], s["total"], s["done"]
        comp = int((done / total) * 100) if total else 0
        mp = marks_map.get(subj)
        ms = mp if mp is not None else 55
        rec = conn.execute("SELECT COUNT(*) AS c FROM study_records WHERE student_id=? AND subject=? "
                           "AND study_date >= date('now', '-3 days')", (user_id, subj)).fetchone()["c"]
        asc = 100 if rec else (40 if done else 15)
        score = min(100, int(comp * 0.4 + ms * 0.4 + asc * 0.2))
        st = "Strong" if score >= 75 else ("Needs Work" if score >= 50 else "Critical")
        health.append({"subject": subj, "score": score, "status": st, "completion": comp,
                       "pending": total - done, "marks_pct": mp})
    health.sort(key=lambda x: x["score"])
    return health


def learning_velocity(conn, user_id):
    return conn.execute("SELECT COUNT(*) AS c FROM study_records WHERE student_id=? AND completed=1 "
                        "AND study_date >= date('now', '-7 days')", (user_id,)).fetchone()["c"]


def letter_grade(pct):
    if pct >= 90: return "A+"
    if pct >= 80: return "A"
    if pct >= 70: return "B+"
    if pct >= 60: return "B"
    if pct >= 50: return "C"
    if pct >= 40: return "D"
    return "F"


def get_revision_queue(conn, user_id, after_days=7):
    return [dict(r) for r in conn.execute(
        "SELECT *, CAST(julianday('now') - julianday(study_date) AS INTEGER) AS days_ago "
        "FROM study_records WHERE student_id=? AND completed=1 AND study_date < date('now', ?) "
        "ORDER BY study_date ASC LIMIT 15", (user_id, f"-{after_days} days")).fetchall()]


def get_daily_goal(conn, user_id):
    row = conn.execute("SELECT * FROM study_goals WHERE student_id=?", (user_id,)).fetchone()
    return dict(row) if row else {"daily_topics": 3, "daily_hours": 2}


def daily_goal_progress(conn, user_id):
    goal = get_daily_goal(conn, user_id)
    today = datetime.today().strftime("%Y-%m-%d")
    done = conn.execute("SELECT COUNT(*) AS c FROM study_records WHERE student_id=? AND study_date=? AND completed=1",
                        (user_id, today)).fetchone()["c"]
    tgt = goal["daily_topics"]
    return {"target_topics": tgt, "target_hours": goal["daily_hours"], "done_today": done,
            "percent": min(100, int((done / tgt) * 100)) if tgt else 0, "met": done >= tgt}


def exam_readiness_scores(conn, user_id):
    stats = subject_stats(conn, user_id)
    mm = marks_by_subject(conn, user_id)
    subs = set(mm.keys()) | {s["subject"] for s in stats}
    out = []
    for subj in sorted(subs):
        st = next((x for x in stats if x["subject"] == subj), None)
        comp = int((st["done"] / st["total"]) * 100) if st and st["total"] else 0
        mp = mm.get(subj)
        if comp and mp is not None: r = int(comp * 0.45 + mp * 0.55)
        elif comp: r = int(comp * 0.85)
        elif mp is not None: r = int(mp * 0.85)
        else: r = 0
        lbl = "Exam Ready" if r >= 75 else ("Almost Ready" if r >= 50 else "Needs Study")
        out.append({"subject": subj, "readiness": r, "label": lbl, "completion": comp, "marks_pct": mp})
    out.sort(key=lambda x: x["readiness"])
    return out


def marks_with_grades(conn, user_id):
    out = []
    for r in conn.execute("SELECT * FROM marks WHERE student_id=? ORDER BY id DESC", (user_id,)).fetchall():
        d = dict(r)
        d["grade"] = letter_grade(d["percentage"])
        out.append(d)
    return out


def calc_sgpa(marks_list):
    if not marks_list: return 0
    gp = {"A+": 10, "A": 9, "B+": 8, "B": 7, "C": 6, "D": 5, "F": 0}
    return round(sum(gp.get(letter_grade(m["percentage"]), 0) for m in marks_list) / len(marks_list), 2)


def log_activity(conn, user_id, event_type, detail, subject=""):
    conn.execute("INSERT INTO activity_log(student_id, event_type, subject, detail) VALUES (?,?,?,?)",
                 (user_id, event_type, subject, detail))


def get_activity_timeline(conn, user_id, limit=12):
    icons = {"topic_added": "📝", "task_done": "✅", "marks_saved": "📊", "plan_created": "📅",
             "goal_met": "🎯", "revision": "🔄", "note_saved": "📌"}
    tl = []
    for r in conn.execute("SELECT event_type, subject, detail, created_at FROM activity_log "
                          "WHERE student_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall():
        d = dict(r)
        d["icon"] = icons.get(d["event_type"], "•")
        d["time"] = d["created_at"][:16] if d["created_at"] else ""
        tl.append(d)
    return tl


def generate_smart_insights(conn, user_id, planner_data=None, marks_avg=0):
    insights = []
    pending = conn.execute("SELECT subject, COUNT(*) AS cnt FROM study_records WHERE student_id=? AND completed=0 "
                           "GROUP BY subject ORDER BY cnt DESC", (user_id,)).fetchall()
    if pending:
        t = pending[0]
        insights.append({"type": "priority", "icon": "📚", "title": f"Priority: {t['subject']}",
            "message": f"{t['cnt']} pending topic(s). Finish in Tasks.", "action": "tasks", "action_label": "Open Tasks"})
    if marks_avg > 0:
        for w in conn.execute("SELECT subject, AVG(percentage) AS avg FROM marks WHERE student_id=? "
                              "GROUP BY subject HAVING avg < ? ORDER BY avg ASC LIMIT 2",
                              (user_id, marks_avg)).fetchall():
            insights.append({"type": "marks", "icon": "📉", "title": f"{w['subject']} needs work",
                "message": f"At {round(w['avg'],1)}% vs your {marks_avg}% avg.", "action": "tracker", "action_label": "Study Tracker"})
    if planner_data:
        d = planner_data.get("days_left") or 0
        if d <= 14:
            insights.append({"type": "exam", "icon": "⏰",
                "title": f"{planner_data.get('subject_name') or 'Exam'}: {d} days",
                "message": f"Target {planner_data.get('today_topics',1)} topics, {planner_data.get('study_time',0)}h today.",
                "action": "planner", "action_label": "Planner"})
    today = datetime.today().strftime("%Y-%m-%d")
    if conn.execute("SELECT COUNT(*) AS c FROM study_records WHERE student_id=? AND study_date=?",
                    (user_id, today)).fetchone()["c"] == 0:
        u = get_user(conn, user_id)
        insights.append({"type": "habit", "icon": "🔥", "title": "Log today's study",
            "message": f"Streak: {u['streak']} days. Add topics in Tracker.", "action": "tracker", "action_label": "Tracker"})
    if not insights:
        insights.append({"type": "start", "icon": "💡", "title": "Get started",
            "message": "Add topics, marks, and a plan to unlock insights.", "action": "tracker", "action_label": "Tracker"})
    return insights[:6]


def build_subject_profiles(conn, user_id, ctx):
    hm = {h["subject"]: h for h in ctx["subject_health"]}
    rm = {r["subject"]: r for r in ctx["readiness_scores"]}
    mm = marks_by_subject(conn, user_id)
    rev = {}
    for r in ctx["revision_queue"]:
        rev[r["subject"]] = rev.get(r["subject"], 0) + 1
    notes = {n["subject"] for n in conn.execute("SELECT subject FROM subject_notes WHERE student_id=?",
                                                (user_id,)).fetchall()}
    pls = (ctx["planner_data"].get("subject_name") or "").strip().lower() if ctx.get("planner_data") else ""
    subs = set(mm.keys()) | {s["subject"] for s in subject_stats(conn, user_id)}
    profiles = []
    for subj in sorted(subs):
        st = next((s for s in subject_stats(conn, user_id) if s["subject"] == subj), {"total": 0, "done": 0})
        h, r = hm.get(subj, {}), rm.get(subj, {})
        if rev.get(subj, 0): nr, nl = "insights", f"Revise {rev[subj]} topic(s)"
        elif st["total"] - st["done"] > 0: nr, nl = "tasks", f"Complete {st['total']-st['done']} tasks"
        elif subj not in mm: nr, nl = "marks", "Enter marks"
        elif not pls: nr, nl = "planner", "Create plan"
        else: nr, nl = "tracker", "Add topics"
        profiles.append({"subject": subj, "total": st["total"], "done": st["done"],
            "pending": st["total"]-st["done"], "completion": int((st["done"]/st["total"])*100) if st["total"] else 0,
            "marks_pct": mm.get(subj), "grade": letter_grade(mm[subj]) if subj in mm else None,
            "health_score": h.get("score", 0), "health_status": h.get("status", "New"),
            "readiness": r.get("readiness", 0), "readiness_label": r.get("label", "—"),
            "revision_due": rev.get(subj, 0), "has_notes": subj in notes,
            "has_planner": pls == subj.lower() if pls else False, "next_route": nr, "next_label": nl})
    profiles.sort(key=lambda x: x["health_score"] if x["health_score"] else 101)
    return profiles


def compute_smartlearn_score(conn, user_id, ctx, user):
    _, _, progress = study_progress(conn, user_id)
    rev = len(ctx["revision_queue"])
    return min(100, int(progress * 0.25 + (min(100, ctx["marks_avg"]) * 0.25 if ctx["marks_avg"] else 0)
        + min(100, user["streak"] * 10) * 0.15 + ctx["daily_goal"]["percent"] * 0.15 + max(0, 20 - min(20, rev * 4))))


def build_learning_loop(conn, user_id, ctx):
    uid = user_id
    tc = conn.execute("SELECT COUNT(*) AS c FROM study_records WHERE student_id=?", (uid,)).fetchone()["c"]
    pend = conn.execute("SELECT COUNT(*) AS c FROM study_records WHERE student_id=? AND completed=0",
                        (uid,)).fetchone()["c"]
    mc = conn.execute("SELECT COUNT(*) AS c FROM marks WHERE student_id=?", (uid,)).fetchone()["c"]
    rev, hp = len(ctx["revision_queue"]), ctx["planner_data"] is not None
    def st(d, a): return "done" if d else ("active" if a else "pending")
    return [
        {"step": 1, "label": "Track", "route": "tracker", "icon": "📝", "status": st(tc > 0, tc == 0), "detail": f"{tc} topics"},
        {"step": 2, "label": "Tasks", "route": "tasks", "icon": "✅", "status": st(tc and not pend, pend > 0), "detail": f"{pend} pending"},
        {"step": 3, "label": "Plan", "route": "planner", "icon": "📅", "status": st(hp, tc and not hp), "detail": "Active" if hp else "Set plan"},
        {"step": 4, "label": "Marks", "route": "marks", "icon": "📊", "status": st(mc > 0, tc and not mc), "detail": f"{mc} entered"},
        {"step": 5, "label": "Revise", "route": "insights", "icon": "🔄", "status": st(rev == 0 and mc, rev > 0), "detail": f"{rev} due"},
    ]


def build_daily_briefing(conn, user_id, ctx):
    items = []
    dg = ctx["daily_goal"]
    items.append({"done": dg["met"], "text": f"Goal: {dg['done_today']}/{dg['target_topics']} topics",
        "route": "tasks" if not dg["met"] else "dashboard", "source": "Daily goal"})
    if ctx["priority_subject"]:
        p = ctx["priority_subject"]
        items.append({"done": False, "text": f"Priority: {p['subject']} ({p['pending']} pending)",
            "route": "tasks", "source": "Insights"})
    if ctx["planner_data"]:
        pl = ctx["planner_data"]
        items.append({"done": dg["done_today"] >= pl.get("today_topics", 1),
            "text": f"Planner: {pl.get('today_topics',1)} topics, {pl.get('study_time',0)}h",
            "route": "planner", "source": "Planner"})
    if ctx["revision_queue"]:
        items.append({"done": False, "text": f"Revise {len(ctx['revision_queue'])} old topic(s)",
            "route": "insights", "source": "Revision"})
    return items


def get_smartlearn_context(conn, user_id):
    user = get_user(conn, user_id)
    pr = conn.execute("SELECT *, CAST((julianday(exam_date)-julianday('now')) AS INTEGER) AS days_left "
                      "FROM planner WHERE student_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    planner_data = parse_plan_json(pr)
    mar = conn.execute("SELECT AVG(percentage) AS avg_pct FROM marks WHERE student_id=?", (user_id,)).fetchone()
    marks_avg = round(mar["avg_pct"], 1) if mar["avg_pct"] else 0
    base = {"planner_data": planner_data, "marks_avg": marks_avg,
            "subject_health": compute_subject_health(conn, user_id),
            "priority_subject": None, "activity_week": study_activity_week(conn, user_id),
            "velocity": learning_velocity(conn, user_id), "subjects": tracker_subjects(conn, user_id),
            "revision_queue": get_revision_queue(conn, user_id), "daily_goal": daily_goal_progress(conn, user_id),
            "readiness_scores": exam_readiness_scores(conn, user_id), "insights": []}
    if base["subject_health"]:
        base["priority_subject"] = base["subject_health"][0]
    base["insights"] = generate_smart_insights(conn, user_id, planner_data, marks_avg)
    base["subject_profiles"] = build_subject_profiles(conn, user_id, base)
    base["smartlearn_score"] = compute_smartlearn_score(conn, user_id, base, user)
    base["learning_loop"] = build_learning_loop(conn, user_id, base)
    base["daily_briefing"] = build_daily_briefing(conn, user_id, base)
    base["activity_timeline"] = get_activity_timeline(conn, user_id)
    base["pending_total"] = conn.execute("SELECT COUNT(*) AS c FROM study_records WHERE student_id=? AND completed=0",
                                           (user_id,)).fetchone()["c"]
    base["subject_summary"] = [{"subject": p["subject"], "total": p["total"], "done": p["done"],
        "pending": p["pending"], "marks_pct": p["marks_pct"]} for p in base["subject_profiles"]]
    return base


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        if not name or not email or not password:
            flash("All fields are required.", "error")
            return redirect(url_for("register"))

        conn = get_db()
        if conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
            conn.close()
            flash("Email already registered.", "error")
            return redirect(url_for("register"))

        conn.execute(
            "INSERT INTO users(name, email, password) VALUES (?, ?, ?)",
            (name, email, generate_password_hash(password)),
        )
        conn.commit()
        conn.close()
        flash("Registration successful. Please login.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE email=?", (email,)
        ).fetchone()
        conn.close()

        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["name"] = user["name"]
            flash(f"Welcome back, {user['name']}!", "success")
            return redirect(url_for("dashboard"))

        flash("Invalid email or password.", "error")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully.", "success")
    return redirect(url_for("home"))


@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()
    user = get_user(conn, session["user_id"])
    total, done, progress = study_progress(conn, session["user_id"])
    ctx = get_smartlearn_context(conn, session["user_id"])
    records = conn.execute("SELECT * FROM study_records WHERE student_id=? ORDER BY id DESC LIMIT 8",
                           (session["user_id"],)).fetchall()
    conn.close()

    return render_template("dashboard.html", name=session["name"], progress=progress,
        total_topics=total, completed_topics=done, xp=user["xp"], streak=user["streak"],
        records=records, planner_data=ctx["planner_data"], marks_avg=ctx["marks_avg"],
        pending_total=ctx["pending_total"], subject_summary=ctx["subject_summary"],
        subjects=ctx["subjects"], insights=ctx["insights"], subject_health=ctx["subject_health"],
        activity_week=ctx["activity_week"], velocity=ctx["velocity"],
        priority_subject=ctx["priority_subject"], revision_queue=ctx["revision_queue"],
        daily_goal=ctx["daily_goal"], smartlearn_score=ctx["smartlearn_score"],
        learning_loop=ctx["learning_loop"], daily_briefing=ctx["daily_briefing"],
        activity_timeline=ctx["activity_timeline"], page="dashboard")


@app.route("/tracker", methods=["GET", "POST"])
@login_required
def tracker():
    if request.method == "POST":
        subject = request.form["subject"].strip()
        study_date = request.form["date"]
        pairs = [("topic1", "priority1", "check1"), ("topic2", "priority2", "check2"),
                 ("topic3", "priority3", "check3"), ("topic4", "priority4", "check4")]
        conn = get_db()
        xp_gain, added = 0, 0
        for tk, pk, ck in pairs:
            topic = request.form.get(tk, "").strip()
            if not topic:
                continue
            completed = 1 if ck in request.form else 0
            priority = request.form.get(pk, "Medium")
            if priority not in ("Easy", "Medium", "Hard"):
                priority = "Medium"
            if completed:
                xp_gain += 10
            conn.execute("INSERT INTO study_records(student_id, subject, study_date, topic, completed, priority) "
                         "VALUES (?,?,?,?,?,?)", (session["user_id"], subject, study_date, topic, completed, priority))
            added += 1
        if added:
            conn.execute("UPDATE users SET xp=xp+?, streak=streak+1 WHERE id=?", (xp_gain, session["user_id"]))
            log_activity(conn, session["user_id"], "topic_added", f"Added {added} topic(s)", subject)
            conn.commit()
            flash(f"Saved {added} topic(s). Complete them in Tasks.", "success")
        else:
            flash("Add at least one topic.", "error")
        conn.close()
        return redirect(url_for("tracker"))

    prefill = request.args.get("subject", "")
    conn = get_db()
    ctx = get_smartlearn_context(conn, session["user_id"])
    conn.close()
    return render_template("tracker.html", page="tracker", prefill_subject=prefill,
        subjects=ctx["subjects"], subject_summary=ctx["subject_summary"])


@app.route("/tasks", methods=["GET", "POST"])
@login_required
def tasks():
    conn = get_db()

    if request.method == "POST":
        if request.form.get("action") == "revise":
            rid = int(request.form["record_id"])
            today = datetime.today().strftime("%Y-%m-%d")
            row = conn.execute("SELECT subject, topic FROM study_records WHERE id=?", (rid,)).fetchone()
            conn.execute("UPDATE study_records SET completed=0, study_date=? WHERE id=? AND student_id=?",
                         (today, rid, session["user_id"]))
            log_activity(conn, session["user_id"], "revision", f"Revising: {row['topic']}", row["subject"])
            conn.commit()
            flash("Moved to today for revision.", "success")
            conn.close()
            return redirect(url_for("tasks"))
        rid = int(request.form["record_id"])
        status = 1 if request.form.get("completed") == "on" else 0
        old = conn.execute("SELECT completed, subject, topic FROM study_records WHERE id=? AND student_id=?",
                           (rid, session["user_id"])).fetchone()
        conn.execute("UPDATE study_records SET completed=? WHERE id=? AND student_id=?",
                     (status, rid, session["user_id"]))
        if old and old["completed"] == 0 and status == 1:
            conn.execute("UPDATE users SET xp=xp+5 WHERE id=?", (session["user_id"],))
            log_activity(conn, session["user_id"], "task_done", f"Done: {old['topic']}", old["subject"])
            if daily_goal_progress(conn, session["user_id"])["met"]:
                log_activity(conn, session["user_id"], "goal_met", "Daily goal met", "")
                flash("Daily goal achieved!", "success")
            flash("+5 XP!", "success")
        conn.commit()

    records = conn.execute("SELECT * FROM study_records WHERE student_id=? ORDER BY study_date DESC, id DESC",
                           (session["user_id"],)).fetchall()
    ctx = get_smartlearn_context(conn, session["user_id"])
    conn.close()
    return render_template("tasks.html", records=records, revision_queue=ctx["revision_queue"],
        pending_total=ctx["pending_total"], planner_data=ctx["planner_data"], page="tasks")


@app.route("/planner", methods=["GET", "POST"])
@login_required
def planner():
    conn = get_db()
    warning = None
    subjects = tracker_subjects(conn, session["user_id"])
    auto_completed = tracker_completed_count(conn, session["user_id"])

    if request.method == "POST":
        subject_name = request.form.get("subject_name", "").strip()
        total_topics = int(request.form["total_topics"])
        completed_topics = int(request.form.get("completed_topics", 0))
        exam_date = request.form["exam_date"]
        difficulty = request.form["difficulty"]
        confidence = request.form.get("confidence", "Medium")
        pace = request.form.get("pace", "Steady")
        revision_days = int(request.form.get("revision_days", 3))
        study_days_per_week = int(request.form.get("study_days_per_week", 6))
        free_hours = float(request.form["free_hours"])
        if request.form.get("sync_tracker") == "on":
            completed_topics = tracker_completed_count(conn, session["user_id"], subject_name or None)
        if completed_topics > total_topics:
            flash("Completed cannot exceed total.", "error")
            conn.close()
            return redirect(url_for("planner"))
        insight = build_study_plan(total_topics, completed_topics, exam_date, difficulty,
            confidence, free_hours, revision_days, study_days_per_week, pace)
        if insight["study_time"] > free_hours:
            warning = f"Needs {insight['study_time']} hrs/day; you have {free_hours}."
        conn.execute("""INSERT INTO planner(student_id, subject_name, total_topics, completed_topics,
            exam_date, difficulty, confidence, pace, revision_days, study_days_per_week, free_hours,
            study_time, learning_hours, revision_hours, topics_per_day, weekly_hours, today_topics,
            risk_status, plan_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session["user_id"], subject_name, total_topics, completed_topics, exam_date, difficulty,
             confidence, pace, revision_days, study_days_per_week, free_hours, insight["study_time"],
             insight["learning_hours"], insight["revision_hours"], insight["topics_per_day"],
             insight["weekly_hours"], insight["today_topics"], insight["risk_status"], json.dumps(insight)))
        log_activity(conn, session["user_id"], "plan_created", f"Exam {exam_date}", subject_name or "General")
        conn.commit()
        flash("Plan saved. Visible on Dashboard & Insights.", "success")

    planner_row = conn.execute("SELECT *, CAST((julianday(exam_date)-julianday('now')) AS INTEGER) AS days_left "
                               "FROM planner WHERE student_id=? ORDER BY id DESC LIMIT 1",
                               (session["user_id"],)).fetchone()
    planner_data = parse_plan_json(planner_row)
    history = conn.execute("SELECT id, subject_name, exam_date, study_time, risk_status, created_at FROM planner "
                           "WHERE student_id=? ORDER BY id DESC LIMIT 5", (session["user_id"],)).fetchall()
    ctx = get_smartlearn_context(conn, session["user_id"])
    linked = None
    if planner_data and planner_data.get("subject_name"):
        for p in ctx["subject_profiles"]:
            if p["subject"].lower() == planner_data["subject_name"].lower():
                linked = p
                break
    conn.close()
    return render_template("planner.html", planner_data=planner_data, warning=warning, subjects=subjects,
        auto_completed=auto_completed, history=history, linked_profile=linked, page="planner")


@app.route("/insights", methods=["GET", "POST"])
@login_required
def insights():
    conn = get_db()
    if request.method == "POST":
        ft = request.form.get("form_type")
        if ft == "goals":
            conn.execute("""INSERT INTO study_goals(student_id, daily_topics, daily_hours, updated_at)
                VALUES (?,?,?,datetime('now')) ON CONFLICT(student_id) DO UPDATE SET
                daily_topics=excluded.daily_topics, daily_hours=excluded.daily_hours""",
                (session["user_id"], int(request.form.get("daily_topics", 3)),
                 float(request.form.get("daily_hours", 2))))
            conn.commit()
            flash("Daily goal updated.", "success")
        elif ft == "notes":
            subj = request.form.get("subject", "").strip()
            note = request.form.get("note_text", "").strip()
            if subj and note:
                conn.execute("""INSERT INTO subject_notes(student_id, subject, note_text, updated_at)
                    VALUES (?,?,?,datetime('now')) ON CONFLICT(student_id, subject) DO UPDATE SET
                    note_text=excluded.note_text""", (session["user_id"], subj, note))
                log_activity(conn, session["user_id"], "note_saved", "Notes updated", subj)
                conn.commit()
                flash(f"Notes saved for {subj}.", "success")
        conn.close()
        return redirect(url_for("insights"))

    ctx = get_smartlearn_context(conn, session["user_id"])
    total, done, progress = study_progress(conn, session["user_id"])
    goal = get_daily_goal(conn, session["user_id"])
    notes_rows = conn.execute("SELECT subject, note_text, updated_at FROM subject_notes WHERE student_id=? ORDER BY subject",
                              (session["user_id"],)).fetchall()
    conn.close()
    return render_template("insights.html", name=session["name"], progress=progress,
        completed_topics=done, total_topics=total, marks_avg=ctx["marks_avg"], planner_data=ctx["planner_data"],
        insights=ctx["insights"], subject_health=ctx["subject_health"], activity_week=ctx["activity_week"],
        velocity=ctx["velocity"], priority_subject=ctx["priority_subject"],
        readiness_scores=ctx["readiness_scores"], revision_queue=ctx["revision_queue"],
        daily_goal=ctx["daily_goal"], goal=goal, notes_rows=notes_rows, subjects=ctx["subjects"],
        smartlearn_score=ctx["smartlearn_score"], learning_loop=ctx["learning_loop"],
        activity_timeline=ctx["activity_timeline"], subject_profiles=ctx["subject_profiles"], page="insights")


@app.route("/marks", methods=["GET", "POST"])
@login_required
def marks():
    conn = get_db()
    if request.method == "POST":
        semester = request.form["semester"]
        subject = request.form["subject"].strip()
        obtained = int(request.form["marks"])
        total = int(request.form["total_marks"])
        if total <= 0 or obtained < 0 or obtained > total:
            flash("Invalid marks.", "error")
        else:
            pct = round((obtained / total) * 100, 2)
            conn.execute("INSERT INTO marks(student_id, semester, subject, marks, total_marks, percentage) "
                         "VALUES (?,?,?,?,?,?)", (session["user_id"], semester, subject, obtained, total, pct))
            log_activity(conn, session["user_id"], "marks_saved", f"{pct}%", subject)
            conn.commit()
            flash("Marks saved.", "success")
    marks_data = marks_with_grades(conn, session["user_id"])
    avg_row = conn.execute("SELECT AVG(percentage) AS avg_pct FROM marks WHERE student_id=?",
                           (session["user_id"],)).fetchone()
    ctx = get_smartlearn_context(conn, session["user_id"])
    conn.close()
    return render_template("marks.html", marks_data=marks_data,
        avg_pct=round(avg_row["avg_pct"], 1) if avg_row["avg_pct"] else 0, sgpa=calc_sgpa(marks_data),
        readiness_scores=ctx["readiness_scores"], subject_profiles=ctx["subject_profiles"],
        subjects=ctx["subjects"], planner_data=ctx["planner_data"], letter_grade=letter_grade, page="marks")


if __name__ == "__main__":
    app.run(debug=True)