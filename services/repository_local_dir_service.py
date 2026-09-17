# -*- coding: utf-8 -*-
"""仓库改名的本地工作副本目录迁移。

## 为什么需要这个模块

仓库的本地路径**没有存在库里**，而是每次按当前名字实时算出来的
（`utils/path_security.py:build_repository_local_path` 的
`{project}_{sanitize(repo)}_{id}`）。`services/repository_update_form_service.py`
改名时只写 `repository.name`，于是：

* 平台下一次读这个仓库时算出的路径与盘上的目录**不是同一个**；
* 该仓库被判成「未克隆」，重新 clone 一份；
* 旧目录成为孤儿，里面任何只存在于本地的状态静默失联。

中文名放大了触发面（`abc` → `配置表` 会改变 sanitize 结果，中文名之间互改则不会），
但根因是既有缺陷，跟中文无关 —— ASCII 改名一样会中招。

## 为什么是 os.rename 而不是 shutil.move

两个路径**必定在同一个基目录下**：`build_repository_local_path` 把新旧都锚到同一个
base，而 `_sanitize_segment` 不可能产出路径分隔符（只保留 `[A-Za-z0-9._-]`），
所以父目录永远等于 base。同卷 → `os.rename` 是原子的。

`shutil.move` 在 `os.rename` 失败时会退化成「复制 + 删源」，那正是本模块要防的
半成功状态：复制到一半失败会同时留下两个半份副本，且耗时不可控。
（`agent/handlers/auto_sync.py` 那条既有迁移用 `shutil.move` 并「失败就回退用旧目录」，
是因为后台同步任务不能失败；改名是交互式的、用户可以重试，所以这里取更严的策略。）

## 为什么失败就放弃改名

库里的名字和盘上的目录是同一个身份的两半。改名成功而目录没动，不是「多留了一个旧
文件夹」，而是平台**立刻**换了目录工作、并重新 clone 一份。本仓库一贯的原则是
「静默的不一致比报错更难查」（见 `utils/runtime_paths.py` 的 docstring）：
「改名没成功，但你知道为什么、可以重试」严格优于「改名成功了，平台在另一个目录上
悄悄干活」。
"""

import os

from utils.path_security import build_repository_local_path

# 迁移结果的原因码。调用方据此决定要不要拦住这次改名，不要靠解析 message。
REASON_SAME_PATH = "same_path"            # 新旧路径相同 —— 无事可做，不是错误
REASON_SOURCE_MISSING = "source_missing"  # 还没有工作副本 —— 没什么可迁移的
REASON_MOVED = "moved"                    # 迁移完成
REASON_TARGET_EXISTS = "target_exists"    # 目标已存在 —— 拦住
REASON_FAILED = "failed"                  # rename 抛异常 —— 拦住

# 需要拦住改名的两种原因。其余都是「可以做，只是没做事」。
BLOCKING_REASONS = frozenset({REASON_TARGET_EXISTS, REASON_FAILED})

TARGET_EXISTS_MESSAGE = (
    "本地工作副本目录迁移失败：目标目录已存在（{new_path}）。"
    "为避免覆盖或留下孤儿副本，本次重命名未保存。请确认并清理该目录后重试。"
)
FAILED_MESSAGE = (
    "本地工作副本目录迁移失败：{error}。"
    "该目录可能正被同步/克隆任务占用，本次重命名未保存。请等待相关任务结束后重试。"
)


def relocate_repository_local_dir(*, project_code, old_name, new_name, repository_id):
    """把工作副本从旧名字对应的目录搬到新名字对应的目录。

    返回 dict：`{"reason", "old_path", "new_path", "error", "message"}`。
    `reason in BLOCKING_REASONS` 时调用方**必须**放弃这次改名并 rollback。

    两个路径都**不传 base_dir** —— 必须与所有读取方（`services/git_service.py`、
    `services/svn_service.py`、`services/threaded_git_service.py`、
    `services/repository_admin_handlers.py`）算出同一个路径。传了 base 就会静默
    算出另一个位置，那正是本模块要修的问题本身。
    """
    old_path = build_repository_local_path(project_code, old_name, repository_id, strict=False)
    new_path = build_repository_local_path(project_code, new_name, repository_id, strict=False)
    result = {"old_path": old_path, "new_path": new_path, "error": None, "message": ""}

    # 中文 → 中文的改名会走到这里：两个名字都 sanitize 成 fallback "repository"，
    # 路径一模一样。这是最常见的情形，必须是无副作用的 no-op。
    if old_path == new_path:
        result["reason"] = REASON_SAME_PATH
        return result

    if not os.path.isdir(old_path):
        # 还没克隆过（或已被删）。没有东西会被孤儿化，改名照常进行。
        # 这条也顺带避免了误报：旧目录不存在时谈不上「孤儿副本」。
        result["reason"] = REASON_SOURCE_MISSING
        return result

    if os.path.exists(new_path):
        # 目标位置已经有一份工作副本。三种收场都不能选：
        # 直接删掉目标＝在表单 POST 里递归删工作副本（本仓库的删除是带
        # pending_deletions 记录的后台流程，不该在这里顺手做）；采用目标、
        # 丢掉旧目录＝凭空制造「同一仓库两份副本，平台悄悄挑一个」；
        # 把旧目录改名成 *.renamed_<ts> 保留＝同样是在两份之间悄悄换权威。
        # 所以拦住，把决定权交回用户。
        result["reason"] = REASON_TARGET_EXISTS
        result["message"] = TARGET_EXISTS_MESSAGE.format(new_path=new_path)
        return result

    try:
        os.rename(old_path, new_path)
    except OSError as exc:
        result["reason"] = REASON_FAILED
        result["error"] = exc
        result["message"] = FAILED_MESSAGE.format(error=exc)
        return result

    # 验证后置条件。没有证据就不报成功 —— 与 utils/runtime_paths.py 同一条原则：
    # 报告一个没被证实的结果，比报告失败更难排查。
    if os.path.isdir(new_path) and not os.path.isdir(old_path):
        result["reason"] = REASON_MOVED
    else:
        result["reason"] = REASON_FAILED
        result["error"] = "迁移后校验未通过（新旧路径同时存在或都不存在）"
        result["message"] = FAILED_MESSAGE.format(error=result["error"])
    return result


def undo_repository_local_dir_move(*, old_path, new_path):
    """commit 失败时把目录搬回去。返回 (是否成功, 追加给用户的说明)。"""
    try:
        if os.path.isdir(new_path) and not os.path.exists(old_path):
            os.rename(new_path, old_path)
            return True, "（本地目录已回退）"
    except OSError as exc:
        return False, (
            f"（警告：本地目录未能回退：{exc}。"
            f"工作副本现在位于 {new_path}，实际使用前请手工改回 {old_path}）"
        )
    return False, (
        f"（警告：本地目录未能回退。工作副本现在位于 {new_path}，"
        f"实际使用前请手工改回 {old_path}）"
    )
