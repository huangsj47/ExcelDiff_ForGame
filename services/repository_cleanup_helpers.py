"""Repository local directory cleanup helpers extracted from app.py."""

from __future__ import annotations

import os
import threading
from datetime import datetime

from services.model_loader import get_runtime_model


PENDING_DELETIONS_FILE = "pending_deletions.txt"


def _log(message, log_type="INFO", force=False):
    try:
        log_print = get_runtime_model("log_print")
        log_print(message, log_type, force=force)
    except Exception:
        pass


def delete_local_repository_directory(local_path, repo_name):
    """Delete repository local directory with multiple fallback strategies."""

    def delete_directory():
        try:
            if not os.path.exists(local_path):
                _log(f"Directory not found, skip delete: {local_path}", "DELETE")
                return

            success = try_standard_delete(local_path, repo_name)
            if success:
                return

            success = try_remove_readonly_and_delete(local_path, repo_name)
            if success:
                return

            success = try_windows_command_delete(local_path, repo_name)
            if success:
                return

            success = try_powershell_force_delete(local_path, repo_name)
            if success:
                return

            _log(f"All delete strategies failed: {local_path}", "DELETE")
            record_pending_deletion(local_path, repo_name)
        except Exception as exc:
            _log(f"Delete directory failed: {local_path} | error: {exc}", "DELETE")
            record_pending_deletion(local_path, repo_name)

    delete_thread = threading.Thread(target=delete_directory)
    delete_thread.daemon = True
    delete_thread.start()


def try_standard_delete(local_path, repo_name):
    """Strategy 1: standard shutil.rmtree delete."""
    try:
        import shutil

        shutil.rmtree(local_path, ignore_errors=False)
        if not os.path.exists(local_path):
            _log(f"Standard delete success: {local_path} (repo: {repo_name})", "DELETE")
            return True

        _log(f"Standard delete incomplete: {local_path}", "DELETE")
        return False
    except Exception as exc:
        _log(f"Standard delete failed: {local_path} | error: {exc}", "DELETE")
        return False


def try_remove_readonly_and_delete(local_path, repo_name):
    """Strategy 2: clear readonly attribute and delete again."""
    try:
        import shutil
        import subprocess

        subprocess.run(
            ["attrib", "-R", f"{local_path}\\*.*", "/S", "/D"],
            capture_output=True,
            check=False,
        )
        shutil.rmtree(local_path, ignore_errors=False)
        if not os.path.exists(local_path):
            _log(f"Readonly-clear delete success: {local_path} (repo: {repo_name})", "DELETE")
            return True

        _log(f"Readonly-clear delete incomplete: {local_path}", "DELETE")
        return False
    except Exception as exc:
        _log(f"Readonly-clear delete failed: {local_path} | error: {exc}", "DELETE")
        return False


def try_windows_command_delete(local_path, repo_name):
    """Strategy 3: Windows rmdir delete."""
    try:
        import subprocess

        result = subprocess.run(
            ["rmdir", "/s", "/q", local_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if not os.path.exists(local_path):
            _log(f"Windows command delete success: {local_path} (repo: {repo_name})", "DELETE")
            return True

        _log(f"Windows command delete failed: {local_path} | stderr: {result.stderr}", "DELETE")
        return False
    except Exception as exc:
        _log(f"Windows command delete exception: {local_path} | error: {exc}", "DELETE")
        return False


def try_powershell_force_delete(local_path, repo_name):
    """Strategy 4: PowerShell forced delete."""
    try:
        import subprocess

        ps_command = f'Remove-Item -Path "{local_path}" -Recurse -Force -ErrorAction SilentlyContinue'
        subprocess.run(
            ["powershell", "-Command", ps_command],
            capture_output=True,
            text=True,
            check=False,
        )
        if not os.path.exists(local_path):
            _log(f"PowerShell forced delete success: {local_path} (repo: {repo_name})", "DELETE")
            return True

        _log(f"PowerShell forced delete failed: {local_path}", "DELETE")
        return False
    except Exception as exc:
        _log(f"PowerShell forced delete exception: {local_path} | error: {exc}", "DELETE")
        return False


def record_pending_deletion(local_path, repo_name):
    """Record pending deletion directory to local file."""
    try:
        with open(PENDING_DELETIONS_FILE, "a", encoding="utf-8") as file_obj:
            file_obj.write(f"{local_path}|{repo_name}|{datetime.now().isoformat()}\n")
        _log(f"Pending delete directory recorded: {local_path}", "REPO")
    except Exception as exc:
        _log(f"Record pending delete failed: {exc}", "REPO", force=True)


def cleanup_pending_deletions():
    """Retry cleanup for pending deletion directories."""
    import shutil

    def cleanup_directories():
        try:
            if not os.path.exists(PENDING_DELETIONS_FILE):
                return

            with open(PENDING_DELETIONS_FILE, "r", encoding="utf-8") as file_obj:
                lines = file_obj.readlines()

            if not lines:
                return

            _log(f"Found {len(lines)} pending directories, start cleanup", "REPO")
            remaining_lines = []
            deleted_count = 0

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                try:
                    parts = line.split("|")
                    if len(parts) < 3:
                        continue

                    local_path = parts[0]
                    repo_name = parts[1]

                    if os.path.exists(local_path):
                        try:
                            shutil.rmtree(local_path, ignore_errors=False)
                            if not os.path.exists(local_path):
                                _log(f"Delete success: {local_path} (repo: {repo_name})", "REPO")
                                deleted_count += 1
                            else:
                                remaining_lines.append(line)
                                _log(f"Delete incomplete, keep pending: {local_path}", "REPO")
                        except PermissionError:
                            remaining_lines.append(line)
                            _log(f"Permission denied, keep pending: {local_path}", "REPO")
                        except Exception as exc:
                            remaining_lines.append(line)
                            _log(f"Delete failed: {local_path}, error: {exc}", "REPO")
                    else:
                        _log(f"Directory already missing, skip: {local_path}", "REPO")
                        deleted_count += 1
                except Exception as exc:
                    remaining_lines.append(line)
                    _log(f"Parse pending record failed: {line}, error: {exc}", "REPO")

            if remaining_lines:
                with open(PENDING_DELETIONS_FILE, "w", encoding="utf-8") as file_obj:
                    for remaining in remaining_lines:
                        file_obj.write(remaining + "\n")
                _log(
                    f"Pending cleanup done, deleted {deleted_count}, remaining {len(remaining_lines)}",
                    "REPO",
                )
            else:
                os.remove(PENDING_DELETIONS_FILE)
                _log(f"Pending cleanup done, deleted all {deleted_count}", "REPO")
        except Exception as exc:
            _log(f"Pending cleanup process failed: {exc}", "REPO", force=True)

    cleanup_thread = threading.Thread(target=cleanup_directories, daemon=True)
    cleanup_thread.start()
