# Mback — Local-First IMAP Email Backup

Mback is a local-first IMAP email backup tool for private-domain and small-business mailboxes. It downloads complete emails, including headers, body content, and attachments, and saves them as standard `.eml` files on the user’s own system.

The project is designed around a simple principle: email backups should remain under the owner’s control. Mback uses encrypted IMAP connections, read-only mailbox access, local SQLite state tracking, and a localhost-only dashboard for monitoring backup health.

> **Portfolio note:** The public version of this project uses fictional demo data only. No real mailbox credentials, backup files, email content, subjects, logs, customer data, or database state are included in this repository.

---

## Features

- Full initial backup of configured IMAP folders.
- Incremental UID-based backups after the first run.
- Complete `.eml` email preservation, including attachments.
- Read-only IMAP access; the backup tool does not delete, move, send, mark, or modify messages.
- Per-message retry ledger for failed downloads.
- UIDVALIDITY-aware synchronization and safe re-sync behavior.
- Collision-safe backup filenames.
- Local SQLite state database.
- Structured logs for troubleshooting.
- Local Flask dashboard for:
  - Backup health and latest run status.
  - Total backed-up messages.
  - Pending retries.
  - Folder-level backup state.
  - Recent backup records.
  - Filterable backup records.
  - Secure local `.eml` download.
  - Manual “Run Backup Now” control.
- Demo mode for safe CV, LinkedIn, and portfolio screenshots.
- Local-only dashboard binding at `127.0.0.1`.

---

## Architecture

```text
IMAP Email Server
       │
       │ Encrypted SSL/TLS IMAP connection
       ▼
Mback.py Backup Engine
       │
       ├── Local .eml archive files
       ├── SQLite backup state
       ├── Retry tracking
       └── Local backup log
       │
       ▼
Flask Dashboard
http://127.0.0.1:5000
```

The dashboard does not directly connect to the mail server. It reads local backup metadata from SQLite and can trigger the existing backup engine through a fixed local command.

---

## Technology Stack

| Component | Technology |
|---|---|
| Backup engine | Python |
| Mail access | IMAP over SSL/TLS using `imaplib` |
| Email parsing | Python `email` module |
| State tracking | SQLite using `sqlite3` |
| Dashboard backend | Flask |
| Dashboard frontend | HTML, CSS, JavaScript |
| Email format | `.eml` / RFC822 |
| Automation | Windows Task Scheduler, optional |
| Packaging roadmap | PyInstaller / Windows installer |

---

## Project Structure

```text
Mback/
├── Mback.py
├── dashboard_app.py
├── requirements.txt
├── README.md
├── .gitignore
├── config.example.json
│
├── static/
│   ├── app.js
│   ├── style.css
│   ├── demo_data.json
│   └── images/
│
├── templates/
│   ├── base.html
│   ├── dashboard.html
│   ├── records.html
│   ├── how_it_works.html
│   ├── security.html
│   ├── faq.html
│   ├── about.html
│   ├── demo_notice.html
│   └── _icons.html
│
├── config.json              # Local only — never commit
├── backup_state.db          # Local only — never commit
├── backups/                 # Local only — never commit
├── email_backup.log         # Local only — never commit
└── .venv/                   # Local only — never commit
```

---

## How It Works

### 1. Initial backup

On the first run for a folder, Mback follows the configured backup mode:

- `"initial_backup_mode": "all"` downloads all messages available in the selected folder.
- `"initial_backup_mode": "recent"` downloads only the configured recent date range.

Each email is saved locally as an `.eml` file.

### 2. Incremental backup

After the first backup, Mback stores the highest processed IMAP UID for each folder. Later runs search only for newer UIDs, preventing normal re-download of emails that have already been backed up.

### 3. Retry handling

Mback stores a row for each message in a SQLite `message_state` table. If a download fails, the message remains marked as `failed` and is retried on the next backup run before newer mail is processed.

### 4. UIDVALIDITY handling

IMAP UIDs are stable only while a folder’s UIDVALIDITY remains unchanged. Mback stores UIDVALIDITY with its backup state. If the mail server changes it, the project performs a safe re-sync and avoids overwriting archived files.

---

## Security Model

Mback is designed to reduce unnecessary exposure of business email data.

- Uses IMAP over SSL/TLS.
- Opens mail folders in read-only mode.
- Uses search and fetch operations only.
- Does not delete, move, send, append, mark, or modify emails.
- Stores passwords only in local `config.json`.
- Keeps full email archives in local `backups/`.
- Keeps backup state in local SQLite.
- Runs the dashboard only at `127.0.0.1`.
- Uses CSRF protection for dashboard actions.
- Uses a fixed subprocess command with `shell=False`.
- Uses parameterized SQLite queries.
- Validates backup-file paths before allowing local downloads.
- Uses `.gitignore` to protect credentials, backups, logs, SQLite state, and virtual environments.

> Mback is a local backup tool, not a legal/compliance guarantee or replacement for a complete disaster-recovery strategy. Protect the Windows account, use encrypted storage, test restoration regularly, and maintain an additional backup strategy for critical business data.

---

## Installation

### Prerequisites

- Windows 10 or Windows 11.
- Python 3.10 or later.
- An IMAP-enabled email account.
- An app password if required by the email provider.
- Sufficient local disk storage for email attachments and archives.

### 1. Clone the repository

```powershell
git clone [https://github.com/YOUR_USERNAME/Mback.git](https://github.com/YOUR_USERNAME/Mback.git)
cd Mback
```

### 2. Create a Python virtual environment

```powershell
python -m venv .venv
```

### 3. Activate the environment

```powershell
.\.venv\Scripts\Activate.ps1
```

### 4. Install dependencies

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If `requirements.txt` does not exist yet, install Flask directly:

```powershell
pip install Flask
```

### 5. Create local configuration

Copy the example configuration:

```powershell
Copy-Item config.example.json config.json
```

Edit `config.json` locally and add your own IMAP settings.

**Never commit `config.json`.**

---

## Example Configuration

Create a local file named `config.json`:

```json
{
  "imap_server": "imap.example.com",
  "imap_port": 993,
  "email_account": "your-email@example.com",
  "email_password": "YOUR_APP_PASSWORD",
  "backup_folder": "backups",
  "days_to_backup": 30,
  "mail_folders": ["INBOX"],
  "state_db": "backup_state.db",
  "log_file": "email_backup.log",
  "initial_backup_mode": "all"
}
```

### Configuration fields

| Setting | Description |
|---|---|
| `imap_server` | Your mail provider’s IMAP server |
| `imap_port` | Usually `993` for IMAP over SSL/TLS |
| `email_account` | The mailbox address to back up |
| `email_password` | App password or mailbox password |
| `backup_folder` | Local directory where `.eml` files are stored |
| `days_to_backup` | Recent-date window used for recovery/re-sync behavior |
| `mail_folders` | IMAP folders to include, such as `INBOX` |
| `state_db` | SQLite state database path |
| `log_file` | Local log filename |
| `initial_backup_mode` | `"all"` or `"recent"` |

---

## Run the Backup Engine

Run Mback directly:

```powershell
python Mback.py
```

The backup engine will:

1. Connect to IMAP over SSL/TLS.
2. Open configured folders in read-only mode.
3. Retry pending failed messages.
4. Find new messages using IMAP UIDs.
5. Save complete emails as `.eml` files.
6. Update local SQLite state.
7. Write backup status to the log.

---

## Run the Dashboard

Start the local Flask dashboard:

```powershell
python dashboard_app.py
```

Then open:

```text
http://127.0.0.1:5000
```

The dashboard includes:

- Overview and health status.
- Recent backup records.
- Pending retry records.
- Folder-level status.
- Record search and filters.
- Download of validated local `.eml` files.
- Manual backup trigger.
- Documentation, FAQ, security information, and demo notice pages.

---

## Demo Mode

For portfolio screenshots or public demonstrations, enable demo mode in `dashboard_app.py`:

```python
DEMO_MODE = True
```

In demo mode:

- The dashboard uses fictional records from `static/demo_data.json`.
- Real `backup_state.db` is not read.
- Real backups cannot be triggered.
- Local `.eml` files cannot be downloaded.
- A “Demo data only” banner appears on every page.

Set it back to:

```python
DEMO_MODE = False
```

for real local use.

---

## Optional Daily Automation

Mback can be scheduled using Windows Task Scheduler.

Recommended task configuration:

```text
Program/script:
Path to python.exe

Arguments:
D:\Mback\Mback.py D:\Mback\config.json

Start in:
D:\Mback
```

Use a time when the system is normally connected to power and the internet.

---

## Important `.gitignore` Rules

Never commit:

```text
config.json
backup_state.db
backups/
*.eml
email_backup.log
.venv/
```

Public GitHub repositories should contain only source code, safe templates, documentation, and fictional demo data.

---

## Restore Testing

A backup should be tested, not only created.

To test restoration:

1. Download or locate a backed-up `.eml` file.
2. Open it with a compatible mail client such as Outlook or Thunderbird.
3. Verify the sender, subject, body, headers, and attachments.
4. Confirm the email can be inspected or imported when needed.

---

## Future Improvements

- Guided installation and first-run setup wizard.
- Windows Credential Manager / DPAPI secret storage.
- Backup run history and reporting.
- SHA-256 archive integrity verification.
- Storage and free-space monitoring.
- Multiple mailbox profiles.
- Configurable retention/archive policy.
- Metadata index and advanced local search.
- Windows installer and signed application package.
- Safe restore workflow with audit history.

---

## License

Add your preferred license before distributing the project.

For a personal portfolio project, you can use:

```text
MIT License
```

For commercial distribution, consult an appropriate legal professional before publishing terms, licenses, privacy policies, or support agreements.

---

## Author

Built by Anurag Chavan as a local-first email backup and monitoring project.

For public demonstrations, Mback uses fictional demo data only. Real business email data, credentials, archive files, logs, and backup databases remain private and local.
