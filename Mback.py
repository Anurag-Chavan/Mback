import imaplib
import email
import os
import sys
import json
import socket
import sqlite3
import logging
import datetime
from email.header import decode_header


# ================= CONFIG LOADING =================

DEFAULT_CONFIG_PATH = "config.json"
DEFAULT_STATE_DB = "backup_state.db"
DEFAULT_INITIAL_BACKUP_MODE = "recent"
VALID_INITIAL_BACKUP_MODES = ("all", "recent")
ERROR_MESSAGE_MAX_LEN = 500  # truncate stored last_error values to this many characters

REQUIRED_KEYS = [
    "imap_server",
    "imap_port",
    "email_account",
    "email_password",
    "backup_folder",
    "days_to_backup",
    "mail_folders",
]


def load_config(path: str) -> dict:
    """Load and validate config.json. Exits the program on failure."""
    if not os.path.exists(path):
        print(f"ERROR: Config file '{path}' not found.")
        print("Copy config.example.json to config.json and fill in your details.")
        sys.exit(1)

    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: Could not parse '{path}' as JSON: {e}")
        sys.exit(1)

    missing = [key for key in REQUIRED_KEYS if key not in config]
    if missing:
        print(f"ERROR: config.json is missing required keys: {', '.join(missing)}")
        sys.exit(1)

    if not isinstance(config["mail_folders"], list) or not config["mail_folders"]:
        print("ERROR: 'mail_folders' in config.json must be a non-empty list.")
        sys.exit(1)

    config.setdefault("state_db", DEFAULT_STATE_DB)
    config.setdefault("log_file", "email_backup.log")
    config.setdefault("initial_backup_mode", DEFAULT_INITIAL_BACKUP_MODE)

    if mode not in VALID_INITIAL_BACKUP_MODES:
        print(
            f"ERROR: 'initial_backup_mode' in config.json must be one of "
            f"{VALID_INITIAL_BACKUP_MODES!r}, got {mode!r}."
        )
        sys.exit(1)

    return config


# ================= LOGGING SETUP =================

def setup_logging(log_file: str) -> None:
    """Log to both the console and a log file. Never logs secrets."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

def init_db(db_path: str) -> sqlite3.Connection:
    """Open (creating if needed) the SQLite state database.

    Uses CREATE TABLE IF NOT EXISTS only -- this never touches or drops an
    existing folder_state or message_state table, so a Prompt 2 or 2.1
    backup_state.db keeps working unmodified.
    """
    conn = sqlite3.connect(db_path)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS folder_state (
            folder      TEXT PRIMARY KEY,
            uidvalidity INTEGER NOT NULL,
            last_uid    INTEGER NOT NULL,
            updated_at  TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_state (
            folder      TEXT NOT NULL,
            uidvalidity INTEGER NOT NULL,
            uid         INTEGER NOT NULL,
            status      TEXT NOT NULL CHECK (status IN ('backed_up', 'failed')),
            filepath    TEXT,
            attempts    INTEGER NOT NULL DEFAULT 0,
            last_error  TEXT,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (folder, uidvalidity, uid)
        )
        """
    )


    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_message_state_retry_lookup
        ON message_state (folder, uidvalidity, status)
        """
    )

    conn.commit()
    return conn



def get_folder_state(conn: sqlite3.Connection, folder: str):
    """Return {'uidvalidity': int, 'last_uid': int} for a folder, or None."""
    cur = conn.execute(
        "SELECT uidvalidity, last_uid FROM folder_state WHERE folder = ?",
        (folder,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {"uidvalidity": row[0], "last_uid": row[1]}


def save_folder_state(conn: sqlite3.Connection, folder: str, uidvalidity: int, last_uid: int) -> None:
    """Insert or update a folder's cursor state and commit immediately.

    Committing right away (rather than batching at the end) means that if
    the script crashes or loses connection mid-run, the next run resumes
    from the last UID that was actually written to disk.
    """
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO folder_state (folder, uidvalidity, last_uid, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(folder) DO UPDATE SET
            uidvalidity = excluded.uidvalidity,
            last_uid    = excluded.last_uid,
            updated_at  = excluded.updated_at
        """,
        (folder, uidvalidity, last_uid, now),
    )
    conn.commit()



def truncate_error(message) -> str:
    """Clamp an error string to ERROR_MESSAGE_MAX_LEN characters before storing it."""
    text = "" if message is None else str(message)
    if len(text) <= ERROR_MESSAGE_MAX_LEN:
        return text
    return text[:ERROR_MESSAGE_MAX_LEN] + "...(truncated)"


def get_message_state(conn: sqlite3.Connection, folder: str, uidvalidity: int, uid: int):
    """Return {'status', 'filepath', 'attempts'} for one message, or None."""
    cur = conn.execute(
        """
        SELECT status, filepath, attempts
        FROM message_state
        WHERE folder = ? AND uidvalidity = ? AND uid = ?
        """,
        (folder, uidvalidity, uid),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {"status": row[0], "filepath": row[1], "attempts": row[2]}


def get_failed_uids(conn: sqlite3.Connection, folder: str, uidvalidity: int) -> list[int]:
    """All UIDs currently marked 'failed' for this folder + CURRENT UIDVALIDITY only.

    Rows from a previous (different) UIDVALIDITY are intentionally excluded --
    those UIDs may no longer refer to the same message, so they are kept in
    the database purely for historical/audit purposes and are never retried.
    """
    cur = conn.execute(
        """
        SELECT uid FROM message_state
        WHERE folder = ? AND uidvalidity = ? AND status = 'failed'
        ORDER BY uid
        """,
        (folder, uidvalidity),
    )
    return [row[0] for row in cur.fetchall()]


def upsert_message_state(conn: sqlite3.Connection, folder: str, uidvalidity: int, uid: int,
                          status: str, filepath, attempts: int, last_error) -> None:
    """Record the outcome of one fetch attempt and commit immediately.

    This is the authoritative retry ledger: a message is only ever
    considered "done" once its row here says status = 'backed_up'.
    """
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO message_state
            (folder, uidvalidity, uid, status, filepath, attempts, last_error, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(folder, uidvalidity, uid) DO UPDATE SET
            status     = excluded.status,
            filepath   = excluded.filepath,
            attempts   = excluded.attempts,
            last_error = excluded.last_error,
            updated_at = excluded.updated_at
        """,
        (folder, uidvalidity, uid, status, filepath, attempts, truncate_error(last_error), now),
    )
    conn.commit()


# ================= HELPERS =================

def safe_filename(text: str) -> str:
    """Turn an email subject into a filesystem-safe string."""
    if not text:
        return "no-subject"

    try:
        decoded_parts = decode_header(text)
    except Exception:
        return "no-subject"

    decoded = ""
    for part, enc in decoded_parts:
        if isinstance(part, bytes):
            try:
                decoded += part.decode(enc or "utf-8", errors="replace")
            except (LookupError, UnicodeDecodeError):
                decoded += part.decode("utf-8", errors="replace")
        else:
            decoded += part

    # Keep only safe characters (also avoids Windows-illegal filename chars)
    safe = "".join(c if c.isalnum() or c in " _-." else "_" for c in decoded)
    return " ".join(safe.split())  # collapse multiple spaces


def safe_foldername(folder: str) -> str:
    """Turn an IMAP folder name (e.g. 'INBOX.Sent') into a safe local dir name."""
    return "".join(c if c.isalnum() or c in " _-." else "_" for c in folder)


def get_date_range_criteria(days: int) -> str:
    """Build an IMAP SEARCH criteria string for 'messages since N days ago'."""
    start_date = datetime.date.today() - datetime.timedelta(days=days)
    since = start_date.strftime("%d-%b-%Y")  # IMAP date format: "DD-Mon-YYYY"
    return f'SINCE "{since}"'


def get_unique_filepath(folder_dir: str, date_str: str, subject_safe: str, uid_str: str, uidvalidity: int) -> str:
    """Build a save path for a message, never overwriting an existing file.

    The UID is already in the filename, which is unique within a single
    UIDVALIDITY. If UIDVALIDITY ever changes (a resync), UIDs can be
    reused by the server for different messages, so on any collision we
    add the UIDVALIDITY, and failing that a numeric suffix, to guarantee
    we never clobber a previously saved .eml file.
    """
    base = f"{date_str}_{subject_safe}_uid-{uid_str}"

    candidate = os.path.join(folder_dir, base + ".eml")
    if not os.path.exists(candidate):
        return candidate

    candidate = os.path.join(folder_dir, f"{base}_v{uidvalidity}.eml")
    if not os.path.exists(candidate):
        return candidate

    n = 1
    while True:
        candidate = os.path.join(folder_dir, f"{base}_v{uidvalidity}_{n}.eml")
        if not os.path.exists(candidate):
            return candidate
        n += 1


def get_uidvalidity(mail: imaplib.IMAP4_SSL):
    """Read the UIDVALIDITY value for the currently selected mailbox."""
    try:
        status, data = mail.response("UIDVALIDITY")
        if status != "UIDVALIDITY" or not data:
            return None
        return int(data[0])
    except (imaplib.IMAP4.error, TypeError, ValueError):
        return None


def message_date_str(msg: "email.message.Message") -> str:
    """Best-effort YYYY-MM-DD for a message: its own Date header, else today."""
    date_str = datetime.date.today().strftime("%Y-%m-%d")
    msg_date = msg.get("Date")
    if msg_date:
        try:
            parsed = email.utils.parsedate_to_datetime(msg_date)
            if parsed:
                date_str = parsed.strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            pass  # fall back to today's date
    return date_str


# ================= CORE LOGIC =================

def connect_imap(server: str, port: int, account: str, password: str):
    """Connect and log in to the IMAP server. Returns the connection object."""
    try:
        mail = imaplib.IMAP4_SSL(server, port, timeout=30)
    except (socket.gaierror, socket.timeout, OSError) as e:
        raise ConnectionError(f"Could not reach IMAP server '{server}:{port}': {e}")

    try:
        mail.login(account, password)
    except imaplib.IMAP4.error as e:

        try:
            mail.logout()
        except Exception:
            pass
        raise PermissionError(f"Login failed for '{account}'. Check your email/app password. ({e})")

    return mail


def fetch_and_save_message(mail: imaplib.IMAP4_SSL, conn: sqlite3.Connection,
                            folder: str, uidvalidity: int, uid: int, folder_dir: str) -> bool:
    """
    Attempt to back up exactly one message (used for both retries and new UIDs).

    Always updates message_state -- the authoritative retry ledger -- with
    the outcome, so a failure here is never silently lost: the next run's
    retry pass (get_failed_uids) will pick this UID up again no matter how
    many higher UIDs succeed in the meantime.

    Returns True if the message ends this call as 'backed_up' (either just
    now, or already on disk from a previous run), False if it failed.
    """
    uid_str = str(uid)
    existing = get_message_state(conn, folder, uidvalidity, uid)
    attempts = (existing["attempts"] if existing else 0) + 1

    # Don't re-download something we already have. A valid prior success is
    # only trusted if the file it points to still genuinely exists on disk
    # and isn't empty -- protects against a manually deleted/corrupted file.
    if existing and existing["status"] == "backed_up" and existing["filepath"]:
        if os.path.isfile(existing["filepath"]) and os.path.getsize(existing["filepath"]) > 0:
            return True
        logging.warning(
            f"[{folder}] UID={uid_str} was marked backed_up but its file is missing "
            f"or empty on disk -- re-fetching."
        )

    try:
        status, msg_data = mail.uid("FETCH", uid_str, "(RFC822)")
        if status != "OK" or not msg_data or msg_data[0] is None:
            raise RuntimeError(f"UID FETCH returned no data (status={status})")

        raw_email = msg_data[0][1]
        if raw_email is None:
            raise RuntimeError("UID FETCH returned an empty message body")

        msg = email.message_from_bytes(raw_email)
        subject_safe = safe_filename(msg.get("Subject") or "")[:60]
        date_str = message_date_str(msg)

        filepath = get_unique_filepath(folder_dir, date_str, subject_safe, uid_str, uidvalidity)
        with open(filepath, "wb") as f:
            f.write(raw_email)

        upsert_message_state(conn, folder, uidvalidity, uid, "backed_up", filepath, attempts, None)
        logging.info(f"[{folder}] Saved: {os.path.basename(filepath)} (attempt {attempts})")
        return True

    except (imaplib.IMAP4.error, socket.error, OSError, RuntimeError) as e:
        upsert_message_state(conn, folder, uidvalidity, uid, "failed", None, attempts, e)
        logging.error(f"[{folder}] Failed to back up UID={uid_str} (attempt {attempts}): {e}")
        return False
    except Exception as e:
        # Catch-all so one weird/corrupt message never stops the whole run
        upsert_message_state(conn, folder, uidvalidity, uid, "failed", None, attempts, e)
        logging.error(f"[{folder}] Unexpected error on UID={uid_str} (attempt {attempts}): {e}")
        return False


def backup_folder(mail: imaplib.IMAP4_SSL, conn: sqlite3.Connection, folder: str,
                   days: int, backup_root: str, initial_backup_mode: str) -> tuple[int, int]:
    """
    Incrementally back up a single IMAP folder. Returns (saved_count, failed_count).
    Never raises for per-message problems -- those are logged and recorded
    in message_state, and processing continues with the next UID.
    """
    logging.info(f"Selecting folder (read-only): {folder}")

    # readonly=True makes imaplib issue EXAMINE instead of SELECT, so the
    # server never marks anything as \Seen and we can't accidentally modify
    # the mailbox even if we made a mistake elsewhere in this function.
    status, _ = mail.select(f'"{folder}"', readonly=True)
    if status != "OK":
        logging.error(f"Could not open folder '{folder}' -- skipping it. "
                       f"(Check the folder name matches your mail provider exactly.)")
        return 0, 0

    uidvalidity = get_uidvalidity(mail)
    if uidvalidity is None:
        logging.error(f"Could not determine UIDVALIDITY for '{folder}' -- skipping it.")
        return 0, 0

    folder_state = get_folder_state(conn, folder)
    first_run = folder_state is None
    resync = False
    last_uid_floor = 0

    if first_run:
        # "all"    -> true UID SEARCH ALL: every message currently in the folder.
        # "recent" -> unchanged Prompt 2.1 behavior: only the last `days` days.
        if initial_backup_mode == "all":
            criteria = "ALL"
            logging.info(f"No previous cursor for '{folder}'. First-time backup: "
                         f"ALL messages (initial_backup_mode='all').")
        else:
            criteria = get_date_range_criteria(days)
            logging.info(f"No previous cursor for '{folder}'. First-time backup: "
                         f"last {days} day(s) (initial_backup_mode='recent').")
    elif folder_state["uidvalidity"] != uidvalidity:
        # it always uses the days_to_backup date window, regardless of
        # initial_backup_mode. A separate, explicitly documented option
        # would be needed to make a resync use ALL instead -- see module
        # docstring.
        resync = True
        logging.warning(
            f"UIDVALIDITY changed for '{folder}' (was {folder_state['uidvalidity']}, now {uidvalidity}). "
            f"Re-scanning the last {days} day(s) to be safe (resync always uses the date window, "
            f"independent of initial_backup_mode). Failed UIDs from the OLD UIDVALIDITY are kept in "
            f"message_state for history but will NOT be retried -- only the current UIDVALIDITY is. "
            f"Existing .eml files are never overwritten."
        )
        criteria = get_date_range_criteria(days)
    else:
        last_uid_floor = folder_state["last_uid"]
        criteria = f"UID {last_uid_floor + 1}:*"
        logging.info(f"Incremental scan for '{folder}' -- fetching UIDs greater than {last_uid_floor}.")

    folder_dir = os.path.join(backup_root, safe_foldername(folder))
    os.makedirs(folder_dir, exist_ok=True)

    saved = 0
    failed = 0
    highest_saved_uid = last_uid_floor

    def maybe_advance_cursor(uid: int) -> None:
        """Advance & persist the folder_state cursor, but ONLY forward, never back.

        message_state (not this cursor) is what guarantees a failed UID gets
        retried -- this function purely keeps the cursor a valid "everything
        at or below this UID has at least been attempted" high-water mark.
        """
        nonlocal highest_saved_uid
        if uid > highest_saved_uid:
            highest_saved_uid = uid
            save_folder_state(conn, folder, uidvalidity, highest_saved_uid)

    # ---- Step 1: retry everything currently marked 'failed' for this
    # folder + CURRENT UIDVALIDITY, BEFORE looking for new mail. This is
    # what guarantees an old failed UID is never abandoned just because a
    # later UID already succeeded and moved the cursor past it. ----
    retry_uids = get_failed_uids(conn, folder, uidvalidity)
    if retry_uids:
        logging.info(f"[{folder}] Retrying {len(retry_uids)} previously failed UID(s): {retry_uids}")
    for uid in retry_uids:
        if fetch_and_save_message(mail, conn, folder, uidvalidity, uid, folder_dir):
            saved += 1
            maybe_advance_cursor(uid)
        else:
            failed += 1
            # Deliberately do NOT advance the cursor and deliberately do NOT
            # stop the loop -- the next failed UID still gets its own attempt,
            # and this one stays 'failed' in message_state for next time.
    try:
        status, data = mail.uid("SEARCH", None, criteria)
    except imaplib.IMAP4.error as e:
        logging.error(f"UID SEARCH failed in '{folder}': {e}")
        if first_run or resync:
            save_folder_state(conn, folder, uidvalidity, highest_saved_uid)
        return saved, failed

    if status != "OK" or not data or not data[0]:
        logging.info(f"No messages found in '{folder}'.")
        if first_run or resync:
            save_folder_state(conn, folder, uidvalidity, highest_saved_uid)
        return saved, failed

    uid_list = sorted(int(u) for u in data[0].split())
    retry_uid_set = set(retry_uids)
    # New candidates: above the cursor, and not something we just retried above
    # (avoids double-processing the same UID twice in one run). On a first
    # run last_uid_floor is 0, so "ALL" or the date window naturally yields
    # every matching UID as a "new" candidate.
    new_uids = [u for u in uid_list if u > last_uid_floor and u not in retry_uid_set]

    if not new_uids:
        logging.info(f"No new messages in '{folder}'.")
        if first_run or resync:
            save_folder_state(conn, folder, uidvalidity, highest_saved_uid)
        return saved, failed

    logging.info(f"Found {len(new_uids)} new message(s) to back up in '{folder}'.")

    for uid in new_uids:
        if fetch_and_save_message(mail, conn, folder, uidvalidity, uid, folder_dir):
            saved += 1
            maybe_advance_cursor(uid)
        else:
            failed += 1
            # Same as above: a new UID that fails right now simply stays
            # 'failed' in message_state and will be retried on the next run,
            # regardless of whether higher UIDs after it succeed.

    return saved, failed


def backup_emails(config: dict) -> None:
    server = config["imap_server"]
    port = config["imap_port"]
    account = config["email_account"]
    password = config["email_password"]
    backup_root = config["backup_folder"]
    days = config["days_to_backup"]
    folders = config["mail_folders"]
    state_db_path = config["state_db"]
    initial_backup_mode = config["initial_backup_mode"]

    policy_desc = (
        f"ALL messages" if initial_backup_mode == "all"
        else f"last {days} day(s) (RECENT)"
    )
    logging.info(
        f"Starting incremental backup for {account} ({len(folders)} folder(s)). "
        f"First-run policy: {policy_desc}."
    )
    os.makedirs(backup_root, exist_ok=True)

    conn = init_db(state_db_path)

    try:
        mail = connect_imap(server, port, account, password)
    except (ConnectionError, PermissionError) as e:
        logging.error(str(e))
        conn.close()
        sys.exit(1)

    total_saved = 0
    total_failed = 0

    try:
        for folder in folders:
            try:
                saved, failed = backup_folder(mail, conn, folder, days, backup_root, initial_backup_mode)
                total_saved += saved
                total_failed += failed
            except imaplib.IMAP4.abort as e:
                # Connection dropped mid-run -- log and stop gracefully.
                # Everything already committed to message_state/folder_state
                # before the abort is safely persisted and correctly retryable.
                logging.error(f"IMAP connection aborted: {e}")
                break
    finally:
        try:
            mail.close()
        except Exception:
            pass  # e.g. if no folder was ever successfully selected
        try:
            mail.logout()
        except Exception:
            pass
        conn.close()

    logging.info(f"Backup completed. Saved: {total_saved}, Failed: {total_failed}")


# ================= ENTRY POINT =================

if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    cfg = load_config(config_path)
    setup_logging(cfg.get("log_file", "email_backup.log"))

    try:
        backup_emails(cfg)
    except KeyboardInterrupt:
        logging.warning("Backup interrupted by user (Ctrl+C).")
        sys.exit(1)
    except Exception as e:
        # Last-resort catch so the log always shows *something* went wrong
        logging.exception(f"Fatal error during backup: {e}")
        sys.exit(1)
