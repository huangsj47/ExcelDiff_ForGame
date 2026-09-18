"""「Excel 版本对比」的两条接口：候选提交列表 + 按需对比。

## 这个功能原先只有一个壳

`templates/commit_diff.html` 的 `#diffCompareModal` 里那个
`<select id="targetCommitSelect">` **从来没有被任何 JS 填过 option**，
`executeDiffCompare()` 的实现是一句 `alert('版本对比功能开发中...')`。
用户看到的就是「选择对比的提交版本: 没有任何可选内容」，点「开始对比」
只弹一个「开发中」。本模块补上它缺的那两条数据链路。

## 候选列表的口径与页面自己的一致

页面正文的「对比版本」由 `resolve_previous_commit(commit, file_commits=...)` 解析，
而 `file_commits` 是「同仓库 + 同文件路径」的全部历史提交（见
`services/commit_diff_view_service.py`）。候选列表用**同一套查询**，只是把结果
下发给前端 —— 两边口径一旦不一致，用户在下拉里选了「页头写着的那一条」却看到
另一份差异。

排序用 `(commit_time DESC, id DESC)` 而不是只按 `commit_time`：同一秒内的多条提交
只按时间排是不稳定的，同一个文件在两个请求里可能给出不同的顺序（仓库里
`resolve_previous_commit` 的同秒兜底就是为这件事写的）。

## 越权：目标提交必须在同一个仓库、同一个文件路径上

对比接口接受一个「另一侧提交」的 id。**只用 id 去取那条提交是不够的** ——
那样任何登录用户都能拿别的仓库 / 别的文件的 commit id 去读它的差异内容。
所以目标提交的查询把 `repository_id` 与 `path` 一起写进 WHERE：取不到就是 404，
不给「存在但无权」与「不存在」两种可区分的响应。

## 缓存：这次对比要不要写缓存

`get_unified_diff_data(commit, previous_commit)` 会把结果写进 Excel diff 数据缓存，
**它的缓存键含基线提交号**（`previous_commit_id=write_baseline`，见该函数 docstring
与 `services/excel_diff_cache_service.py` 头部的「基线」说明）。所以按需对比写下的
是「(提交, 文件, 这次这个基线)」这一组三元组的真实结果，不会污染页面那条链：
页面读缓存时带的是它自己的 `expected_baseline`（见 `services/excel_diff_api_service.py`
里那段被注释写死的约束），两边读的是不同的行。

**这里刻意不碰 HTML 缓存链**（`services/excel_html_cache_service.py`）：
那一带「读缓存必须带 expected_baseline」的约束是因为它曾经读到过别的基线渲染出来的
HTML。对比结果由前端拿 `diff_data` 现场渲染（唯一实现
`static/js/excel_diff_table.js`），请求里不读也不写那份 HTML 缓存。
"""

from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError

from services.api_response_service import json_error, json_success
from utils.timezone_utils import format_beijing_time

# attach_author_display 读库失败时要能兜住：候选人行上没有 author_display 就让前端
# 显示原始 author，不该让整个候选列表 500。
COMMIT_COMPARE_AUTHOR_MAP_ERRORS = (
    RuntimeError,
    TypeError,
    ValueError,
    AttributeError,
    LookupError,
    SQLAlchemyError,
)

# 现算差异这条路上的可预期异常。差异服务要读 VCS 内容、解析 Excel、写缓存，
# 任何一环失败都该变成一条可重试的错误响应，而不是 500 页面。
COMMIT_COMPARE_DIFF_ERRORS = (
    SQLAlchemyError,
    RuntimeError,
    TypeError,
    ValueError,
    AttributeError,
    KeyError,
    OSError,
)

DIRECTION_CURRENT_TO_TARGET = "current_to_target"
DIRECTION_TARGET_TO_CURRENT = "target_to_current"
ALLOWED_DIRECTIONS = (DIRECTION_CURRENT_TO_TARGET, DIRECTION_TARGET_TO_CURRENT)
# 默认方向：**选择的提交是旧侧（基线），当前提交是新侧**。
# 这与本页正文、页头「对比版本」的口径是同一个 —— 用户在下拉里选中
# 「当前正在对比的那一条」时，结果与页面正文逐字相同，不会出现
# 「同一个基线，弹窗里看到的和页面上看到的不是一回事」。
DEFAULT_DIRECTION = DIRECTION_TARGET_TO_CURRENT

# 候选列表最多下发多少条。一个热门配表文件可能有上千条提交，全量下发会让弹窗
# 打开明显变慢，而下拉本身也搜不过来 —— 取最近的一批，多出来的用 truncated
# 标记 + 总数告诉用户「还有更早的，没列出来」。
CANDIDATE_LIMIT = 300
SHORT_COMMIT_ID_LENGTH = 8
MESSAGE_LINE_LIMIT = 80
UNKNOWN_AUTHOR = "未知"


def _author_display(commit):
    """作者显示名：优先映射后的 author_display，回退原始 author。"""
    raw = getattr(commit, "author_display", None) or getattr(commit, "author", None)
    text = str(raw or "").strip()
    return text or UNKNOWN_AUTHOR


def _message_first_line(message):
    """提交信息首行（多行 message 只取第一行，超长截断）。

    下拉里一行放不下整段提交信息；不截断的话一个换行就会把选项撑成两行。
    """
    text = str(message or "").strip()
    if not text:
        return ""
    first_line = text.splitlines()[0].strip()
    if len(first_line) > MESSAGE_LINE_LIMIT:
        return first_line[:MESSAGE_LINE_LIMIT] + "…"
    return first_line


def serialize_compare_commit(commit, *, page_baseline_commit_id=None):
    """一条提交在「版本对比」上下文里的形态。

    `is_page_baseline` 是**页面当前正在对比的那一条**（与
    `resolve_page_previous_commit` 的结果比对），前端据此默认选中它 ——
    否则用户打开弹窗后默认选中的是「最近的一条」，直接点「开始对比」得到的是
    一份和页面正文不同的差异，还以为平台算错了。
    """
    commit_id = str(getattr(commit, "commit_id", "") or "")
    return {
        "id": getattr(commit, "id", None),
        "commit_id": commit_id,
        "short_id": commit_id[:SHORT_COMMIT_ID_LENGTH],
        "version": getattr(commit, "version", None),
        "operation": getattr(commit, "operation", None),
        "commit_time": format_beijing_time(getattr(commit, "commit_time", None)),
        "author": _author_display(commit),
        "message": _message_first_line(getattr(commit, "message", None)),
        "is_page_baseline": bool(
            page_baseline_commit_id
            and commit_id
            and commit_id == str(page_baseline_commit_id)
        ),
    }


def handle_get_commit_compare_candidates(
    *,
    commit_id,
    jsonify,
    Commit,
    ensure_commit_access_or_403,
    resolve_previous_commit,
    attach_author_display,
    log_print,
):
    """列出「同一仓库、同一文件路径」的历史提交，供版本对比下拉使用。"""
    commit = Commit.query.get_or_404(commit_id)
    repository, project = ensure_commit_access_or_403(commit)
    if not getattr(commit, "path", None):
        # 没有文件路径就没有「同一个文件的其它版本」可言 —— 空数组而不是报错，
        # 前端照常渲染成「没有可选版本」。
        return json_success(
            jsonify=jsonify,
            message="该提交没有文件路径，无法列出可对比版本",
            candidates=[],
            total=0,
            truncated=False,
            baseline_commit_id=None,
        )

    # 一次性取全量再切片，**不**在 SQL 里 limit：这份列表还要喂给
    # resolve_previous_commit（它的同秒兜底要按 id 找到当前提交在列表里的位置），
    # 截断过的列表会让兜底算错。页面自己也是这么查的（commit_diff_view_service）。
    file_commits = list(
        Commit.query.filter(
            Commit.repository_id == repository.id,
            Commit.path == commit.path,
        )
        .order_by(Commit.commit_time.desc(), Commit.id.desc())
        .all()
    )

    # 完整列表（含当前这条）交给解析器：它的同秒兜底要按 id 找到当前提交的位置。
    previous_commit = resolve_previous_commit(commit, file_commits=file_commits)
    baseline_commit_id = getattr(previous_commit, "commit_id", None) if previous_commit else None

    candidates = [
        item for item in file_commits if getattr(item, "id", None) != getattr(commit, "id", None)
    ]
    total = len(candidates)
    truncated = total > CANDIDATE_LIMIT
    candidates = candidates[:CANDIDATE_LIMIT]

    try:
        attach_author_display(candidates)
    except COMMIT_COMPARE_AUTHOR_MAP_ERRORS as author_map_error:
        # 作者映射只是显示问题，回退到原始 author 即可（与页面同一条兜底）。
        log_print(f"版本对比候选列表作者姓名映射失败，回退原始作者: {author_map_error}", "DIFF")

    return json_success(
        jsonify=jsonify,
        message="ok",
        commit=serialize_compare_commit(commit),
        baseline_commit_id=baseline_commit_id,
        candidates=[
            serialize_compare_commit(item, page_baseline_commit_id=baseline_commit_id)
            for item in candidates
        ],
        total=total,
        truncated=truncated,
    )


def _resolve_direction(request):
    """解析对比方向；非法值直接落到默认方向（GET 请求，不必为此报错）。"""
    raw = str(request.args.get("direction") or "").strip()
    return raw if raw in ALLOWED_DIRECTIONS else DEFAULT_DIRECTION


def resolve_target_commit(*, raw_target, commit, repository, Commit):
    """按 id 取出目标提交，并确保它属于同一仓库、同一文件路径。

    返回 `(target, error_kind, error_message)`：`target` 非空时另外两项为 None。

    校验写在 SQL 的 WHERE 里而不是「按 id 取出来再比」：取出来再比的话，
    越权与不存在是两条不同的代码路径，很容易在其中一条上漏掉判定；而且
    「按 id 取到了」这件事本身就已经把别的仓库的提交读进了内存。
    """
    text = str(raw_target or "").strip()
    if not text:
        return None, "missing_target_commit", "缺少目标提交参数 target_commit_id"
    try:
        target_id = int(text)
    except (TypeError, ValueError):
        return None, "invalid_target_commit", "target_commit_id 必须是整数"
    if target_id <= 0:
        return None, "invalid_target_commit", "target_commit_id 必须是正整数"
    if target_id == getattr(commit, "id", None):
        return None, "invalid_target_commit", "不能与当前提交自身对比"

    target = Commit.query.filter(
        Commit.id == target_id,
        Commit.repository_id == repository.id,
        Commit.path == commit.path,
    ).first()
    if target is None:
        return None, "target_commit_out_of_scope", "目标提交不属于该仓库的同一个文件"
    return target, None, None


def _empty_result_reason(diff_data):
    """差异载荷「没有可展示内容」时给出人能读懂的原因。

    返回 None 表示载荷可以正常渲染。注意**「两边内容一样」不算空**：
    那种载荷有 sheets，只是每张表的行都没有变更，前端用 `sheetHasChanges`
    判断后会显示「这两个版本之间没有差异」—— 后端这里判的只是「压根没有表」。
    """
    if diff_data is None:
        return "差异服务没有返回结果"
    if not isinstance(diff_data, dict):
        return "差异服务返回了无法识别的结果"
    if diff_data.get("type") == "error":
        return str(diff_data.get("message") or diff_data.get("error") or "差异计算失败")
    if not diff_data.get("sheets"):
        return "这两个版本之间没有可比较的工作表"
    return None


def handle_get_commit_compare_diff(
    *,
    commit_id,
    request,
    jsonify,
    Commit,
    ensure_commit_access_or_403,
    get_unified_diff_data,
    attach_author_display,
    log_print,
):
    """按指定提交现算一份差异（不读、也不写页面那条 HTML 缓存链）。"""
    commit = Commit.query.get_or_404(commit_id)
    repository, project = ensure_commit_access_or_403(commit)

    target, error_kind, error_message = resolve_target_commit(
        raw_target=request.args.get("target_commit_id"),
        commit=commit,
        repository=repository,
        Commit=Commit,
    )
    if error_kind:
        return json_error(
            jsonify=jsonify,
            message=error_message,
            error_type=error_kind,
            # 越权/不存在一律 404，不给出「存在但无权」这个可区分的信号
            http_status=404 if error_kind == "target_commit_out_of_scope" else 400,
        )

    direction = _resolve_direction(request)
    # 方向决定谁当基线（旧侧）：`get_unified_diff_data(新, 旧)` 的第二个参数是基线。
    if direction == DIRECTION_CURRENT_TO_TARGET:
        new_commit, baseline_commit = target, commit
    else:
        new_commit, baseline_commit = commit, target

    try:
        diff_data = get_unified_diff_data(new_commit, baseline_commit)
    except COMMIT_COMPARE_DIFF_ERRORS as exc:
        log_print(f"版本对比计算失败: {getattr(commit, 'path', '')} | {exc}", "DIFF")
        return json_error(
            jsonify=jsonify,
            message=f"版本对比计算失败: {exc}",
            error_type="compare_diff_failed",
            http_status=500,
        )

    empty_reason = _empty_result_reason(diff_data)
    try:
        attach_author_display([commit, target])
    except COMMIT_COMPARE_AUTHOR_MAP_ERRORS as author_map_error:
        log_print(f"版本对比作者姓名映射失败，回退原始作者: {author_map_error}", "DIFF")

    return json_success(
        jsonify=jsonify,
        message=empty_reason or "ok",
        direction=direction,
        # from = 旧侧（基线），to = 新侧。页头写「A（时间）→ B（时间）」，A 是 from。
        from_commit=serialize_compare_commit(baseline_commit),
        to_commit=serialize_compare_commit(new_commit),
        diff_data=diff_data if empty_reason is None else None,
        empty=empty_reason is not None,
        empty_reason=empty_reason,
    )
