"""Helpers for deleted-file handling in weekly Excel diff views."""

from __future__ import annotations

import json
from html import escape
from typing import Any
from urllib.parse import quote

from services.commit_lookup_service import find_commit_by_commit_id
from services.excel_header_profiles import header_kwargs_for


def is_deleted_operation(operation: Any) -> bool:
    op = str(operation or "").strip().upper()
    return op in {"D", "DEL", "DELETE", "DELETED", "REMOVE", "REMOVED"}


def resolve_primary_operation(operations: Any) -> str:
    """周版本列表里给文件上色的操作类型：跟**窗口内的最终状态**走。

    `operations` 是该文件在窗口内按提交时间升序的操作序列（见
    `commit_diff_logic.generate_merged_diff_data`）。旧口径是「序列里出现过 D 就
    标红」，于是一个**被删掉又建回来**的文件在列表里是红的「删除文件」，
    而它的 diff 页（比对窗口首末两版）显示的是新增内容 —— 列表与页面互相打脸。
    线上 奖励模式_CfgRewardMode.xlsx 就是：A→D→A→M→M，列表标红，
    页面却是 5 行新增。

    现在的口径：

    * 最后一次操作是删除 → `D`（文件在窗口结束时确实不存在，与 diff 页的
      「已删除」判定同源，见 `resolve_weekly_deleted_excel_state`）；
    * 首次操作是新增（且没被删掉）→ `A`（窗口内新建、且仍然存在）；
    * 其余 → `M`。
    """
    if not operations:
        return "M"
    try:
        ops = [str(op or "").strip().upper() for op in operations]
    except TypeError:
        return "M"
    if is_deleted_operation(ops[-1]):
        return "D"
    if ops[0] == "A":
        return "A"
    return "M"


def resolve_weekly_deleted_excel_state(
    *,
    commit_model,
    config,
    diff_cache,
    file_path: str,
) -> tuple[bool, str | None]:
    """Judge whether the weekly Excel result ended in deleted state."""
    latest_commit = None
    repository_id = getattr(config, "repository_id", None)
    if not repository_id:
        repository_id = getattr(getattr(config, "repository", None), "id", None)

    if repository_id and getattr(diff_cache, "latest_commit_id", None):
        try:
            latest_commit = (
                commit_model.query.filter(
                    commit_model.repository_id == repository_id,
                    commit_model.path == file_path,
                    commit_model.commit_id == diff_cache.latest_commit_id,
                )
                .order_by(commit_model.commit_time.desc(), commit_model.id.desc())
                .first()
            )
        except Exception:
            latest_commit = None

    if latest_commit and is_deleted_operation(getattr(latest_commit, "operation", None)):
        previous_commit = None
        try:
            previous_commit = (
                commit_model.query.filter(
                    commit_model.repository_id == repository_id,
                    commit_model.path == file_path,
                    commit_model.commit_time < latest_commit.commit_time,
                )
                .order_by(commit_model.commit_time.desc(), commit_model.id.desc())
                .first()
            )
            if previous_commit is None:
                previous_commit = (
                    commit_model.query.filter(
                        commit_model.repository_id == repository_id,
                        commit_model.path == file_path,
                        commit_model.commit_time == latest_commit.commit_time,
                        commit_model.id < latest_commit.id,
                    )
                    .order_by(commit_model.id.desc())
                    .first()
                )
        except Exception:
            previous_commit = None

        previous_commit_id = (
            previous_commit.commit_id if previous_commit and previous_commit.commit_id else diff_cache.base_commit_id
        )
        return True, previous_commit_id

    if getattr(diff_cache, "merged_diff_data", None):
        try:
            merged_payload = json.loads(diff_cache.merged_diff_data)
            if isinstance(merged_payload, dict):
                operations = merged_payload.get("operations")
                commit_ids = merged_payload.get("commit_ids")
                if isinstance(operations, list) and operations and is_deleted_operation(operations[-1]):
                    previous_commit_id = None
                    if isinstance(commit_ids, list) and len(commit_ids) >= 2:
                        previous_commit_id = commit_ids[-2]
                    if not previous_commit_id:
                        previous_commit_id = diff_cache.base_commit_id
                    return True, previous_commit_id
        except Exception:
            pass

    return False, None


def previous_version_url(
    *,
    commit_model,
    url_for,
    config,
    file_path: str,
    previous_commit_id: str | None,
) -> str | None:
    """「删除前的上一版」链接：优先指向那条提交的 diff 页，取不到就退回按路径+版本号取内容的页面。

    文件名是仓库里的路径（不可信），进 query 必须 urlencode —— 见
    `render_deleted_file_content` 里的同一条说明。
    """
    if not previous_commit_id:
        return None
    previous_commit_str = str(previous_commit_id).strip()
    if not previous_commit_str:
        return None
    previous_url = None
    # 按 commit_id 找回那条提交 —— 口径（短 SHA 才是前缀、SVN 修订号只精确匹配）
    # 在 services/commit_lookup_service.py 里，两处共用。原先这里自己写了一套
    # 「长度 >= 40 就精确、否则 like」，SVN 的 `r1` 会前缀匹配上 `r19`。
    previous_commit = find_commit_by_commit_id(
        commit_model,
        repository_id=config.repository_id,
        file_path=file_path,
        commit_id=previous_commit_str,
    )

    if previous_commit:
        try:
            previous_url = url_for(
                "commit_diff_with_path",
                project_code=config.project.code,
                repository_name=config.repository.name,
                commit_id=previous_commit.id,
            )
        except Exception:
            try:
                previous_url = url_for("commit_diff", commit_id=previous_commit.id)
            except Exception:
                previous_url = None

    if not previous_url:
        encoded_file_path = quote(file_path or "", safe="")
        previous_url = (
            f"/weekly-version-config/{config.id}/file-previous-version"
            f"?file_path={encoded_file_path}&commit_id={quote(previous_commit_str, safe='')}"
        )
    return previous_url


def build_deleted_excel_payload(*, file_path: str, previous_content: bytes | None, diff_service,
                                key_columns=None, header_rows=None, header_name_row=None,
                                marker_column=None):
    """删除前那一版的字节 → 「整份删除」载荷（每张表、每一行都是删除行）。

    与提交页同一条路（`services/vcs_content_service.py::get_deleted_file_diff_data`）：
    `DiffService.process_deleted_file` 要求调用方**显式声明**这是删除，它不接受靠
    「当前内容为空」推断 —— 读文件失败同样是空内容，渲染成「全表删除」等于让评审者
    把一次读取失败当成一次真实的删除确认掉。所以这里只在真拿到基线字节时建载荷；
    拿不到（或建出来的载荷一张表都没有）就返回 None，由上层退回「已删除」提示。

    header_rows 是仓库配的「表头行数」：删除态也要把表头行从「删除 N 行」里分出来，
    否则一张三行表头的表被删掉时，计数里会多出两行表头。

    header_name_row 是仓库配的「名称行」：删除态的表头块同样按它取列名，
    否则同一张表在「改了一格」与「整份删除」两种提交里会显示两套列头。

    marker_column 是标记列（`services/excel_header_profiles.py`）：删除态也要按它
    把那一列排除掉，否则同一列备注在「改了一格」时不算变更、在「整份删除」时
    被算成 N 行变更。
    """
    if not previous_content:
        return None
    try:
        payload = diff_service.process_deleted_file(
            file_path, previous_content, key_columns=key_columns, header_rows=header_rows,
            header_name_row=header_name_row, marker_column=marker_column)
    except Exception:
        return None
    if not isinstance(payload, dict) or not payload.get("sheets"):
        return None
    return payload


def render_deleted_excel_content(
    *,
    commit_model,
    url_for,
    config,
    file_path: str,
    previous_commit_id: str | None,
    payload,
    render_excel_html,
) -> str:
    """周版本里被删掉的 Excel：提示条 + **删除前的完整内容**（全部按删除行渲染）。

    为什么不能只给一句「Excel文件已删除」：改名在配表里是按「删旧名 + 加新名」记的，
    评审者在周版本列表里看到一条「已删除」、旁边又冒出一个内容几乎相同的新文件，
    却看不到被删的到底是什么 —— 无法判断这是一次改名还是真的删掉了一张表。
    非 Excel 的删除早就是这个口径了（`render_deleted_file_content` 会列出删除的行），
    提交页的删除提交也是（`get_deleted_file_diff_data` 整表渲染成删除行）。
    """
    safe_file_name = escape((file_path or "").split("/")[-1] or file_path or "该文件")
    summary = payload.get("summary") if isinstance(payload, dict) else None
    removed = (summary or {}).get("removed")
    sheet_count = len(payload.get("sheets") or {}) if isinstance(payload, dict) else 0
    stats_html = ""
    if isinstance(removed, int):
        stats_html = (
            f"<div class='mt-1 small'>共 {sheet_count} 张工作表、{removed} 行，下面全部按「删除行」显示。</div>"
        )
    previous_commit_str = str(previous_commit_id or "").strip()
    link = previous_version_url(
        commit_model=commit_model,
        url_for=url_for,
        config=config,
        file_path=file_path,
        previous_commit_id=previous_commit_id,
    )
    reference = f"删除前（{escape(previous_commit_str[:8])}）" if previous_commit_str else "删除前"
    link_html = ""
    if link:
        link_html = (
            "<div class='mt-1 small text-muted'>"
            f"也可以查看 <a href='{link}' class='alert-link' target='_blank'>上一个版本 ({escape(previous_commit_str[:8])})</a> "
            "来对照整个文件。"
            "</div>"
        )
    return (
        "<div class='excel-deleted-with-content'>"
        "<div class='alert alert-warning mb-3'>"
        "<i class='bi bi-trash me-2'></i>"
        "<strong>Excel文件已删除</strong>"
        f"<div class='mt-1 small'>{safe_file_name} 在该周版本中已被删除；下面是{reference}的完整内容。</div>"
        f"{stats_html}"
        f"{link_html}"
        "</div>"
        + render_excel_html(payload, file_path)
        + "</div>"
    )


def read_deleted_excel_baseline(
    *,
    repository,
    file_path: str,
    previous_commit_id: str | None,
    readers: dict,
    log_print=None,
) -> bytes | None:
    """按仓库类型读出「删除前那一版」的字节；读不到返回 None。

    为什么基线只能用「删除它的那条提交之前的那一版」：周版本 diff 缓存里删除态文件的
    `base_commit_id` 是空的（线上实测接口回 `base_commit_info: null`），本周期没有可用的
    窗口基线 —— 这一点由 `resolve_weekly_deleted_excel_state` 返回的 previous_commit_id
    兜住，删除提示里链接的也是它。
    """
    if not previous_commit_id:
        return None
    kind = str(getattr(repository, "type", "git") or "git").lower()
    reader = readers.get(kind) or readers.get("git")
    if reader is None:
        return None
    try:
        return reader(repository, previous_commit_id, file_path)
    except Exception as exc:
        if log_print:
            log_print(f"⚠️ 周版本删除文件读取基线内容失败: {file_path} | {exc}", "WEEKLY", force=True)
        return None


def render_weekly_deleted_excel(
    *,
    commit_model,
    url_for,
    config,
    file_path: str,
    previous_commit_id: str | None,
    repository,
    readers: dict,
    diff_service,
    render_excel_html,
    render_notice=None,
    log_print=None,
) -> str:
    """周版本里被删掉的 Excel 的正文：能取到基线字节就整份渲染删除前的内容，否则退回提示条。

    两条分支都必须**明说**自己是哪一条（日志里能区分「渲染了被删内容」与「只给了提示」）：
    线上就是只给提示这一条，评审者看到「一张表被删了」，却无从判断它是改名还是真删。
    """
    previous_content = read_deleted_excel_baseline(
        repository=repository,
        file_path=file_path,
        previous_commit_id=previous_commit_id,
        readers=readers,
        log_print=log_print,
    )
    payload = build_deleted_excel_payload(
        file_path=file_path, previous_content=previous_content, diff_service=diff_service,
        **header_kwargs_for(repository, file_path, raw=previous_content),
    )
    if payload:
        if log_print:
            log_print(f"周版本Excel文件已删除，渲染删除前的内容: {file_path}", "WEEKLY")
        return render_deleted_excel_content(
            commit_model=commit_model,
            url_for=url_for,
            config=config,
            file_path=file_path,
            previous_commit_id=previous_commit_id,
            payload=payload,
            render_excel_html=render_excel_html,
        )
    if log_print:
        log_print(
            f"周版本Excel文件已删除，返回删除提示: {file_path} | 基线={str(previous_commit_id or '')[:8]}",
            "WEEKLY",
            force=True,
        )
    if render_notice is not None:
        # 调用方注入的提示渲染器是它自己的薄包装（形如 (config, file_path, previous_commit_id)），
        # 由它去补 commit_model / url_for 这些运行时依赖。
        return render_notice(config=config, file_path=file_path, previous_commit_id=previous_commit_id)
    return render_weekly_deleted_excel_notice(
        commit_model=commit_model,
        url_for=url_for,
        config=config,
        file_path=file_path,
        previous_commit_id=previous_commit_id,
    )


def render_weekly_deleted_excel_notice(
    *,
    commit_model,
    url_for,
    config,
    file_path: str,
    previous_commit_id: str | None,
) -> str:
    """Render deleted-file notice HTML with optional previous version shortcut."""
    safe_file_name = escape((file_path or "").split("/")[-1] or file_path or "该文件")
    previous_html = ""
    previous_url = previous_version_url(
        commit_model=commit_model,
        url_for=url_for,
        config=config,
        file_path=file_path,
        previous_commit_id=previous_commit_id,
    )
    if previous_url:
        previous_commit_str = str(previous_commit_id).strip()
        previous_html = (
            "<hr>"
            "<p class='mb-0'>"
            "<small class='text-muted'>"
            f"可以查看 <a href='{previous_url}' class='alert-link' target='_blank'>上一个版本 ({escape(previous_commit_str[:8])})</a> "
            "来查看删除前的Excel内容。"
            "</small>"
            "</p>"
        )

    return (
        "<div class='p-4 text-center'>"
        "<div class='alert alert-warning mb-4'>"
        "<i class='bi bi-trash fs-1 mb-3 d-block text-warning'></i>"
        "<h5 class='alert-heading'>Excel文件已删除</h5>"
        f"<p class='mb-0'>{safe_file_name} 在该周版本中已被删除。</p>"
        f"{previous_html}"
        "</div>"
        "</div>"
    )
