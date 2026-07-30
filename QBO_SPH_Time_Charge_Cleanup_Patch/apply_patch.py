from __future__ import annotations

from datetime import datetime
from pathlib import Path
import argparse
import py_compile
import re
import shutil
import sys

PATCH_DIR = Path(__file__).resolve().parent
PAYLOAD_DIR = PATCH_DIR / "payload"


def backup_file(path: Path, backup_root: Path, repo_root: Path) -> None:
    if not path.exists():
        return
    relative = path.relative_to(repo_root)
    destination = backup_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, destination)


def write_payload(repo_root: Path, backup_root: Path) -> None:
    for source in PAYLOAD_DIR.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(PAYLOAD_DIR)
        destination = repo_root / relative
        backup_file(destination, backup_root, repo_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        print(f"Wrote {relative}")


def patch_main(repo_root: Path, backup_root: Path) -> None:
    path = repo_root / "app" / "main.py"
    text = path.read_text(encoding="utf-8")
    original = text

    import_line = "from .time_charge_cleanup import router as time_charge_cleanup_router"
    if import_line not in text:
        marker = ")\nsettings = get_settings()"
        if marker not in text:
            raise RuntimeError("Could not locate the end of the qbo_client import block in app/main.py.")
        text = text.replace(marker, f")\n{import_line}\nsettings = get_settings()", 1)

    include_line = "app.include_router(time_charge_cleanup_router)"
    if include_line not in text:
        marker = 'app = FastAPI(title="QBO SPH Calculator and File Editor")'
        if marker not in text:
            raise RuntimeError("Could not locate the FastAPI app declaration in app/main.py.")
        text = text.replace(marker, f"{marker}\n{include_line}", 1)

    if text != original:
        backup_file(path, backup_root, repo_root)
        path.write_text(text, encoding="utf-8")
        print("Patched app/main.py")
    else:
        print("app/main.py already contains the router integration")


def patch_base_template(repo_root: Path, backup_root: Path) -> None:
    path = repo_root / "app" / "templates" / "base.html"
    text = path.read_text(encoding="utf-8")
    original = text
    new_link = '<a href="/time-charges">Time Charge Cleanup</a>'
    if new_link not in text:
        marker = '<a href="/items">Item Price Manager</a>'
        if marker not in text:
            raise RuntimeError("Could not locate the Item Price Manager navigation link in base.html.")
        text = text.replace(marker, f"{marker}\n\n{new_link}", 1)

    if text != original:
        backup_file(path, backup_root, repo_root)
        path.write_text(text, encoding="utf-8")
        print("Patched app/templates/base.html")
    else:
        print("base.html already contains the Time Charge Cleanup link")


def patch_dashboard(repo_root: Path, backup_root: Path) -> None:
    path = repo_root / "app" / "templates" / "dashboard.html"
    text = path.read_text(encoding="utf-8")
    original = text
    new_card = '''

<a class="home-nav-card" href="/time-charges">

<span>Time Charge Cleanup</span>

<strong>Review, back up, and delete migrated time charges</strong>

</a>'''
    if 'href="/time-charges"' not in text:
        pattern = re.compile(
            r'(<a class="home-nav-card" href="/items">.*?</a>)',
            flags=re.DOTALL,
        )
        match = pattern.search(text)
        if not match:
            raise RuntimeError("Could not locate the Item Price Manager dashboard card in dashboard.html.")
        text = text[: match.end()] + new_card + text[match.end() :]

    if text != original:
        backup_file(path, backup_root, repo_root)
        path.write_text(text, encoding="utf-8")
        print("Patched app/templates/dashboard.html")
    else:
        print("dashboard.html already contains the Time Charge Cleanup card")


def patch_readme(repo_root: Path, backup_root: Path) -> None:
    path = repo_root / "README.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    marker = "## Time Charge Cleanup"
    if marker in text:
        print("README.md already contains the Time Charge Cleanup section")
        return
    addition = '''

## Time Charge Cleanup

The app includes a bulk cleanup workflow at `/time-charges` for migrated QuickBooks Online `TimeActivity` records.

Safety controls:

- Scans expire after 45 minutes.
- Only records contained in the current scan can be selected.
- A CSV backup covering every selected QuickBooks ID is required before deletion.
- The user must type an exact record-count confirmation phrase.
- `HasBeenBilled` time activities are permanently protected by this tool.
- Deletes are sent through the QuickBooks batch endpoint in groups of 10, with browser-side pacing between chunks.
- Every attempted record is retained in a local audit table.

QuickBooks writes remain controlled by `QBO_READ_ONLY`. Set `QBO_READ_ONLY=false` only after testing the scan and backup workflow.
'''
    backup_file(path, backup_root, repo_root)
    path.write_text(text.rstrip() + addition + "\n", encoding="utf-8")
    print("Updated README.md")


def validate(repo_root: Path) -> None:
    py_compile.compile(str(repo_root / "app" / "time_charge_cleanup.py"), doraise=True)
    main_text = (repo_root / "app" / "main.py").read_text(encoding="utf-8")
    base_text = (repo_root / "app" / "templates" / "base.html").read_text(encoding="utf-8")
    template_text = (repo_root / "app" / "templates" / "time_charge_cleanup.html").read_text(encoding="utf-8")

    required_main = [
        "from .time_charge_cleanup import router as time_charge_cleanup_router",
        "app.include_router(time_charge_cleanup_router)",
    ]
    for value in required_main:
        if value not in main_text:
            raise RuntimeError(f"Validation failed: missing {value!r} in app/main.py")
    if 'href="/time-charges"' not in base_text:
        raise RuntimeError("Validation failed: navigation link was not added to base.html")
    if '{% extends "base.html" %}' not in template_text:
        raise RuntimeError("Validation failed: time_charge_cleanup.html is incomplete")
    print("Validation passed")


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the QBO bulk TimeActivity cleanup feature.")
    parser.add_argument("repo", nargs="?", default=".", help="Path to the QBO_SPH repository root")
    args = parser.parse_args()

    repo_root = Path(args.repo).expanduser().resolve()
    required = [repo_root / "app" / "main.py", repo_root / "app" / "templates" / "base.html"]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        print("This does not look like the QBO_SPH repository root.", file=sys.stderr)
        for path in missing:
            print(f"Missing: {path}", file=sys.stderr)
        return 2

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = repo_root / f".time-charge-cleanup-backup-{timestamp}"
    backup_root.mkdir(parents=True, exist_ok=True)

    try:
        write_payload(repo_root, backup_root)
        patch_main(repo_root, backup_root)
        patch_base_template(repo_root, backup_root)
        patch_dashboard(repo_root, backup_root)
        patch_readme(repo_root, backup_root)
        validate(repo_root)
    except Exception as exc:
        print(f"Patch failed: {exc}", file=sys.stderr)
        print(f"Original files copied to: {backup_root}", file=sys.stderr)
        return 1

    print()
    print("Time Charge Cleanup installed successfully.")
    print(f"Backups: {backup_root}")
    print("Next: run the app, open /time-charges, scan with QBO_READ_ONLY=true, and test CSV export.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
