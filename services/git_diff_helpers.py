"""Reusable low-level diff helpers extracted from git_service."""

from __future__ import annotations

import difflib
import re

GIT_DIFF_HELPER_DF_COMPARE_ERRORS = (AttributeError, TypeError, ValueError, IndexError, KeyError)
GIT_DIFF_HELPER_BASIC_DIFF_ERRORS = (AttributeError, TypeError, ValueError)
GIT_DIFF_HELPER_INITIAL_DIFF_ERRORS = (AttributeError, TypeError, ValueError)


def parse_unified_diff(patch_text):
    """Parse unified diff patch text into hunks."""
    hunks = []
    lines = patch_text.split("\n")
    current_hunk = None

    for line in lines:
        if line.startswith("@@"):
            if current_hunk:
                hunks.append(current_hunk)
            match = re.match(r"@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@(.*)", line)
            if match:
                old_start = int(match.group(1))
                old_count = int(match.group(2)) if match.group(2) else 1
                new_start = int(match.group(3))
                new_count = int(match.group(4)) if match.group(4) else 1
                context = match.group(5).strip()
                current_hunk = {
                    "header": line,
                    "old_start": old_start,
                    "old_count": old_count,
                    "new_start": new_start,
                    "new_count": new_count,
                    "context": context,
                    "lines": [],
                }
        elif current_hunk and (line.startswith(" ") or line.startswith("+") or line.startswith("-")):
            if line.startswith(" "):
                line_type = "context"
            elif line.startswith("+"):
                line_type = "added"
            elif line.startswith("-"):
                line_type = "removed"
            else:
                continue
            current_hunk["lines"].append(
                {
                    "type": line_type,
                    "content": line[1:] if len(line) > 0 else "",
                    "raw": line,
                }
            )
    if current_hunk:
        hunks.append(current_hunk)
    return hunks


def compare_dataframes(old_df, new_df, sheet_name):
    """Compare DataFrame changes (legacy compatibility helper)."""
    changes = []
    try:
        old_df = old_df.astype(str).fillna("")
        new_df = new_df.astype(str).fillna("")
        old_rows, _old_cols = old_df.shape
        new_rows, _new_cols = new_df.shape
        if new_rows > old_rows:
            for i in range(old_rows, new_rows):
                row_data = {}
                for j, _col in enumerate(new_df.columns):
                    if j < len(new_df.columns):
                        row_data[chr(65 + j)] = new_df.iloc[i, j] if i < len(new_df) else ""
                changes.append(
                    {
                        "type": "added",
                        "sheet_name": sheet_name,
                        "row": i + 1,
                        "data": row_data,
                        "message": f"{sheet_name} 第{i+1}行新增",
                    }
                )
        min_rows = min(old_rows, new_rows)
        for _i in range(min_rows):
            # Legacy placeholder loop retained for compatibility with historical behavior.
            pass
    except GIT_DIFF_HELPER_DF_COMPARE_ERRORS as exc:
        print(f"DataFrame比较失败: {str(exc)}")
        return []
    return changes


def generate_basic_diff(previous_content, current_content, file_path):
    """Build basic textual diff payload."""
    try:
        previous_lines = previous_content.splitlines() if previous_content else []
        current_lines = current_content.splitlines() if current_content else []
        diff_lines = list(
            difflib.unified_diff(
                previous_lines,
                current_lines,
                fromfile=f"a/{file_path}",
                tofile=f"b/{file_path}",
                lineterm="",
            )
        )
        if not diff_lines:
            return None
        patch_text = "\n".join(diff_lines)
        hunks = parse_unified_diff(patch_text)
        return {
            "type": "code",
            "file_path": file_path,
            "patch": patch_text,
            "hunks": hunks,
        }
    except GIT_DIFF_HELPER_BASIC_DIFF_ERRORS as exc:
        print(f"生成基本diff失败: {str(exc)}")
        return None


def generate_initial_commit_diff(current_content, file_path):
    """Build initial commit diff payload where all lines are added."""
    try:
        lines = current_content.splitlines() if current_content else []
        hunk = {
            "header": f"@@ -0,0 +1,{len(lines)} @@",
            "old_start": 0,
            "old_count": 0,
            "new_start": 1,
            "new_count": len(lines),
            "context": "",
            "lines": [],
        }
        for i, line in enumerate(lines):
            hunk["lines"].append(
                {
                    "type": "added",
                    "content": line,
                    "raw": f"+{line}",
                    "old_line_number": None,
                    "new_line_number": i + 1,
                }
            )
        patch_lines = [f"--- /dev/null", f"+++ b/{file_path}", hunk["header"]]
        for line_info in hunk["lines"]:
            patch_lines.append(line_info["raw"])
        patch_text = "\n".join(patch_lines)
        return {
            "type": "code",
            "file_path": file_path,
            "patch": patch_text,
            "hunks": [hunk],
        }
    except GIT_DIFF_HELPER_INITIAL_DIFF_ERRORS as exc:
        print(f"生成初始提交diff失败: {str(exc)}")
        return None
