import zipfile
import io
import os
import shutil
from pathlib import Path
import re
import sys
import http.client
import json
import subprocess
import urllib.parse

# Configuration
REPO_OWNER = "eylenburg"
REPO_NAME = "linoffice"
CURRENT_VERSION = "2.2.9"
GITHUB_API_URL = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/releases"
PRESERVE_FILES = {"config/compose.yaml", "config/linoffice.conf", "config/oem/registry/regional_settings.reg"}
GITHUB_TOKEN = None  # Can replace with GitHub Personal Access Token if hitting API limits

APP_DIR = Path.home() / ".local/share/linoffice"
BIN_PATH = Path.home() / ".local/bin/linoffice"
STATE_DIR = Path.home() / ".local/state/linoffice"
LEGACY_APPDATA_DIR = Path.home() / ".local/share/linoffice"


def move_contents(source, destination):
    """Move all children from source into destination."""
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        target = destination / item.name
        if target.exists():
            if target.is_dir() and item.is_dir():
                move_contents(item, target)
                item.rmdir()
            else:
                backup = target.with_name(f"{target.name}.old")
                if backup.exists():
                    backup = target.with_name(f"{target.name}.old.{os.getpid()}")
                target.rename(backup)
                shutil.move(str(item), str(target))
        else:
            shutil.move(str(item), str(target))


def write_wrapper(app_dir=APP_DIR):
    BIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    BIN_PATH.write_text(f'#!/usr/bin/env bash\nexec "{app_dir}/linoffice.sh" "$@"\n')
    BIN_PATH.chmod(0o755)


def refresh_desktop_files(app_dir):
    setup_script = app_dir / "setup.sh"
    if not setup_script.exists():
        print(f"Skipping desktop launcher refresh; setup script not found: {setup_script}")
        return

    print("Refreshing desktop launchers...")
    result = subprocess.run(
        ["/bin/bash", str(setup_script), "--desktop"],
        cwd=str(app_dir),
        text=True,
    )
    if result.returncode == 0:
        print("Desktop launchers refreshed successfully.")
    else:
        print(f"Desktop launcher refresh failed with exit code {result.returncode}.")


def migrate_install_layout(current_dir):
    """Move old quickstart installs into the current partial layout."""
    current_dir = Path(current_dir).resolve()
    legacy_bin_dir = BIN_PATH

    if LEGACY_APPDATA_DIR.exists() and not (LEGACY_APPDATA_DIR / "linoffice.sh").exists():
        print(f"Moving state files from {LEGACY_APPDATA_DIR} to {STATE_DIR}")
        move_contents(LEGACY_APPDATA_DIR, STATE_DIR)
        try:
            LEGACY_APPDATA_DIR.rmdir()
        except OSError:
            pass

    if current_dir == legacy_bin_dir and legacy_bin_dir.is_dir() and (legacy_bin_dir / "linoffice.sh").exists():
        print(f"Moving LinOffice app from {legacy_bin_dir} to {APP_DIR}")
        APP_DIR.parent.mkdir(parents=True, exist_ok=True)
        if APP_DIR.exists():
            move_contents(legacy_bin_dir, APP_DIR)
            legacy_bin_dir.rmdir()
        else:
            shutil.move(str(legacy_bin_dir), str(APP_DIR))
        write_wrapper(APP_DIR)
        return APP_DIR

    if current_dir == APP_DIR:
        write_wrapper(APP_DIR)

    return current_dir

def get_latest_release():
    """Fetch the latest non-draft, non-prerelease release from GitHub."""
    try:
        conn = http.client.HTTPSConnection("api.github.com")
        headers = {
            "User-Agent": "LinofficeUpdateScript",
            "Accept": "application/vnd.github.v3+json"
        }
        if GITHUB_TOKEN:
            headers["Authorization"] = f"token {GITHUB_TOKEN}"

        path = f"/repos/{REPO_OWNER}/{REPO_NAME}/releases"
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        
        if response.status != 200:
            print(f"Error fetching releases: {response.status} {response.reason}")
            return None
        
        releases = json.loads(response.read().decode())
        for release in releases:
            if not release.get("prerelease") and not release.get("draft"):
                return release
        return None
    except Exception as e:
        print(f"Error fetching releases: {e}")
        return None

def version_tuple(v):
    return tuple(map(int, (v.split("."))))

def compare_versions(current_version, latest_version):
    """Compare two version strings."""
    return version_tuple(latest_version) > version_tuple(current_version)

def major_version(v):
    try:
        return int(v.split(".")[0])
    except Exception:
        return -1

def download_and_update(asset_url, current_dir):
    """Download and extract the new release, preserving specified files."""
    try:
        parsed_url = urllib.parse.urlparse(asset_url)
        conn = http.client.HTTPSConnection(parsed_url.netloc)
        headers = {
            "User-Agent": "PythonUpdateScript"
        }
        if GITHUB_TOKEN:
            headers["Authorization"] = f"token {GITHUB_TOKEN}"
        conn.request("GET", parsed_url.path, headers=headers)
        response = conn.getresponse()

        # Handle redirects (e.g., GitHub -> AWS)
        if response.status in (301, 302, 303, 307, 308):
            redirect_url = response.getheader("Location")
            if not redirect_url:
                print("Redirect without Location header")
                return False
            print(f"Redirected to: {redirect_url}")
            return download_and_update(redirect_url, current_dir)

        if response.status != 200:
            print(f"Error downloading asset: {response.status} {response.reason}")
            return False

        zip_file = zipfile.ZipFile(io.BytesIO(response.read()))

        # Get the top-level folder name in the zip (e.g., 'linoffice-1.0.7/')
        top_level_folder = next((name for name in zip_file.namelist() if '/' in name), None)
        if not top_level_folder:
            print("Error: Could not determine top-level folder in zip.")
            return False
        prefix = top_level_folder.split('/')[0] + '/'

        # Prefer extracting contents of 'src/' if present; otherwise use archive root
        has_src = any(name.startswith(prefix + 'src/') for name in zip_file.namelist())
        content_prefix = prefix + 'src/' if has_src else prefix

        # Count updated files
        updated_count = 0

        # Extract files, skipping directory entries and using selected content prefix
        for file_info in zip_file.infolist():
            if file_info.is_dir():
                continue
            # Only process files within the selected content prefix
            if not file_info.filename.startswith(content_prefix):
                continue
            relative_path = file_info.filename[len(content_prefix):]
            if not relative_path:
                continue
            target_path = Path(current_dir) / relative_path

            if relative_path in PRESERVE_FILES:
                print(f"Preserving {relative_path}")
                continue

            target_path.parent.mkdir(parents=True, exist_ok=True)
            with zip_file.open(file_info) as source, open(target_path, "wb") as target:
                shutil.copyfileobj(source, target)
            updated_count += 1

        zip_file.close()
        print(f"Update completed successfully. Updated {updated_count} files.")
        return True
    except Exception as e:
        print(f"Error during update: {e}")
        return False

def main():
    """Main function to check for updates and apply them."""
    print("Checking for updates...")
    release_data = get_latest_release()
    if not release_data:
        print("Failed to fetch release information.")
        return

    latest_version = release_data.get("tag_name", "").lstrip("v")
    if not re.match(r"\d+\.\d+\.\d+", latest_version):
        print("Invalid version format in latest release.")
        return

    if not compare_versions(CURRENT_VERSION, latest_version):
        print(f"No update needed. Current version: {CURRENT_VERSION}, Latest: {latest_version}")
        return

    print(f"New version available: {latest_version} (Current: {CURRENT_VERSION})")

    # If major version changes, prompt with release notes link and explicit warning
    if major_version(CURRENT_VERSION) != major_version(latest_version):
        release_notes_url = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/releases/tag/v{latest_version}"
        print("WARNING: This update changes the major version and may include breaking changes.")
        print("You may have to intervene manually if updating from the current version.")
        print(f"Please review the release notes: {release_notes_url}")
        confirm_major = input("Do you still want to update? (y/n): ").strip().lower()
        if confirm_major != 'y':
            print("Update cancelled.")
            return
    else:
        confirm = input("Do you want to download and install the update? (y/n): ").strip().lower()
        if confirm != 'y':
            print("Update cancelled.")
            return

    # Construct the GitHub tag-based zip URL
    asset_url = f"https://github.com/{REPO_OWNER}/{REPO_NAME}/archive/refs/tags/v{latest_version}.zip"
    print(f"Using download URL: {asset_url}")

    current_dir = migrate_install_layout(Path(sys.argv[0]).parent)
    if download_and_update(asset_url, current_dir):
        write_wrapper(current_dir)
        refresh_desktop_files(current_dir)
        print("Please restart the application to use the new version.")
    else:
        print("Update failed.")

if __name__ == "__main__":
    main()
