#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时脚本：替换 app.py 中第519-1368行为导入语句"""

import os

filepath = os.path.join(os.path.dirname(__file__), 'app.py')

with open(filepath, 'r', encoding='utf-8') as f:
    lines = f.readlines()

print(f"原始行数: {len(lines)}")
print(f"将删除第519行到第1368行（共 {1368 - 519 + 1} 行）")

# 要插入的导入代码（替换 lines[518:1368]，即第519-1368行）
import_block = [
    "# ---------------------------------------------------------------------------\n",
    "# 后台任务工作服务（已拆分到 services/task_worker_service.py）\n",
    "# ---------------------------------------------------------------------------\n",
    "from services.task_worker_service import (\n",
    "    configure_task_worker, register_cleanup,\n",
    "    TaskWrapper, background_task_queue,\n",
    "    start_background_task_worker, stop_background_task_worker,\n",
    "    add_excel_diff_task, add_excel_diff_tasks_batch,\n",
    "    create_auto_sync_task, create_weekly_sync_task,\n",
    "    cleanup_git_processes, queue_missing_git_branch_refresh,\n",
    "    regenerate_repository_cache,\n",
    "    setup_schedule, start_scheduler,\n",
    ")\n",
    "\n",
]

new_lines = lines[:518] + import_block + lines[1368:]

with open(filepath, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print(f"新行数: {len(new_lines)}")
print(f"减少了: {len(lines) - len(new_lines)} 行")
