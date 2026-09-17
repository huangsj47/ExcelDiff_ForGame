#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VCS 内容获取服务 - 从 app.py 拆分
包含 Git/SVN 文件内容获取、服务实例缓存、统一差异计算
"""

import json
import os
import threading
import time

from services.deployment_mode import is_agent_dispatch_mode
from utils.logger import log_print
from services.performance_metrics_service import get_perf_metrics_service

# ---------------------------------------------------------------------------
#  Git / SVN 服务实例缓存
# ---------------------------------------------------------------------------

# 全局Git服务缓存，避免重复创建实例
_git_service_cache = {}
_git_service_lock = threading.Lock()

# 全局SVN服务缓存，避免重复创建实例
_svn_service_cache = {}
_svn_service_lock = threading.Lock()

# active_git_processes 引用，由 app.py 在初始化时注入
_active_git_processes = None


def _is_agent_dispatch_mode() -> bool:
    return is_agent_dispatch_mode()


def configure_vcs_service(active_git_processes_ref):
    """配置 VCS 服务模块，注入 active_git_processes 引用"""
    global _active_git_processes
    _active_git_processes = active_git_processes_ref


def get_git_service(repository):
    """获取Git服务实例（使用缓存避免重复创建）"""
    cache_key = f"{repository.id}_{repository.url}"
    with _git_service_lock:
        if cache_key not in _git_service_cache:
            from services.threaded_git_service import ThreadedGitService
            _git_service_cache[cache_key] = ThreadedGitService(
                repository.url, repository.root_directory,
                repository.username, repository.token,
                repository, _active_git_processes
            )
            log_print(f"🔧 创建新的Git服务实例: {repository.name}", 'GIT')
        return _git_service_cache[cache_key]


def get_svn_service(repository):
    """获取SVN服务实例（使用缓存避免重复创建）"""
    cache_key = f"{repository.id}_{repository.url}"
    with _svn_service_lock:
        if cache_key not in _svn_service_cache:
            from services.svn_service import SVNService
            _svn_service_cache[cache_key] = SVNService(repository)
            log_print(f"🔧 创建新的SVN服务实例: {repository.name}", 'SVN')
        return _svn_service_cache[cache_key]


# ---------------------------------------------------------------------------
#  文件内容获取
# ---------------------------------------------------------------------------

def get_file_content_from_svn(repository, commit_id, file_path):
    """从SVN仓库获取指定提交的文件内容"""
    try:
        svn_service = get_svn_service(repository)
        # SVN的commit_id格式为r12345，需要提取数字部分
        revision = commit_id
        if revision.startswith('r'):
            revision = revision[1:]
        log_print(f"获取SVN文件内容: {file_path}@{revision}", 'SVN')
        # 确保本地仓库存在
        if not os.path.exists(svn_service.local_path):
            if _is_agent_dispatch_mode():
                log_print(
                    "platform/agent 模式：禁止平台本地 checkout SVN 仓库，请由 Agent 节点提供数据",
                    'SVN',
                    force=True,
                )
                return None
            success, message = svn_service.checkout_or_update_repository()
            if not success:
                log_print(f"SVN仓库检出失败: {message}", 'SVN', force=True)
                return None

        # 使用本地工作目录的相对路径，与SVN服务的现有方法保持一致
        # 将绝对路径转换为相对路径
        relative_path = file_path
        if file_path.startswith('/trunk/ProjectMecury/RawData/'):
            # 去掉SVN路径前缀，只保留实际的文件路径
            relative_path = file_path[len('/trunk/ProjectMecury/RawData/'):]
        elif file_path.startswith('/trunk/'):
            # 去掉开头的/trunk/部分，因为本地工作目录已经是trunk
            relative_path = file_path[7:]  # 去掉'/trunk/'
        elif file_path.startswith('/'):
            # 去掉开头的/
            relative_path = file_path[1:]
        log_print(f"原始路径: {file_path}", 'SVN')
        log_print(f"转换后相对路径: {relative_path}", 'SVN')
        # 使用SVN cat命令获取文件内容
        import subprocess
        # 构建正确的SVN URL，避免路径重复
        from urllib.parse import urlparse, quote
        parsed_url = urlparse(repository.url)
        repo_path = parsed_url.path  # /svn/trunk/ProjectMecury/RawData
        # 从file_path中去掉与repo_path重复的部分
        if file_path.startswith('/trunk/ProjectMecury/RawData/'):
            # 只保留相对于仓库根目录的路径
            relative_file_path = file_path[len('/trunk/ProjectMecury/RawData/'):]
            # 对中文文件名进行URL编码
            encoded_file_path = quote(relative_file_path, safe='/')
            svn_url = f"{repository.url}/{encoded_file_path}@{revision}"
        else:
            # 如果路径格式不符合预期，直接拼接
            encoded_file_path = quote(file_path, safe='/')
            svn_url = f"{repository.url}{encoded_file_path}@{revision}"
        cmd = [svn_service.svn_executable, 'cat', svn_url]
        # 安全获取认证信息，避免SQLAlchemy会话问题
        try:
            username = getattr(repository, 'username', None)
            password = getattr(repository, 'password', None)
            if username and password:
                cmd.extend(['--username', username, '--password', password])
        except Exception as session_error:
            log_print(f"✗ 获取SVN认证信息失败: {session_error}", 'SVN', force=True)
            log_print(f"🔄 SVN操作因会话问题退出，不影响后续操作", 'SVN')
            return None

        # 添加非交互模式参数
        cmd.extend(['--non-interactive', '--trust-server-cert'])
        from utils.security_utils import redact_command_args
        log_print(f"SVN cat命令: {redact_command_args(cmd)}", 'SVN')
        log_print(f"SVN URL: {svn_url}", 'SVN')
        log_print(f"完整命令参数: {len(cmd)} 个参数", 'SVN')
        log_print(f"调试 - 完整命令: {redact_command_args(cmd)}", 'SVN')
        try:
            # SVN cat命令不需要工作目录，直接使用完整URL
            # 设置环境变量确保使用UTF-8编码
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            env['LC_ALL'] = 'en_US.UTF-8'
            result = subprocess.run(cmd, capture_output=True, text=False, timeout=30, cwd=None, env=env)
            if result.returncode == 0:
                # 直接返回二进制内容，不进行文本解码
                log_print(f"✅ SVN文件内容获取成功: {len(result.stdout)} 字节", 'SVN')
                return result.stdout  # 返回原始bytes格式

            else:
                error_msg = svn_service._decode_subprocess_output(result.stderr)
                log_print(f"❌ SVN文件内容获取失败: {error_msg}", 'SVN', force=True)
                return None

        except subprocess.TimeoutExpired:
            log_print("❌ SVN cat命令超时", 'SVN', force=True)
            return None

    except Exception as e:
        log_print(f"❌ 获取SVN文件内容异常: {str(e)}", 'SVN', force=True)
        return None


def get_file_content_from_git(repository, commit_id, file_path):
    """从Git仓库获取指定提交的文件内容"""
    try:
        import git
        # 使用缓存的GitService实例
        git_service = get_git_service(repository)
        log_print(f"检查本地路径: {git_service.local_path}", 'GIT')
        log_print(f"路径是否存在: {os.path.exists(git_service.local_path)}", 'GIT')
        if not os.path.exists(git_service.local_path):
            if _is_agent_dispatch_mode():
                log_print(
                    "platform/agent 模式：禁止平台本地 clone Git 仓库，请由 Agent 节点提供数据",
                    'GIT',
                    force=True,
                )
                return None
            success, message = git_service.clone_or_update_repository()
            if not success:
                log_print(f"仓库克隆失败: {message}", 'GIT', force=True)
                return None

        repo = git.Repo(git_service.local_path)
        # 尝试获取完整的commit ID
        try:
            # 如果commit_id是短SHA，尝试获取完整SHA
            if len(commit_id) < 40:
                # 使用Git命令直接解析短SHA，避免遍历所有提交
                try:
                    full_sha = repo.git.rev_parse(commit_id)
                    commit_id = full_sha
                    log_print(f"短SHA解析成功: {commit_id[:8]} -> {full_sha[:8]}", 'GIT')
                except Exception as parse_e:
                    log_print(f"短SHA解析失败，尝试有限遍历: {parse_e}", 'GIT')
                    # 只遍历最近1000个提交，避免卡死
                    commits = list(repo.iter_commits(max_count=1000))
                    for c in commits:
                        if c.hexsha.startswith(commit_id):
                            commit_id = c.hexsha
                            log_print(f"在最近1000个提交中找到匹配: {commit_id[:8]}", 'GIT')
                            break

                    else:
                        log_print(f"在最近1000个提交中未找到匹配的短SHA: {commit_id}", 'GIT', force=True)
            commit = repo.commit(commit_id)
        except Exception as e:
            log_print(f"无法找到commit {commit_id}: {e}", 'GIT')
            # 尝试fetch最新数据
            if _is_agent_dispatch_mode():
                log_print(
                    "platform/agent 模式：禁止平台本地 fetch Git 远端，返回空内容",
                    'GIT',
                    force=True,
                )
                return None
            try:
                repo.remotes.origin.fetch()
                commit = repo.commit(commit_id)
            except Exception as e2:
                log_print(f"fetch后仍无法找到commit: {e2}", 'GIT', force=True)
                return None

        try:
            blob = commit.tree[file_path]
            return blob.data_stream.read()

        except KeyError:
            # 文件在基线里叫别的名字（提交做了改名：`git diff-tree -M` 会把它记成
            # R0xx "旧名" -> "新名"）。这时按新路径取内容是空的，而空内容会被上层
            # 当成「上一版什么都没有」——整份表被渲染成「新增工作表」。
            # 线上实例 6140：一次改名，真实差异只有 5 格，页面写「257 行全部新增」。
            renamed_from = _find_rename_source(repo, commit_id, file_path)
            if renamed_from:
                try:
                    blob = commit.tree[renamed_from]
                    log_print(
                        f"文件在 {commit_id[:8]} 中是改名前的 {renamed_from}，按旧路径取内容: {file_path}",
                        'GIT', force=True,
                    )
                    return blob.data_stream.read()
                except KeyError:
                    log_print(f"改名来源在 {commit_id[:8]} 中也取不到: {renamed_from}", 'GIT')
            log_print(f"文件在提交 {commit_id[:8]} 中不存在: {file_path}", 'GIT')
            return None

    except Exception as e:
        log_print(f"获取Git文件内容失败: {str(e)}", 'GIT', force=True)
        return None


# ---------------------------------------------------------------------------
#  统一差异计算
# ---------------------------------------------------------------------------

# 「调用方没交 previous_commit」与「调用方明确说这条提交没有基线（None）」是
# 两件事，用哨兵区分（语义与 services/excel_diff_cache_service.py 的
# BASELINE_UNSET 一一对应）：
#   * 没传   → 读缓存时不指定基线，让服务自己解析权威基线（老调用方行为不变）；
#   * 传 None → 就是要「和空版本比」（新增文件）那一行。
PREVIOUS_COMMIT_UNSET = object()


def _find_rename_source(repo, commit_id, file_path):
    """`file_path` 在 `commit_id` 里不存在时，找出它改名前的路径。

    配表里「改名」很常见（`【40】怪物表_Object_物件.xlsx` → 加个「——废弃」后缀之类）。
    改名之后按新路径去基线版本取内容会取到空 —— 而空内容会被上层当成「上一版什么都
    没有」，整份表被渲染成「新增工作表」。线上 6140 就是这样：一次改名，真实差异
    只有 5 个单元格，页面写的是「257 行全部新增」。

    两条路都试：
    1. `git log --follow` 从这一版往前追 —— 这一版自己就是改名那次提交时直接命中；
    2. 基线在改名**之前**时上面那条追不到（往前追看不到未来的改名），于是先找
       「新增了这个路径」的那次提交（关掉改名检测，改名看起来就是一次新增），
       再在那次提交上开改名检测把 `R0xx 旧名 新名` 里的旧名读出来。

    只往前追一跳：连续多次改名时给出的是一跳之前的名字，仍然取不到就如实返回 None。
    """
    def _parse(output):
        tokens = output.split('\x00')
        for index, token in enumerate(tokens):
            status = token.strip()
            if len(status) >= 2 and status[0] in ('R', 'C') and status[1:].isdigit():
                if index + 2 < len(tokens) and tokens[index + 2] == file_path:
                    return tokens[index + 1] or None
        return None

    try:
        direct = repo.git.log('--follow', '--name-status', '-M', '-z', '-1',
                              '--format=%H', commit_id, '--', file_path)
        source = _parse(direct)
        if source:
            return source
    except Exception as exc:
        log_print(f"查询改名来源失败(顺历史): {file_path} | {exc}", 'GIT')

    try:
        added_by = repo.git.log('--no-renames', '--diff-filter=A', '--format=%H',
                                '-1', 'HEAD', '--', file_path).strip()
        if not added_by:
            return None
        status = repo.git.diff_tree('-r', '-M', '--name-status', '--no-commit-id',
                                    '-z', added_by)
        return _parse(status)
    except Exception as exc:
        log_print(f"查询改名来源失败(找新增提交): {file_path} | {exc}", 'GIT')
        return None


def get_deleted_file_diff_data(commit, previous_commit):
    """整份文件被删除时的差异数据：把**基线版本的每一张工作表**渲染成「已删除」。

    调用方必须已经确认「这份文件在这个提交里被删除了」（`commit.operation == 'D'`）。
    这里不再自己判断删除，也不通过「当前内容为空」来推断 —— 见
    `DiffService.process_deleted_file` 的说明。

    返回 None 表示**取不到基线的字节**（仓库没同步、路径对不上、git 出错）。
    这种时候上层必须显示「无法获取差异」，不能退化成空差异。
    """
    from services.diff_service import DiffService
    from services.excel_diff_cache_service import ExcelDiffCacheService

    if commit is None or not _has_previous_commit(previous_commit):
        return None
    repository = commit.repository
    excel_cache_service = ExcelDiffCacheService()
    baseline_id = previous_commit.commit_id
    try:
        cached_diff = excel_cache_service.get_cached_diff(
            repository.id, commit.commit_id, commit.path, previous_commit_id=baseline_id
        )
        if cached_diff:
            cached_payload = json.loads(cached_diff.diff_data)
            if cached_payload.get("sheets"):
                log_print(f"✅ 删除文件的差异命中缓存: {commit.path}", 'CACHE')
                return cached_payload
            # 缓存里一行内容都没有 —— 对「删除提交」来说这份载荷等于什么都没有。
            # 历史缺陷：通用路径（get_unified_diff_data）不知道这是删除，算出的是
            # 「没有工作表的 excel」，接口把它回成「没有找到Excel工作表数据」，
            # 后台任务还会把它写进**同一把缓存键**；页面随后读缓存拿到这份空载荷，
            # 被删掉的内容就再也显示不出来（线上实测：同一批删除提交里，先被接口
            # 访问过的那几条页面全空，没被访问过的正常渲染）。当作未命中去重算。
            log_print(
                f"⚠️ 删除文件的缓存里没有工作表内容，忽略并重算: {commit.path}", 'CACHE', force=True
            )

        previous_content = None
        if repository.type == 'git':
            previous_content = get_file_content_from_git(repository, baseline_id, commit.path)
        elif repository.type == 'svn':
            previous_content = get_file_content_from_svn(repository, baseline_id, commit.path)
        if not previous_content:
            log_print(
                f"⚠️ 删除文件取不到基线内容，无法展示被删内容: {commit.path} "
                f"| 基线={str(baseline_id)[:8]}",
                'DIFF', force=True
            )
            return None

        diff_data = DiffService().process_deleted_file(commit.path, previous_content)
        if diff_data and diff_data.get('sheets'):
            excel_cache_service.save_cached_diff(
                repository_id=repository.id,
                commit_id=commit.commit_id,
                file_path=commit.path,
                diff_data=diff_data,
                processing_time=0.0,
                previous_commit_id=baseline_id,
                commit_time=commit.commit_time,
            )
        return diff_data
    except Exception as exc:
        log_print(f"❌ 生成删除文件差异失败: {commit.path} | {exc}", 'DIFF', force=True)
        return None


def _has_previous_commit(previous_commit):
    """调用方是否真的交了一个用来做对比的提交（而不是 None / 没传）。"""
    return previous_commit is not None and previous_commit is not PREVIOUS_COMMIT_UNSET


def _collect_excel_metrics(diff_data):
    metrics = {'sheet_count': 0, 'changed_rows': 0, 'summary': {}}
    if not isinstance(diff_data, dict) or diff_data.get('type') != 'excel':
        return metrics
    try:
        sheets = diff_data.get('sheets') or {}
        metrics['sheet_count'] = len(sheets)
        metrics['changed_rows'] = sum(
            len((sheet or {}).get('rows') or [])
            for sheet in sheets.values()
        )
        metrics['summary'] = diff_data.get('summary') or {}
    except Exception:
        pass
    return metrics


def get_unified_diff_data(commit, previous_commit=PREVIOUS_COMMIT_UNSET):
    """使用新的统一差异服务获取差异数据（优化版本，优先使用缓存）

    previous_commit 是**这次比较的基线**，必须同时喂给缓存的读与写：缓存键包含
    previous_commit_id（见 excel_diff_cache_service 头部「基线」注释块），读的
    时候不传、让服务去解析「权威基线」，而写的时候用真实基线落库，两边就会**各
    说各话** —— 请求「c3 对 c1」的区间比较会直接命中先前「c3 对 c2」留下的缓存行，
    返回的内容是 c2 的，不报错、也不重算（本文件历史缺陷）。
    """
    from services.diff_service import DiffService
    from services.excel_diff_cache_service import BASELINE_UNSET, ExcelDiffCacheService

    excel_cache_service = ExcelDiffCacheService()
    perf_metrics_service = get_perf_metrics_service()
    repository = commit.repository
    has_previous = _has_previous_commit(previous_commit)
    # 读缓存要校验的基线：真的传了提交就精确匹配它的 commit_id；明确传 None 就是
    # 要 previous_commit_id IS NULL 那一行；没传才退回「服务自解析」。
    read_baseline = (
        BASELINE_UNSET if previous_commit is PREVIOUS_COMMIT_UNSET
        else (previous_commit.commit_id if has_previous else None)
    )
    # 写缓存按**实际比较对象**落库：没传/传 None 都是与空版本比 → 落 NULL。
    # 这里绝不能跟着 read_baseline 去自解析权威基线，否则会把「与空版本比」的
    # 结果冒充成「与权威基线比」的结果，反过来污染权威基线那一行。
    write_baseline = previous_commit.commit_id if has_previous else None
    # 删除提交必须走删除自己的路径。
    #
    # 通用路径不知道「这个文件已经没了」：它拿不到当前内容，算出来的是一个
    # **没有工作表的 excel 载荷**。接口把这份空载荷回成「没有找到Excel工作表数据」，
    # 后台任务还会把它写进缓存（键与页面完全相同）；页面随后读缓存拿到它，
    # 删除前的内容就再也显示不出来。线上实测：同一批删除提交里，先被接口访问过的
    # 那几条页面全空，没被访问过的正常渲染出全部被删行。
    if getattr(commit, "operation", None) == "D" and has_previous:
        deleted_diff = get_deleted_file_diff_data(commit, previous_commit)
        if deleted_diff and deleted_diff.get("sheets"):
            return deleted_diff
    perf_project_tags = {
        "project_id": repository.project_id if repository else "",
        "project_code": (repository.project.code if repository and repository.project else ""),
    }
    start_time = time.time()
    try:
        log_print(f"🔧 统一差异服务开始处理: {commit.path}", 'DIFF', force=True)
        log_print(f"📂 当前提交: {commit.commit_id[:8]} | 前一提交: {previous_commit.commit_id[:8] if has_previous else 'None'}", 'DIFF', force=True)
        # 如果是Excel文件，优先检查缓存
        is_excel = excel_cache_service.is_excel_file(commit.path)
        cache_lookup_start = time.time()
        if is_excel:
            log_print(f"🔍 Excel文件，检查缓存: {commit.path}", 'CACHE')
            # 检查Excel diff缓存 —— 必须带上本次的基线，不能交给服务自解析
            cached_diff = excel_cache_service.get_cached_diff(
                repository.id, commit.commit_id, commit.path, previous_commit_id=read_baseline
            )
            if cached_diff:
                cache_time = time.time() - start_time
                log_print(f"✅ 缓存命中，跳过实时计算: {commit.path} | 耗时: {cache_time:.2f}秒", 'CACHE')
                perf_metrics_service.record(
                    "unified_excel_diff",
                    success=True,
                    metrics={"total_ms": cache_time * 1000},
                    tags={
                        "source": "cache_hit",
                        "repository_id": repository.id,
                        "project_id": perf_project_tags["project_id"],
                        "project_code": perf_project_tags["project_code"],
                        "file_path": commit.path,
                    },
                )
                return json.loads(cached_diff.diff_data)

            else:
                log_print(f"❌ 缓存未命中，开始实时计算: {commit.path}", 'CACHE')
                log_print(f"⏱️ 缓存查询耗时: {time.time() - cache_lookup_start:.2f}秒", 'DIFF')
        # 如果没有前一提交，这可能是问题所在
        if not has_previous:
            log_print("⚠️ 警告: 没有前一提交，将与空版本比较 - 这可能导致显示为初始版本", 'DIFF', force=True)
        # 根据仓库类型获取文件内容
        read_start = time.time()
        if repository.type == 'git':
            # 获取当前版本文件内容
            current_content = get_file_content_from_git(repository, commit.commit_id, commit.path)
            # 获取前一版本文件内容
            previous_content = None
            if has_previous:
                previous_content = get_file_content_from_git(repository, previous_commit.commit_id, commit.path)
        elif repository.type == 'svn':
            # 获取SVN文件内容
            current_content = get_file_content_from_svn(repository, commit.commit_id, commit.path)
            # 获取前一版本文件内容
            previous_content = None
            if has_previous:
                previous_content = get_file_content_from_svn(repository, previous_commit.commit_id, commit.path)
        else:
            log_print(f"❌ 不支持的仓库类型: {repository.type}", 'DIFF', force=True)
            return {
                'type': 'error',
                'file_path': commit.path,
                'error': f'不支持的仓库类型: {repository.type}',
                'message': f'不支持的仓库类型: {repository.type}'
            }
        read_time = time.time() - read_start
        # 处理差异
        diff_service = DiffService()
        calc_start_time = time.time()
        # 关键列（Repository.key_columns，列号从 1 开始）决定「怎么认同一行」。
        # 帮助文档已经写明按它匹配新旧版本的同一行，但引擎历史上没读过这个配置，
        # 只按前 3 列相似度猜配对 —— 会把不同的行配成一条「修改」。
        diff_data = diff_service.process_diff(
            commit.path, current_content, previous_content,
            key_columns=getattr(repository, 'key_columns', None))
        processing_time = time.time() - calc_start_time
        if diff_data:
            total_time = time.time() - start_time
            log_print(f"✅ 实时diff计算完成: {commit.path} | 类型: {diff_data.get('type', 'unknown')} | 计算耗时: {processing_time:.2f}秒 | 总耗时: {total_time:.2f}秒", 'DIFF')
            log_print(
                f"📊 diff分段耗时: read={read_time:.2f}s, calc={processing_time:.2f}s | "
                f"content_bytes(current={len(current_content or b'')}, previous={len(previous_content or b'')})",
                'DIFF'
            )
            # 如果是Excel文件且没有缓存，保存到缓存
            if is_excel and diff_data.get('type') == 'excel':
                try:
                    cache_save_start = time.time()
                    excel_cache_service.save_cached_diff(
                        repository_id=repository.id,
                        commit_id=commit.commit_id,
                        file_path=commit.path,
                        diff_data=diff_data,  # 传递原始对象，不要预先JSON编码
                        processing_time=processing_time,
                        file_size=0,
                        previous_commit_id=write_baseline,
                        commit_time=commit.commit_time
                    )
                    cache_save_time = time.time() - cache_save_start
                    metrics = _collect_excel_metrics(diff_data)
                    log_print(f"💾 Excel diff结果已保存到缓存: {commit.path}", 'CACHE')
                    log_print(
                        f"📈 Excel diff指标: sheets={metrics['sheet_count']}, rows={metrics['changed_rows']}, "
                        f"summary={metrics['summary']} | save_cache={cache_save_time:.2f}s",
                        'DIFF'
                    )
                    perf_metrics_service.record(
                        "unified_excel_diff",
                        success=True,
                        metrics={
                            "total_ms": total_time * 1000,
                            "read_ms": read_time * 1000,
                            "diff_ms": processing_time * 1000,
                            "save_cache_ms": cache_save_time * 1000,
                            "changed_rows": metrics["changed_rows"],
                            "sheet_count": metrics["sheet_count"],
                        },
                        tags={
                            "source": "realtime_excel",
                            "repository_id": repository.id,
                            "project_id": perf_project_tags["project_id"],
                            "project_code": perf_project_tags["project_code"],
                            "file_path": commit.path,
                        },
                    )
                except Exception as cache_error:
                    log_print(f"⚠️ 保存缓存失败: {cache_error}", 'CACHE')
                    perf_metrics_service.record(
                        "unified_excel_diff",
                        success=False,
                        metrics={
                            "total_ms": total_time * 1000,
                            "read_ms": read_time * 1000,
                            "diff_ms": processing_time * 1000,
                        },
                        tags={
                            "source": "realtime_excel_save_cache_failed",
                            "repository_id": repository.id,
                            "project_id": perf_project_tags["project_id"],
                            "project_code": perf_project_tags["project_code"],
                            "file_path": commit.path,
                        },
                    )
            else:
                perf_metrics_service.record(
                    "unified_excel_diff",
                    success=True,
                    metrics={
                        "total_ms": total_time * 1000,
                        "read_ms": read_time * 1000,
                        "diff_ms": processing_time * 1000,
                    },
                    tags={
                        "source": "realtime_non_excel",
                        "repository_id": repository.id,
                        "project_id": perf_project_tags["project_id"],
                        "project_code": perf_project_tags["project_code"],
                        "file_path": commit.path,
                    },
                )
        else:
            total_time = time.time() - start_time
            log_print(f"❌ 实时diff计算失败: {commit.path} | 耗时: {total_time:.2f}秒", 'DIFF', force=True)
            perf_metrics_service.record(
                "unified_excel_diff",
                success=False,
                metrics={"total_ms": total_time * 1000},
                tags={
                    "source": "diff_data_empty",
                    "repository_id": repository.id,
                    "project_id": perf_project_tags["project_id"],
                    "project_code": perf_project_tags["project_code"],
                    "file_path": commit.path,
                },
            )
        return diff_data

    except Exception as e:
        total_time = time.time() - start_time if 'start_time' in locals() else 0
        log_print(f"❌ 统一差异服务错误: {e} | 耗时: {total_time:.2f}秒", 'DIFF', force=True)
        perf_metrics_service.record(
            "unified_excel_diff",
            success=False,
            metrics={"total_ms": total_time * 1000},
            tags={
                "source": "exception",
                "repository_id": repository.id if repository else "",
                "project_id": perf_project_tags["project_id"],
                "project_code": perf_project_tags["project_code"],
                "file_path": commit.path if commit else "",
            },
        )
        return None
