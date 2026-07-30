# QBO SPH — Time Charge Cleanup Patch

This patch adds bulk QuickBooks Online `TimeActivity` cleanup to the existing SPH Calculator and File Editor.

## What it adds

- `/time-charges` page inside the existing FastAPI app
- Paginated scan of QuickBooks `TimeActivity` records
- Date, customer/job, employee/vendor, item, description, and billable-status filters
- Client-side table pagination and safe bulk selection
- Mandatory CSV backup of every selected ID before deletion
- Exact typed deletion confirmation based on the selected count
- Permanent protection for `HasBeenBilled` records
- QuickBooks batch deletes in groups of 10
- Browser-side pacing between delete chunks to stay below API limits
- PostgreSQL/SQLite audit tables for scans and deletion results
- No new Python dependencies

## Install

1. Download and unzip this patch.
2. Open a terminal in your local `QBO_SPH` repository.
3. Run:

### Windows

```powershell
python "C:\path\to\QBO_SPH_Time_Charge_Cleanup_Patch\apply_patch.py" .
```

### macOS/Linux

```bash
python /path/to/QBO_SPH_Time_Charge_Cleanup_Patch/apply_patch.py .
```

The installer:

- copies `app/time_charge_cleanup.py`
- copies `app/templates/time_charge_cleanup.html`
- adds the router to `app/main.py`
- adds navigation and dashboard links
- appends documentation to `README.md`
- creates timestamped backups inside the repository
- validates the patched files

It is safe to run the installer more than once.

## First test

Keep this environment variable enabled:

```text
QBO_READ_ONLY=true
```

Start the app and open:

```text
http://127.0.0.1:8000/time-charges
```

Test in this order:

1. Scan a narrow date range.
2. Review the matching records.
3. Select a few safe records.
4. Download the CSV backup.
5. Confirm that deletion remains blocked while `QBO_READ_ONLY=true`.

After the scan and backup workflow is verified, change the production environment to:

```text
QBO_READ_ONLY=false
```

Deploy, scan a very narrow date range, and delete one or two known test records first.

## Deletion behavior

- A scan remains valid for 45 minutes.
- The server stores the scan temporarily in the app database.
- The server remembers exactly which QuickBooks IDs were included in downloaded backups.
- A changed selection requires another backup download.
- Each browser request deletes at most 40 records.
- Each QuickBooks batch contains at most 10 delete operations.
- The browser pauses between chunks.
- `HasBeenBilled` records are never submitted for deletion.
- QuickBooks error code `610` is treated as already missing.
- Every success, protected record, failure, and unknown result is logged.

## New database tables

The feature creates these automatically with `checkfirst=True`:

```text
time_charge_cleanup_scans
time_charge_cleanup_deletion_log
```

No Alembic migration is required for this repository's current deployment pattern.

## Rollback

The installer creates a folder similar to:

```text
.time-charge-cleanup-backup-20260730-123000
```

Restore the backed-up `main.py`, `base.html`, `dashboard.html`, and `README.md`, then delete:

```text
app/time_charge_cleanup.py
app/templates/time_charge_cleanup.html
```

The two database tables can remain without affecting the existing application.
