"""
Local-only Flask dashboard for the Mback.py IMAP email backup project.

Security design:
- Binds only to 127.0.0.1 with debug disabled.
- Never reads, parses, serves, or logs config.json.
- Reads backup_state.db only through read-only SQLite connections.
- Runs Mback.py only through one fixed subprocess argument list.
- Does not expose raw backup-script stdout/stderr in the browser.
- Serves .eml files only after POST + CSRF + database lookup + path containment checks.
- Uses fictional demo data only when DEMO_MODE=True.
"""

import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file, session


# ================= CONFIGURATION =================

BASE_DIR = Path(__file__).resolve().parent

MBACK_SCRIPT = BASE_DIR / "Mback.py"
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "backup_state.db"
BACKUPS_DIR = BASE_DIR / "backups"
DEMO_DATA_PATH = BASE_DIR / "static" / "demo_data.json"

DEMO_MODE = False

HOST = "127.0.0.1"
PORT = 5000
DEBUG = False

RECENT_SNAPSHOT_LIMIT = 8
ALLOWED_PAGE_SIZES = (25, 50, 100)
DEFAULT_PAGE_SIZE = 25
MAX_CAPTURED_OUTPUT_CHARS = 8000
MAX_UI_ERROR_CHARS = 240
RUN_TIMEOUT_SECONDS = 30 * 60

VALID_STATUSES = ("backed_up", "failed")
SEARCH_ALLOWED_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,120}$")
FOLDER_ALLOWED_RE = re.compile(r"^[A-Za-z0-9 ._\-/]{1,150}$")

NAV_ITEMS = [
    ("dashboard", "Overview"),
    ("records", "Backup Records"),
    ("how_it_works", "How It Works"),
    ("security", "Security & Privacy"),
    ("faq", "FAQ"),
    ("about", "About Project"),
    ("demo_notice", "Demo Notice"),
]


# ================= FLASK APP =================

app = Flask(
    __name__,
    static_folder=str(BASE_DIR / "static"),
    template_folder=str(BASE_DIR / "templates"),
)

_env_secret = os.environ.get("MBACK_DASHBOARD_SECRET")

if _env_secret:
    app.secret_key = _env_secret
else:
    app.secret_key = secrets.token_hex(32)
    print(
        "WARNING: MBACK_DASHBOARD_SECRET is not set. "
        "Using a temporary random session secret for this run only.",
        file=sys.stderr,
    )

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,
)


# ================= RESPONSE SECURITY HEADERS =================

CSP = (
    "default-src 'self'; "
    "style-src 'self'; "
    "script-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


@app.after_request
def add_security_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = CSP
    return response


@app.errorhandler(400)
def handle_bad_request(error):
    return jsonify(error="The request was invalid."), 400


@app.errorhandler(403)
def handle_forbidden(error):
    return jsonify(error="This action is not permitted."), 403


@app.errorhandler(404)
def handle_not_found(error):
    return jsonify(error="The requested resource was not found."), 404


@app.errorhandler(500)
def handle_server_error(error):
    return jsonify(error="An unexpected local dashboard error occurred."), 500


# ================= BACKUP RUN STATE =================

run_lock = threading.RLock()

run_state = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "returncode": None,
    "saved": None,
    "failed": None,
    "error": None,
    "log_tail": "",
}

SUMMARY_RE = re.compile(
    r"Backup completed\.\s*Saved:\s*(\d+),\s*Failed:\s*(\d+)"
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _public_run_state():
    """Return browser-safe run data only. Never include raw script output."""
    with run_lock:
        return {
            "status": run_state["status"],
            "started_at": run_state["started_at"],
            "finished_at": run_state["finished_at"],
            "returncode": run_state["returncode"],
            "saved": run_state["saved"],
            "failed": run_state["failed"],
            "error": run_state["error"],
        }


def _set_run_failed(message):
    with run_lock:
        run_state.update(
            status="failed",
            finished_at=_now_iso(),
            returncode=None,
            saved=None,
            failed=None,
            error=message,
            log_tail="",
        )


def run_mback_command():
    """
    Start Mback.py with a fixed command only.

    Browser input never controls executable paths, script paths, config paths,
    arguments, or shell text.
    """
    if not MBACK_SCRIPT.is_file():
        _set_run_failed("Backup script is unavailable. Check the local project setup.")
        return

    if not CONFIG_PATH.is_file():
        _set_run_failed("Backup configuration is unavailable. Check the local project setup.")
        return

    command = [
        sys.executable,
        str(MBACK_SCRIPT),
        str(CONFIG_PATH),
    ]

    try:
        process = subprocess.run(
            command,
            cwd=str(BASE_DIR),
            shell=False,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SECONDS,
        )

        stdout = process.stdout or ""
        stderr = process.stderr or ""

        summary_match = SUMMARY_RE.search(stdout)

        saved = (
            int(summary_match.group(1))
            if summary_match
            else None
        )

        failed = (
            int(summary_match.group(2))
            if summary_match
            else None
        )

        combined_output = stdout

        if stderr.strip():
            combined_output += "\n--- stderr ---\n" + stderr

        with run_lock:
            run_state.update(
                status="success" if process.returncode == 0 else "failed",
                finished_at=_now_iso(),
                returncode=process.returncode,
                saved=saved,
                failed=failed,
                error=(
                    None
                    if process.returncode == 0
                    else "Mback.py did not complete successfully. Check the local backup log."
                ),
                log_tail=combined_output[-MAX_CAPTURED_OUTPUT_CHARS:],
            )

    except subprocess.TimeoutExpired:
        _set_run_failed(
            f"Backup timed out after {RUN_TIMEOUT_SECONDS} seconds."
        )

    except FileNotFoundError:
        _set_run_failed(
            "Python or the Mback backup script could not be found."
        )

    except Exception:
        _set_run_failed(
            "Backup failed to start. Check the local backup log."
        )


# ================= DATABASE HELPERS =================

def _get_db_readonly():
    """Open SQLite database strictly read-only."""
    if not DB_PATH.is_file():
        return None

    database_uri = f"file:{DB_PATH.as_posix()}?mode=ro"

    try:
        connection = sqlite3.connect(
            database_uri,
            uri=True,
            timeout=5,
        )
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error:
        return None


def _escape_like(value):
    return (
        value
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _safe_error_for_ui(value):
    if not value:
        return None

    text = str(value).replace("\r", " ").replace("\n", " ").strip()

    if len(text) > MAX_UI_ERROR_CHARS:
        return text[:MAX_UI_ERROR_CHARS] + "…"

    return text


def _row_to_record(row):
    filename = Path(row["filepath"]).name if row["filepath"] else None

    return {
        "folder": row["folder"],
        "uidvalidity": row["uidvalidity"],
        "uid": row["uid"],
        "status": row["status"],
        "attempts": row["attempts"],
        "filename": filename,
        "updated_at": row["updated_at"],
    }


class SqliteDataSource:
    """Read-only source backed by the real backup_state.db."""

    def available(self):
        return DB_PATH.is_file()

    def status_counts(self):
        connection = _get_db_readonly()

        if connection is None:
            return {"backed_up": 0, "failed": 0}

        try:
            backed_up = connection.execute(
                "SELECT COUNT(*) FROM message_state "
                "WHERE status = 'backed_up'"
            ).fetchone()[0]

            failed = connection.execute(
                "SELECT COUNT(*) FROM message_state "
                "WHERE status = 'failed'"
            ).fetchone()[0]

            return {
                "backed_up": backed_up,
                "failed": failed,
            }
        except sqlite3.Error:
            return {"backed_up": 0, "failed": 0}
        finally:
            connection.close()

    def folder_summary(self):
        connection = _get_db_readonly()

        if connection is None:
            return []

        try:
            rows = connection.execute(
                "SELECT folder, uidvalidity, last_uid, updated_at "
                "FROM folder_state ORDER BY folder"
            ).fetchall()

            summary = []

            for row in rows:
                backed_up = connection.execute(
                    "SELECT COUNT(*) FROM message_state "
                    "WHERE folder = ? AND status = 'backed_up'",
                    (row["folder"],),
                ).fetchone()[0]

                failed = connection.execute(
                    "SELECT COUNT(*) FROM message_state "
                    "WHERE folder = ? AND status = 'failed'",
                    (row["folder"],),
                ).fetchone()[0]

                summary.append(
                    {
                        "folder": row["folder"],
                        "uidvalidity": row["uidvalidity"],
                        "last_uid": row["last_uid"],
                        "updated_at": row["updated_at"],
                        "backed_up": backed_up,
                        "failed": failed,
                    }
                )

            return summary

        except sqlite3.Error:
            return []

        finally:
            connection.close()

    def latest_success_at(self):
        connection = _get_db_readonly()

        if connection is None:
            return None

        try:
            row = connection.execute(
                "SELECT MAX(updated_at) FROM message_state "
                "WHERE status = 'backed_up'"
            ).fetchone()

            return row[0] if row else None

        except sqlite3.Error:
            return None

        finally:
            connection.close()

    def available_folders(self):
        connection = _get_db_readonly()

        if connection is None:
            return []

        try:
            rows = connection.execute(
                "SELECT DISTINCT folder FROM message_state ORDER BY folder"
            ).fetchall()

            return [row[0] for row in rows]

        except sqlite3.Error:
            return []

        finally:
            connection.close()

    def recent_snapshot(self, limit):
        connection = _get_db_readonly()

        if connection is None:
            return {"recent": [], "failed": []}

        try:
            recent_rows = connection.execute(
                "SELECT folder, uidvalidity, uid, status, attempts, filepath, updated_at "
                "FROM message_state "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

            failed_rows = connection.execute(
                "SELECT folder, uidvalidity, uid, attempts, last_error, updated_at "
                "FROM message_state "
                "WHERE status = 'failed' "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

            failures = []

            for row in failed_rows:
                failures.append(
                    {
                        "folder": row["folder"],
                        "uidvalidity": row["uidvalidity"],
                        "uid": row["uid"],
                        "attempts": row["attempts"],
                        "last_error": _safe_error_for_ui(
                            row["last_error"]
                        ),
                        "updated_at": row["updated_at"],
                    }
                )

            return {
                "recent": [
                    _row_to_record(row)
                    for row in recent_rows
                ],
                "failed": failures,
            }

        except sqlite3.Error:
            return {"recent": [], "failed": []}

        finally:
            connection.close()

    def records_page(self, folder, status, search, page, page_size):
        connection = _get_db_readonly()

        if connection is None:
            return {
                "records": [],
                "total_count": 0,
            }

        try:
            clauses = []
            parameters = []

            if folder:
                clauses.append("folder = ?")
                parameters.append(folder)

            if status:
                clauses.append("status = ?")
                parameters.append(status)

            if search:
                if search.isdigit():
                    clauses.append("uid = ?")
                    parameters.append(int(search))
                else:
                    clauses.append("filepath LIKE ? ESCAPE '\\'")
                    parameters.append(
                        f"%{_escape_like(search)}%"
                    )

            where_clause = (
                " WHERE " + " AND ".join(clauses)
                if clauses
                else ""
            )

            total_count = connection.execute(
                f"SELECT COUNT(*) FROM message_state{where_clause}",
                parameters,
            ).fetchone()[0]

            offset = (page - 1) * page_size

            rows = connection.execute(
                f"SELECT folder, uidvalidity, uid, status, attempts, filepath, updated_at "
                f"FROM message_state{where_clause} "
                f"ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (*parameters, page_size, offset),
            ).fetchall()

            return {
                "records": [
                    _row_to_record(row)
                    for row in rows
                ],
                "total_count": total_count,
            }

        except sqlite3.Error:
            return {
                "records": [],
                "total_count": 0,
            }

        finally:
            connection.close()

    def lookup_filepath(self, folder, uidvalidity, uid):
        connection = _get_db_readonly()

        if connection is None:
            return None

        try:
            row = connection.execute(
                "SELECT filepath FROM message_state "
                "WHERE folder = ? AND uidvalidity = ? AND uid = ? "
                "AND status = 'backed_up'",
                (folder, uidvalidity, uid),
            ).fetchone()

            return (
                row["filepath"]
                if row and row["filepath"]
                else None
            )

        except sqlite3.Error:
            return None

        finally:
            connection.close()


class DemoDataSource:
    """Fictional-only source used only when DEMO_MODE=True."""

    def __init__(self, data):
        self._folders = data.get("folders", [])
        self._messages = data.get("messages", [])

    def available(self):
        return True

    def status_counts(self):
        return {
            "backed_up": sum(
                message.get("status") == "backed_up"
                for message in self._messages
            ),
            "failed": sum(
                message.get("status") == "failed"
                for message in self._messages
            ),
        }

    def folder_summary(self):
        summary = []

        for folder_data in self._folders:
            folder = folder_data.get("folder", "Unknown")

            backed_up = sum(
                message.get("folder") == folder
                and message.get("status") == "backed_up"
                for message in self._messages
            )

            failed = sum(
                message.get("folder") == folder
                and message.get("status") == "failed"
                for message in self._messages
            )

            summary.append(
                {
                    **folder_data,
                    "backed_up": backed_up,
                    "failed": failed,
                }
            )

        return summary

    def latest_success_at(self):
        timestamps = [
            message.get("updated_at")
            for message in self._messages
            if message.get("status") == "backed_up"
            and message.get("updated_at")
        ]

        return max(timestamps) if timestamps else None

    def available_folders(self):
        return sorted(
            {
                message.get("folder")
                for message in self._messages
                if isinstance(message.get("folder"), str)
            }
        )

    def recent_snapshot(self, limit):
        sorted_rows = sorted(
            self._messages,
            key=lambda item: item.get("updated_at", ""),
            reverse=True,
        )

        recent = [
            self._to_record(row)
            for row in sorted_rows[:limit]
        ]

        failed = [
            {
                "folder": row.get("folder"),
                "uidvalidity": row.get("uidvalidity"),
                "uid": row.get("uid"),
                "attempts": row.get("attempts"),
                "last_error": _safe_error_for_ui(
                    row.get("last_error")
                ),
                "updated_at": row.get("updated_at"),
            }
            for row in sorted_rows
            if row.get("status") == "failed"
        ][:limit]

        return {
            "recent": recent,
            "failed": failed,
        }

    def records_page(self, folder, status, search, page, page_size):
        rows = list(self._messages)

        if folder:
            rows = [
                row for row in rows
                if row.get("folder") == folder
            ]

        if status:
            rows = [
                row for row in rows
                if row.get("status") == status
            ]

        if search:
            if search.isdigit():
                uid = int(search)
                rows = [
                    row for row in rows
                    if row.get("uid") == uid
                ]
            else:
                needle = search.lower()
                rows = [
                    row for row in rows
                    if needle in str(
                        row.get("filename", "")
                    ).lower()
                ]

        rows.sort(
            key=lambda item: item.get("updated_at", ""),
            reverse=True,
        )

        total_count = len(rows)
        offset = (page - 1) * page_size

        return {
            "records": [
                self._to_record(row)
                for row in rows[offset:offset + page_size]
            ],
            "total_count": total_count,
        }

    def lookup_filepath(self, folder, uidvalidity, uid):
        return None

    @staticmethod
    def _to_record(row):
        return {
            "folder": row.get("folder"),
            "uidvalidity": row.get("uidvalidity"),
            "uid": row.get("uid"),
            "status": row.get("status"),
            "attempts": row.get("attempts"),
            "filename": row.get("filename"),
            "updated_at": row.get("updated_at"),
        }


_demo_data_cache = None


def get_data_source():
    if not DEMO_MODE:
        return SqliteDataSource()

    global _demo_data_cache

    if _demo_data_cache is None:
        try:
            with open(
                DEMO_DATA_PATH,
                "r",
                encoding="utf-8",
            ) as demo_file:
                _demo_data_cache = json.load(demo_file)

        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "Demo data is unavailable or invalid."
            ) from error

    return DemoDataSource(_demo_data_cache)


# ================= INPUT VALIDATION =================

def _safe_int(value):
    if value is None or value == "":
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _validate_page_size(raw_value):
    value = _safe_int(raw_value)

    return (
        value
        if value in ALLOWED_PAGE_SIZES
        else DEFAULT_PAGE_SIZE
    )


def _validate_search(raw_value):
    if not raw_value:
        return None, None

    if not SEARCH_ALLOWED_RE.fullmatch(raw_value):
        return (
            None,
            "Search contains unsupported characters.",
        )

    return raw_value, None


def _validate_folder(raw_value):
    if not raw_value:
        return None, None

    if not FOLDER_ALLOWED_RE.fullmatch(raw_value):
        return None, "Invalid folder filter."

    return raw_value, None


def _validate_record_key(folder, uidvalidity, uid):
    if (
        not isinstance(folder, str)
        or not FOLDER_ALLOWED_RE.fullmatch(folder)
    ):
        return False

    if uidvalidity is None or uid is None:
        return False

    if uidvalidity < 0 or uid < 0:
        return False

    return True


# ================= CSRF =================

def _issue_csrf_token():
    token = session.get("csrf_token")

    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token

    return token


def _require_valid_csrf():
    expected_token = session.get("csrf_token")
    provided_token = request.headers.get("X-CSRF-Token", "")

    if not expected_token or not provided_token:
        abort(403)

    if not secrets.compare_digest(
        expected_token,
        provided_token,
    ):
        abort(403)


@app.context_processor
def inject_template_globals():
    return {
        "demo_mode": DEMO_MODE,
        "csrf_token": _issue_csrf_token(),
        "nav_items": NAV_ITEMS,
    }


# ================= PAGE ROUTES =================

@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/records")
def records():
    return render_template(
        "records.html",
        allowed_page_sizes=ALLOWED_PAGE_SIZES,
        default_page_size=DEFAULT_PAGE_SIZE,
    )


@app.route("/how-it-works")
def how_it_works():
    return render_template("how_it_works.html")


@app.route("/security")
def security():
    return render_template("security.html")


@app.route("/faq")
def faq():
    return render_template("faq.html")


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/demo-notice")
def demo_notice():
    return render_template("demo_notice.html")


# ================= API ROUTES =================

@app.route("/api/dashboard", methods=["GET"])
def api_dashboard():
    try:
        data_source = get_data_source()
    except RuntimeError:
        return jsonify(
            error="Dashboard data is unavailable."
        ), 503

    folders = data_source.folder_summary()
    snapshot = data_source.recent_snapshot(
        RECENT_SNAPSHOT_LIMIT
    )

    return jsonify(
        {
            "initialized": data_source.available(),
            "demo_mode": DEMO_MODE,
            "status_summary": data_source.status_counts(),
            "configured_folder_count": len(folders),
            "latest_success_at": data_source.latest_success_at(),
            "folders": folders,
            "available_folders": data_source.available_folders(),
            "recent_records": snapshot["recent"],
            "recent_failures": snapshot["failed"],
            "run_state": _public_run_state(),
        }
    )


@app.route("/api/records", methods=["GET"])
def api_records():
    folder_raw = request.args.get("folder") or None
    status = request.args.get("status") or None
    search_raw = request.args.get("search") or None

    folder, folder_error = _validate_folder(folder_raw)

    if folder_error:
        return jsonify(error=folder_error), 400

    if status is not None and status not in VALID_STATUSES:
        return jsonify(error="Invalid status filter."), 400

    search, search_error = _validate_search(search_raw)

    if search_error:
        return jsonify(error=search_error), 400

    page = _safe_int(request.args.get("page")) or 1
    page = max(page, 1)

    page_size = _validate_page_size(
        request.args.get("page_size")
    )

    try:
        result = get_data_source().records_page(
            folder,
            status,
            search,
            page,
            page_size,
        )
    except RuntimeError:
        return jsonify(
            error="Dashboard data is unavailable."
        ), 503

    total_count = result["total_count"]
    total_pages = max(
        1,
        (total_count + page_size - 1) // page_size,
    )

    if page > total_pages:
        page = total_pages

    return jsonify(
        {
            "demo_mode": DEMO_MODE,
            "records": result["records"],
            "page": page,
            "page_size": page_size,
            "total_count": total_count,
            "total_pages": total_pages,
        }
    )


@app.route("/api/run-status", methods=["GET"])
def api_run_status():
    return jsonify(_public_run_state())


@app.route("/api/backup/run", methods=["POST"])
def api_run_backup():
    if DEMO_MODE:
        abort(403)

    _require_valid_csrf()

    with run_lock:
        if run_state["status"] == "running":
            response = _public_run_state()
            response["error"] = "A backup is already running."
            return jsonify(response), 409

        run_state.update(
            status="running",
            started_at=_now_iso(),
            finished_at=None,
            returncode=None,
            saved=None,
            failed=None,
            error=None,
            log_tail="",
        )

    worker = threading.Thread(
        target=run_mback_command,
        daemon=True,
        name="mback-dashboard-runner",
    )

    worker.start()

    return jsonify(_public_run_state())


@app.route("/api/open-file", methods=["POST"])
def api_open_file():
    """
    Download a single .eml file through a validated database lookup.

    Browser input never sends a filesystem path. The backend:
    1. Receives only folder + uidvalidity + uid.
    2. Looks up filepath in SQLite.
    3. Resolves it.
    4. Verifies it belongs inside backups/.
    5. Verifies it is a real .eml file.
    """
    if DEMO_MODE:
        abort(403)

    _require_valid_csrf()

    payload = request.get_json(silent=True)

    if not isinstance(payload, dict):
        abort(400)

    folder = payload.get("folder")
    uidvalidity = _safe_int(
        payload.get("uidvalidity")
    )
    uid = _safe_int(payload.get("uid"))

    if not _validate_record_key(
        folder,
        uidvalidity,
        uid,
    ):
        abort(400)

    filepath = SqliteDataSource().lookup_filepath(
        folder,
        uidvalidity,
        uid,
    )

    if not filepath:
        abort(404)

    try:
        resolved_file = Path(filepath).resolve(
            strict=True
        )
        resolved_backups_root = BACKUPS_DIR.resolve(
            strict=True
        )
    except OSError:
        abort(404)

    if (
        resolved_file.suffix.lower() != ".eml"
        or not resolved_file.is_file()
    ):
        abort(404)

    try:
        resolved_file.relative_to(
            resolved_backups_root
        )
    except ValueError:
        abort(403)

    return send_file(
        resolved_file,
        mimetype="message/rfc822",
        as_attachment=True,
        download_name=resolved_file.name,
        conditional=False,
        max_age=0,
    )


# ================= STARTUP =================

def validate_startup_paths():
    """Warn locally about missing required project files without reading secrets."""
    if DEMO_MODE:
        return

    missing_files = []

    if not MBACK_SCRIPT.is_file():
        missing_files.append("Mback.py")

    if not CONFIG_PATH.is_file():
        missing_files.append("config.json")

    if missing_files:
        print(
            "WARNING: Dashboard started, but these local files are missing: "
            + ", ".join(missing_files),
            file=sys.stderr,
        )


if __name__ == "__main__":
    validate_startup_paths()

    if DEMO_MODE:
        print(
            "Starting in DEMO_MODE: fictional data only. "
            "Real backup execution and local file download are disabled."
        )

    app.run(
        host=HOST,
        port=PORT,
        debug=DEBUG,
        threaded=True,
    )