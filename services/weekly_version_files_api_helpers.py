"""Helpers for weekly_version_files_api to keep weekly_version_logic lean."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from services.excel_header_profiles import BUILTIN_PROFILES, parse_config
from services.weekly_deleted_excel_helpers import resolve_primary_operation


def normalize_naive_datetime(raw_value):
    if not isinstance(raw_value, datetime):
        return None
    if raw_value.tzinfo is None:
        return raw_value
    return raw_value.astimezone(timezone.utc).replace(tzinfo=None)


def _task_age_seconds(task, now_value):
    if task is None:
        return None
    status_value = str(getattr(task, "status", "") or "").lower()
    if status_value == "processing":
        base_time = normalize_naive_datetime(getattr(task, "started_at", None)) or normalize_naive_datetime(
            getattr(task, "created_at", None)
        )
    else:
        base_time = normalize_naive_datetime(getattr(task, "created_at", None))
    if base_time is None:
        return None
    return (now_value - base_time).total_seconds()


def is_stale_sync_task(task, now_value, *, pending_timeout_seconds=300, processing_timeout_seconds=1800):
    if task is None:
        return False
    status_value = str(getattr(task, "status", "") or "").lower()
    age_seconds = _task_age_seconds(task, now_value)
    if age_seconds is None:
        return False
    if status_value == "pending":
        return age_seconds > pending_timeout_seconds
    if status_value == "processing":
        return age_seconds > processing_timeout_seconds
    return False


def should_treat_sync_task_as_stale(task, now_value, *, is_enqueued=None, **timeouts):
    """这个同步任务该按「陈旧」处理吗（页面解锁 / 重建任务）。

    **还在内存队列里的 `pending` 不算陈旧**：队列只有一个 worker，前面排着每 2 分钟
    一轮的 `auto_sync`（所有仓库）与大仓库的周版本同步（800+ 文件、分钟级），所以一个
    刚建几分钟的 pending 排不到头是常态。原先只看年龄（300 秒），页面每轮询一次就把它
    置 failed、紧接着调度器又建一条新的 —— 实测一轮 30 分钟里重置 12 次、重建 12 次，
    队列剩余稳定在 31~33 不下降。

    **只对 `pending` 生效**：`processing` 的任务 worker 正拿在手里、账本里也还挂着
    （注销发生在处理完之后），拿账本挡会让真正卡死的那条永远不解锁页面 ——
    已经开始跑的任务仍旧按「跑了多久」判定。

    `is_enqueued` 由调用方传入（本模块不 import task_worker 那边的账本，避免反向依赖）。
    """
    if task is None:
        return False
    status_value = str(getattr(task, "status", "") or "").lower()
    if status_value == "pending" and is_enqueued is not None:
        if is_enqueued(getattr(task, "id", None)):
            return False
    return is_stale_sync_task(task, now_value, **timeouts)


def parse_json_list(raw_value):
    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        return raw_value
    if isinstance(raw_value, tuple):
        return list(raw_value)
    if isinstance(raw_value, str):
        text_value = raw_value.strip()
        if not text_value:
            return []
        try:
            parsed = json.loads(text_value)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, tuple):
                return list(parsed)
        except Exception:
            return [item.strip() for item in re.split(r"[,，;；|\n\r]+", text_value) if item and item.strip()]
    return []


def parse_json_obj(raw_value):
    if raw_value is None:
        return {}
    if isinstance(raw_value, dict):
        return raw_value
    if isinstance(raw_value, str):
        text_value = raw_value.strip()
        if not text_value:
            return {}
        try:
            parsed = json.loads(text_value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def parse_confirm_usernames(raw_value):
    if not raw_value:
        return []
    usernames = [item.strip() for item in re.split(r"[,，;；|\n\r]+", str(raw_value)) if item and item.strip()]
    unique_usernames = []
    for username in usernames:
        if username not in unique_usernames:
            unique_usernames.append(username)
    return unique_usernames


def extract_author_lookup_keys(raw_author):
    text = str(raw_author or "").strip()
    if not text:
        return []
    keys = []
    lower_text = text.lower()
    if all(symbol not in lower_text for symbol in ("@", "<", ">", " ")):
        keys.append(lower_text)
    if "@" in lower_text and "<" not in lower_text and ">" not in lower_text:
        email_prefix = lower_text.split("@", 1)[0].strip()
        if email_prefix and email_prefix not in keys:
            keys.append(email_prefix)
    for email in re.findall(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})", text):
        email_prefix = email.lower().split("@", 1)[0].strip()
        if email_prefix and email_prefix not in keys:
            keys.append(email_prefix)
    return keys


def resolve_author_display(raw_author, *, username_to_display_name_lower, email_prefix_to_display_name):
    text = str(raw_author or "").strip()
    if not text:
        return ""
    for author_key in extract_author_lookup_keys(text):
        mapped_name = username_to_display_name_lower.get(author_key) or email_prefix_to_display_name.get(author_key)
        if mapped_name:
            return mapped_name
    return text


def collect_file_entries(
    diff_caches,
    *,
    username_to_display_name,
    username_to_display_name_lower,
    email_prefix_to_display_name,
):
    """把缓存行组装成接口要的条目列表。返回 `(files, authors)`。

    ## 为什么搬出来

    `services/weekly_version_logic.py` 贴着 1800 行的硬闸门
    （`tests/test_todo_split_followup_round2.py:37`），而这一块是**纯数据组装**：
    进来一批缓存行、出去一个 dict 列表。搬走之后，「这个字段是从哪来的」只需要看
    这一个文件；原处留同名转发，调用方一字不改。

    `authors` 是这批文件里出现过的提交者（已映射成显示名、去重）。
    """
    files = []
    authors = set()
    for cache in diff_caches:
        # 解析提交者信息
        commit_authors = parse_json_list(cache.commit_authors)
        commit_messages = parse_json_list(cache.commit_messages)
        commit_times = parse_json_list(cache.commit_times)
        mapped_commit_authors = [
            resolve_author_display(
                author,
                username_to_display_name_lower=username_to_display_name_lower,
                email_prefix_to_display_name=email_prefix_to_display_name,
            )
            for author in commit_authors
            if str(author or '').strip()
        ]
        authors.update(mapped_commit_authors)

        confirm_usernames = parse_confirm_usernames(cache.status_changed_by)
        confirm_display_names = [
            username_to_display_name.get(username)
            or username_to_display_name_lower.get(username.lower(), username)
            for username in confirm_usernames
        ]
        confirm_user_display = ''
        confirm_user_title = ''
        if cache.overall_status in ('confirmed', 'rejected') and confirm_usernames:
            confirm_user_display = ', '.join(confirm_display_names)
            # title 保留用户名，便于定位账号
            confirm_user_title = ', '.join(confirm_usernames)

        # 解析合并diff数据以获取文件操作信息
        file_operations = []
        if cache.merged_diff_data:
            try:
                merged_data = parse_json_obj(cache.merged_diff_data)
                file_operations = merged_data.get('operations', [])
            except Exception:
                pass

        # 确定文件的主要操作类型（用于颜色编码）
        # 跟**窗口内的最终状态**走，而不是「操作里出现过 D 就标红」：文件被删掉又建回来时，
        # 旧口径会把它标成红色的「删除文件」，而它的 diff 页显示的是新增内容
        # （线上 奖励模式_CfgRewardMode.xlsx）。
        primary_operation = resolve_primary_operation(file_operations)
        files.append({
            'file_path': cache.file_path,
            'commit_count': cache.commit_count,
            'commit_authors': json.dumps(mapped_commit_authors, ensure_ascii=False),
            'commit_messages': json.dumps(commit_messages, ensure_ascii=False),  # 添加提交日志
            'commit_times': json.dumps(commit_times, ensure_ascii=False),        # 添加提交时间
            'overall_status': cache.overall_status,
            'status_changed_by': cache.status_changed_by,  # 操作者用户名
            'confirm_user_display': confirm_user_display,
            'confirm_user_title': confirm_user_title,
            'confirmation_status': cache.confirmation_status,
            'last_sync_time': cache.last_sync_time.isoformat() if cache.last_sync_time else None,
            'operations': file_operations,  # 所有操作
            'primary_operation': primary_operation,  # 主要操作类型
            # 这张表命中的表头方案（None = 没命中任何规则，即「默认表头」那一组）。
            # 列表按它分组排序：见 `templates/weekly_version_diff.html` 的 `updateFileTable`；
            # 写入侧见 `services/weekly_version_logic.generate_weekly_merged_diff`。
            # 用 getattr 而不是直接取属性：这条读路径的输入不只是真行对象，还有测试里
            # 那些手搓的轻量替身 —— 取不到就是「默认表头」，那是正确的缺省，不是错误。
            'header_profile_key': getattr(cache, 'header_profile_key', None),
        })
    return files, authors


def _lazy_header_probe(repository, file_path, commit_id):
    """惰性表头探测器：**只有真配了 `header_detect` 规则的仓库才会去读文件字节**，
    纯路径规则（文件名/目录/正则）一次多余的读都没有。取不到字节按「判据不成立」处理。
    """
    # 延迟 import：vcs_content_service 反过来依赖周版本链路，模块级导入会成环。
    from services.excel_header_probe import make_probe
    from services.vcs_content_service import get_file_content_from_git, get_file_content_from_svn

    cached = {}

    def probe(column, within):
        if 'probe' not in cached:
            raw = None
            if commit_id:
                fetch = (
                    get_file_content_from_svn
                    if getattr(repository, 'type', '') == 'svn'
                    else get_file_content_from_git
                )
                try:
                    raw = fetch(repository, commit_id, file_path)
                except Exception:
                    raw = None
            cached['probe'] = make_probe(raw)
        return cached['probe'](column, within)

    return probe


def _matched_header_profile(repository, file_path, commit_id=None):
    """这个文件命中**用户配的规则**的那套方案；走仓库兜底坐标时返回 None。"""
    from services.excel_header_profiles import resolve_for_file

    if repository is None or not file_path:
        return None
    return resolve_for_file(
        repository, file_path,
        probe=_lazy_header_probe(repository, file_path, commit_id),
        match_only=True,
    )


def resolve_weekly_file_header_profile(repository, file_path, commit_id):
    """这张表命中哪套表头方案的 key（写进缓存表那一列）；走兜底坐标时返回 None。"""
    profile = _matched_header_profile(repository, file_path, commit_id)
    return profile.key if profile is not None else None


def describe_file_header_profile(repository, file_path, commit_id=None):
    """下发给 diff 页的表头方案信息（`{key,label,note}`）；走兜底坐标时返回 None。

    返回 None 时页面**不挂说明** —— 默认表头的文件占绝大多数，给它们也挂一条
    就等于把说明变成噪音。
    """
    profile = _matched_header_profile(repository, file_path, commit_id)
    if profile is None:
        return None
    return {'key': profile.key, 'label': profile.label, 'note': profile.note}


def describe_header_profiles(repository):
    """下发给文件列表的方案清单，**这个顺序就是「第几种表头」**。

    1. `profiles` 里用户配置的方案，按**配置顺序**（列表按它倒序展示：配了 3 种就先显示第三种）；
    2. 被规则引用、但没在 `profiles` 里定义的内置预设，追加在后面
       （`by_key` 会从 `BUILTIN_PROFILES` 兜底取到，所以它们也是有效方案）；
    3. 「默认表头」不在这个清单里 —— 前端把 `header_profile_key` 为空的文件垫在最后。
    """
    config = parse_config(getattr(repository, 'header_profiles', None))
    items = [
        {'key': profile.key, 'label': profile.label, 'note': profile.note}
        for profile in config.profiles
    ]
    seen = {item['key'] for item in items}
    for binding in config.bindings:
        if binding.profile_key in seen:
            continue
        profile = config.by_key(binding.profile_key) or BUILTIN_PROFILES.get(binding.profile_key)
        if profile is None:
            continue
        items.append({'key': profile.key, 'label': profile.label, 'note': profile.note})
        seen.add(profile.key)
    return items
