import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, render_template, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cloud_monitor")

app = Flask(__name__)

# Configuration from environment variables
DEFAULT_DB_PATH = os.environ.get("DB_PATH", "monitoring.db")
DEFAULT_TARGET_NAME = os.environ.get("TARGET_NAME", "Empty Classroom Finder")
DEFAULT_TARGET_URL = os.environ.get(
    "TARGET_URL", "https://emptyclassroomfinder-thi7.onrender.com/"
)
DEFAULT_CHECK_INTERVAL = max(10, int(os.environ.get("CHECK_INTERVAL_SECONDS", "60")))
DEFAULT_GITHUB_REPO = os.environ.get("GITHUB_REPO", "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
HIGH_LATENCY_MS = float(os.environ.get("HIGH_LATENCY_MS", "3000"))
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "15"))

app.config["DB_PATH"] = DEFAULT_DB_PATH

_monitor_thread_started = False
_monitor_lock = threading.Lock()


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_db_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Create a SQLite connection with row factory enabled."""
    path = db_path or app.config.get("DB_PATH", DEFAULT_DB_PATH)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    """Initialize SQLite tables and seed the default target monitor if empty."""
    with get_db_connection(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                github_repo TEXT DEFAULT '',
                check_interval INTEGER NOT NULL DEFAULT 60,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS health_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('UP', 'DOWN')),
                status_code INTEGER,
                response_time_ms REAL,
                error_message TEXT DEFAULT '',
                checked_at TEXT NOT NULL,
                FOREIGN KEY (monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_health_checks_monitor_time
                ON health_checks(monitor_id, checked_at DESC);

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER NOT NULL,
                alert_type TEXT NOT NULL CHECK(alert_type IN ('DOWNTIME', 'RECOVERY', 'HIGH_LATENCY')),
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_alerts_monitor_time
                ON alerts(monitor_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS workflow_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER NOT NULL,
                run_id INTEGER NOT NULL,
                repo TEXT NOT NULL,
                workflow_name TEXT NOT NULL,
                run_number INTEGER,
                status TEXT NOT NULL,
                conclusion TEXT,
                branch TEXT DEFAULT '',
                event TEXT DEFAULT '',
                html_url TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                UNIQUE(monitor_id, run_id),
                FOREIGN KEY (monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
            );
            """
        )

        existing = conn.execute("SELECT COUNT(*) FROM monitors").fetchone()[0]
        if existing == 0:
            # First run: seed the default monitor
            conn.execute(
                """
                INSERT INTO monitors (name, url, github_repo, check_interval, is_active, created_at)
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                (
                    DEFAULT_TARGET_NAME,
                    DEFAULT_TARGET_URL,
                    DEFAULT_GITHUB_REPO,
                    DEFAULT_CHECK_INTERVAL,
                    utc_now_iso(),
                ),
            )
        else:
            # Always keep the primary (first) monitor in sync with the current
            # TARGET_URL / TARGET_NAME config so changing the env-var or the
            # hardcoded default takes effect immediately on the next restart
            # without having to wipe the database manually.
            conn.execute(
                """
                UPDATE monitors
                SET name = ?, url = ?
                WHERE id = (SELECT MIN(id) FROM monitors)
                """,
                (DEFAULT_TARGET_NAME, DEFAULT_TARGET_URL),
            )
            logger.info(
                "Primary monitor synced to: %s (%s)",
                DEFAULT_TARGET_NAME,
                DEFAULT_TARGET_URL,
            )
        conn.commit()


def is_valid_http_url(url: str) -> bool:
    """Validate that the URL uses http or https and includes a network host."""
    try:
        parsed = urlparse(url.strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def send_webhook_notification(title: str, message: str, status: str) -> None:
    """Send an optional webhook alert (compatible with Slack/Discord/generic JSON)."""
    if not ALERT_WEBHOOK_URL:
        return
    payload = {
        "text": f"[{status}] {title}: {message}",
        "content": f"**[{status}] {title}**\n{message}",
        "status": status,
        "title": title,
        "message": message,
        "timestamp": utc_now_iso(),
    }
    try:
        requests.post(ALERT_WEBHOOK_URL, json=payload, timeout=5)
    except requests.RequestException as exc:
        logger.warning("Failed to send webhook notification: %s", exc)


def record_alert(
    conn: sqlite3.Connection,
    monitor_id: int,
    monitor_name: str,
    alert_type: str,
    message: str,
) -> None:
    """Insert an alert into SQLite and trigger webhook notification if configured."""
    conn.execute(
        """
        INSERT INTO alerts (monitor_id, alert_type, message, created_at, acknowledged)
        VALUES (?, ?, ?, ?, 0)
        """,
        (monitor_id, alert_type, message, utc_now_iso()),
    )
    send_webhook_notification(monitor_name, message, alert_type)


def perform_health_check(
    monitor_id: int, db_path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Send an HTTP GET health check to the target monitor and store results."""
    with get_db_connection(db_path) as conn:
        monitor = conn.execute(
            "SELECT * FROM monitors WHERE id = ?", (monitor_id,)
        ).fetchone()
        if not monitor:
            return None

        url = monitor["url"]
        name = monitor["name"]

        prev_check = conn.execute(
            """
            SELECT status FROM health_checks
            WHERE monitor_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (monitor_id,),
        ).fetchone()
        prev_status = prev_check["status"] if prev_check else None

    status = "DOWN"
    status_code: Optional[int] = None
    response_time_ms: Optional[float] = None
    error_message = ""

    start_time = time.perf_counter()
    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": "CloudAppMonitor/1.0"},
            allow_redirects=True,
        )
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
        response_time_ms = elapsed_ms
        status_code = resp.status_code

        if 200 <= resp.status_code < 400:
            status = "UP"
        else:
            status = "DOWN"
            error_message = f"HTTP {resp.status_code} {resp.reason or ''}".strip()
    except requests.Timeout:
        response_time_ms = round((time.perf_counter() - start_time) * 1000, 2)
        error_message = f"Request timed out after {REQUEST_TIMEOUT_SECONDS}s"
    except requests.ConnectionError as exc:
        response_time_ms = round((time.perf_counter() - start_time) * 1000, 2)
        error_message = f"Connection error: {exc}"
    except requests.RequestException as exc:
        response_time_ms = round((time.perf_counter() - start_time) * 1000, 2)
        error_message = f"Request failed: {exc}"

    checked_at = utc_now_iso()

    with get_db_connection(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO health_checks
                (monitor_id, status, status_code, response_time_ms, error_message, checked_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (monitor_id, status, status_code, response_time_ms, error_message, checked_at),
        )
        check_id = cursor.lastrowid

        # Generate state-transition and latency alerts
        if status == "DOWN" and prev_status != "DOWN":
            record_alert(
                conn,
                monitor_id,
                name,
                "DOWNTIME",
                f"{name} ({url}) is DOWN. {error_message}",
            )
        elif status == "UP" and prev_status == "DOWN":
            record_alert(
                conn,
                monitor_id,
                name,
                "RECOVERY",
                f"{name} ({url}) is back ONLINE (HTTP {status_code}, {response_time_ms} ms).",
            )
        elif (
            status == "UP"
            and response_time_ms is not None
            and response_time_ms > HIGH_LATENCY_MS
        ):
            record_alert(
                conn,
                monitor_id,
                name,
                "HIGH_LATENCY",
                f"{name} responded slowly in {response_time_ms} ms (threshold: {HIGH_LATENCY_MS} ms).",
            )

        conn.commit()

    return {
        "id": check_id,
        "monitor_id": monitor_id,
        "status": status,
        "status_code": status_code,
        "response_time_ms": response_time_ms,
        "error_message": error_message,
        "checked_at": checked_at,
    }


def sync_github_workflows(
    monitor_id: int, db_path: Optional[str] = None
) -> Dict[str, Any]:
    """Fetch recent GitHub Actions workflow runs for the monitor's configured repo."""
    with get_db_connection(db_path) as conn:
        monitor = conn.execute(
            "SELECT id, github_repo FROM monitors WHERE id = ?", (monitor_id,)
        ).fetchone()
        if not monitor:
            return {"ok": False, "error": "Monitor not found", "runs": []}
        repo = (monitor["github_repo"] or "").strip()

    if not repo or "/" not in repo:
        return {
            "ok": False,
            "error": "No GitHub repository configured (expected owner/repo).",
            "runs": [],
        }

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "CloudAppMonitor/1.0",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    api_url = f"https://api.github.com/repos/{repo}/actions/runs?per_page=10"
    try:
        resp = requests.get(api_url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return {
                "ok": False,
                "error": f"GitHub API returned HTTP {resp.status_code}",
                "runs": [],
            }
        data = resp.json()
    except requests.RequestException as exc:
        return {"ok": False, "error": f"GitHub API request failed: {exc}", "runs": []}

    runs = data.get("workflow_runs", [])
    fetched_at = utc_now_iso()

    with get_db_connection(db_path) as conn:
        for run in runs:
            conn.execute(
                """
                INSERT INTO workflow_runs
                    (monitor_id, run_id, repo, workflow_name, run_number, status,
                     conclusion, branch, event, html_url, created_at, updated_at, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(monitor_id, run_id) DO UPDATE SET
                    workflow_name = excluded.workflow_name,
                    status = excluded.status,
                    conclusion = excluded.conclusion,
                    updated_at = excluded.updated_at,
                    fetched_at = excluded.fetched_at
                """,
                (
                    monitor_id,
                    run.get("id"),
                    repo,
                    run.get("name") or "Workflow",
                    run.get("run_number"),
                    run.get("status") or "unknown",
                    run.get("conclusion") or "pending",
                    run.get("head_branch") or "",
                    run.get("event") or "",
                    run.get("html_url") or "",
                    run.get("created_at") or fetched_at,
                    run.get("updated_at") or fetched_at,
                    fetched_at,
                ),
            )
        conn.commit()

    return {"ok": True, "count": len(runs)}


def background_monitor_loop() -> None:
    """Periodically check active monitors according to their check_interval."""
    logger.info("Background monitoring loop started.")
    last_checked: Dict[int, float] = {}
    last_workflow_sync: Dict[int, float] = {}

    while True:
        try:
            with get_db_connection() as conn:
                monitors = conn.execute(
                    "SELECT id, check_interval, github_repo FROM monitors WHERE is_active = 1"
                ).fetchall()

            now = time.monotonic()
            for m in monitors:
                mid = m["id"]
                interval = max(10, int(m["check_interval"]))
                if now - last_checked.get(mid, 0) >= interval:
                    last_checked[mid] = now
                    perform_health_check(mid)

                repo = (m["github_repo"] or "").strip()
                if repo and (now - last_workflow_sync.get(mid, 0) >= 300):
                    last_workflow_sync[mid] = now
                    sync_github_workflows(mid)
        except Exception as exc:
            logger.exception("Error in background monitoring loop: %s", exc)

        time.sleep(5)


def start_background_monitor() -> None:
    """Start the background monitoring daemon thread once."""
    global _monitor_thread_started
    if os.environ.get("ENABLE_BACKGROUND_MONITOR", "true").lower() == "false":
        return
    # Avoid double-starting when Flask's Werkzeug reloader spawns a child process
    if os.environ.get("FLASK_DEBUG") == "1" and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return

    with _monitor_lock:
        if not _monitor_thread_started:
            t = threading.Thread(target=background_monitor_loop, daemon=True)
            t.start()
            _monitor_thread_started = True


# Initialize DB and start background monitor on module import (supports gunicorn)
init_db()
start_background_monitor()


# ---------------------------------------------------------------------------
# Web & API Routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    """Render the main monitoring dashboard."""
    return render_template("index.html")


@app.route("/health")
def health():
    """Health check endpoint for Render platform monitoring."""
    return jsonify({"status": "ok", "timestamp": utc_now_iso()})


@app.route("/api/monitors", methods=["GET"])
def list_monitors():
    """Return all configured monitors."""
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM monitors ORDER BY id ASC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/monitors", methods=["POST"])
def create_monitor():
    """Create a new monitored target."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    url = (data.get("url") or "").strip()
    github_repo = (data.get("github_repo") or "").strip()
    check_interval = max(10, int(data.get("check_interval") or 60))

    if not name or not is_valid_http_url(url):
        return jsonify({"error": "Valid name and http/https URL are required."}), 400

    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO monitors (name, url, github_repo, check_interval, is_active, created_at)
            VALUES (?, ?, ?, ?, 1, ?)
            """,
            (name, url, github_repo, check_interval, utc_now_iso()),
        )
        monitor_id = cursor.lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,)).fetchone()

    return jsonify(dict(row)), 201


@app.route("/api/monitors/<int:monitor_id>", methods=["PUT"])
def update_monitor(monitor_id: int):
    """Update an existing monitor's configuration."""
    data = request.get_json(silent=True) or {}
    with get_db_connection() as conn:
        existing = conn.execute(
            "SELECT * FROM monitors WHERE id = ?", (monitor_id,)
        ).fetchone()
        if not existing:
            return jsonify({"error": "Monitor not found."}), 404

        name = (data.get("name") or existing["name"]).strip()
        url = (data.get("url") or existing["url"]).strip()
        github_repo = (
            data["github_repo"].strip()
            if "github_repo" in data
            else existing["github_repo"]
        )
        check_interval = max(
            10, int(data.get("check_interval") or existing["check_interval"])
        )
        is_active = (
            int(bool(data["is_active"]))
            if "is_active" in data
            else existing["is_active"]
        )

        if not name or not is_valid_http_url(url):
            return jsonify({"error": "Valid name and http/https URL are required."}), 400

        conn.execute(
            """
            UPDATE monitors
            SET name = ?, url = ?, github_repo = ?, check_interval = ?, is_active = ?
            WHERE id = ?
            """,
            (name, url, github_repo, check_interval, is_active, monitor_id),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM monitors WHERE id = ?", (monitor_id,)
        ).fetchone()

    return jsonify(dict(updated))


@app.route("/api/monitors/<int:monitor_id>", methods=["DELETE"])
def delete_monitor(monitor_id: int):
    """Delete a monitor if more than one exists."""
    with get_db_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM monitors").fetchone()[0]
        if count <= 1:
            return jsonify({"error": "Cannot delete the last remaining monitor."}), 400
        conn.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
        conn.commit()
    return jsonify({"deleted": True, "id": monitor_id})


@app.route("/api/check-now", methods=["POST"])
def check_now():
    """Trigger an immediate health check for the specified monitor."""
    data = request.get_json(silent=True) or {}
    monitor_id = data.get("monitor_id")

    with get_db_connection() as conn:
        if not monitor_id:
            first = conn.execute("SELECT id FROM monitors ORDER BY id ASC LIMIT 1").fetchone()
            monitor_id = first["id"] if first else None

    if not monitor_id:
        return jsonify({"error": "No monitor configured."}), 404

    result = perform_health_check(int(monitor_id))
    if not result:
        return jsonify({"error": "Monitor not found."}), 404
    return jsonify(result)


@app.route("/api/workflows/sync", methods=["POST"])
def sync_workflows_route():
    """Manually trigger GitHub Actions workflow sync for a monitor."""
    data = request.get_json(silent=True) or {}
    monitor_id = data.get("monitor_id")
    if not monitor_id:
        return jsonify({"error": "monitor_id is required."}), 400

    res = sync_github_workflows(int(monitor_id))
    status_code = 200 if res.get("ok") else 400
    return jsonify(res), status_code


@app.route("/api/alerts/<int:alert_id>/ack", methods=["POST"])
def acknowledge_alert(alert_id: int):
    """Mark an alert as acknowledged."""
    with get_db_connection() as conn:
        conn.execute("UPDATE alerts SET acknowledged = 1 WHERE id = ?", (alert_id,))
        conn.commit()
    return jsonify({"acknowledged": True, "id": alert_id})


@app.route("/api/dashboard", methods=["GET"])
def get_dashboard_data():
    """Return aggregated metrics, history, errors, alerts, and workflows."""
    monitor_id = request.args.get("monitor_id", type=int)
    limit = min(200, max(10, request.args.get("limit", default=50, type=int)))

    with get_db_connection() as conn:
        monitors = [
            dict(r)
            for r in conn.execute("SELECT * FROM monitors ORDER BY id ASC").fetchall()
        ]
        if not monitors:
            return jsonify({"error": "No monitors available."}), 404

        active_monitor = next(
            (m for m in monitors if m["id"] == monitor_id), monitors[0]
        )
        mid = active_monitor["id"]

        stats_row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_checks,
                SUM(CASE WHEN status = 'UP' THEN 1 ELSE 0 END) AS successful_checks,
                SUM(CASE WHEN status = 'DOWN' THEN 1 ELSE 0 END) AS failed_checks,
                AVG(CASE WHEN response_time_ms IS NOT NULL THEN response_time_ms END) AS avg_response_time,
                MIN(CASE WHEN response_time_ms IS NOT NULL THEN response_time_ms END) AS min_response_time,
                MAX(CASE WHEN response_time_ms IS NOT NULL THEN response_time_ms END) AS max_response_time
            FROM health_checks
            WHERE monitor_id = ?
            """,
            (mid,),
        ).fetchone()

        latest_check = conn.execute(
            """
            SELECT * FROM health_checks
            WHERE monitor_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (mid,),
        ).fetchone()

        recent_checks = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM health_checks
                WHERE monitor_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (mid, limit),
            ).fetchall()
        ]

        error_history = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM health_checks
                WHERE monitor_id = ? AND (status = 'DOWN' OR status_code >= 400)
                ORDER BY id DESC LIMIT 25
                """,
                (mid,),
            ).fetchall()
        ]

        alerts = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM alerts
                WHERE monitor_id = ?
                ORDER BY id DESC LIMIT 25
                """,
                (mid,),
            ).fetchall()
        ]

        workflows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM workflow_runs
                WHERE monitor_id = ?
                ORDER BY run_id DESC LIMIT 10
                """,
                (mid,),
            ).fetchall()
        ]

    total = stats_row["total_checks"] or 0
    success = stats_row["successful_checks"] or 0
    failed = stats_row["failed_checks"] or 0
    uptime_pct = round((success / total) * 100, 2) if total > 0 else 0.0

    summary = {
        "current_status": latest_check["status"] if latest_check else "PENDING",
        "last_status_code": latest_check["status_code"] if latest_check else None,
        "last_response_time_ms": (
            latest_check["response_time_ms"] if latest_check else None
        ),
        "last_checked_at": latest_check["checked_at"] if latest_check else None,
        "uptime_percentage": uptime_pct,
        "avg_response_time_ms": (
            round(stats_row["avg_response_time"], 2)
            if stats_row["avg_response_time"] is not None
            else 0.0
        ),
        "min_response_time_ms": (
            round(stats_row["min_response_time"], 2)
            if stats_row["min_response_time"] is not None
            else 0.0
        ),
        "max_response_time_ms": (
            round(stats_row["max_response_time"], 2)
            if stats_row["max_response_time"] is not None
            else 0.0
        ),
        "total_checks": total,
        "successful_checks": success,
        "failed_checks": failed,
    }

    return jsonify(
        {
            "monitors": monitors,
            "active_monitor": active_monitor,
            "summary": summary,
            "recent_checks": recent_checks,
            "error_history": error_history,
            "alerts": alerts,
            "workflows": workflows,
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
