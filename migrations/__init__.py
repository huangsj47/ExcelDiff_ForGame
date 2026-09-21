#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻量迁移的存放处。

平台尚未正式部署，所以这一版的原则是「**能重建就重建**」
（`python scripts/recreate_db.py`），迁移只服务于「保留数据的库」。

* `ai_run_claim_columns` —— `ai_analysis_run` 的活动运行认领（唯一约束）与降级原因
  两列一索引。前者是「同一目标 + 同一份输入同时只允许一条活动运行」这件事的
  **数据库级**保证。

本目录被 `scripts/check_file_length.py` 排除在外（见那里的 `EXCLUDED_DIRS`）。
"""
