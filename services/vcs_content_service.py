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
        log_print(f"SVN cat命令: {' '.join(cmd[:2])} [URL和认证信息已隐藏]", 'SVN')
        log_print(f"SVN URL: {svn_url}", 'SVN')
        log_print(f"完整命令参数: {len(cmd)} 个参数", 'SVN')
        log_print(f"调试 - 完整命令: {cmd[:3] + ['[认证信息已隐藏]'] + cmd[7:]}", 'SVN')
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
            log_print(f"文件在提交 {commit_id[:8]} 中不存在: {file_path}", 'GIT')
            return None

    except Exception as e:
        log_print(f"获取Git文件内容失败: {str(e)}", 'GIT', force=True)
        return None


# ---------------------------------------------------------------------------
#  统一差异计算
# ---------------------------------------------------------------------------

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


def get_unified_diff_data(commit, previous_commit=None):
    """使用新的统一差异服务获取差异数据（优化版本，优先使用缓存）"""
    from services.diff_service import DiffService
    from services.excel_diff_cache_service import ExcelDiffCacheService

    excel_cache_service = ExcelDiffCacheService()
    perf_metrics_service = get_perf_metrics_service()
    repository = commit.repository
    perf_project_tags = {
        "project_id": repository.project_id if repository else "",
        "project_code": (repository.project.code if repository and repository.project else ""),
    }
    start_time = time.time()
    try:
        log_print(f"🔧 统一差异服务开始处理: {commit.path}", 'DIFF', force=True)
        log_print(f"📂 当前提交: {commit.commit_id[:8]} | 前一提交: {previous_commit.commit_id[:8] if previous_commit else 'None'}", 'DIFF', force=True)
        # 如果是Excel文件，优先检查缓存
        is_excel = excel_cache_service.is_excel_file(commit.path)
        cache_lookup_start = time.time()
        if is_excel:
            log_print(f"🔍 Excel文件，检查缓存: {commit.path}", 'CACHE')
            # 检查Excel diff缓存
            cached_diff = excel_cache_service.get_cached_diff(
                repository.id, commit.commit_id, commit.path
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
        if previous_commit is None:
            log_print(f"⚠️ 警告: 没有前一提交，将与空版本比较 - 这可能导致显示为初始版本", 'DIFF', force=True)
        # 根据仓库类型获取文件内容
        read_start = time.time()
        if repository.type == 'git':
            # 获取当前版本文件内容
            current_content = get_file_content_from_git(repository, commit.commit_id, commit.path)
            # 获取前一版本文件内容
            previous_content = None
            if previous_commit:
                previous_content = get_file_content_from_git(repository, previous_commit.commit_id, commit.path)
        elif repository.type == 'svn':
            # 获取SVN文件内容
            current_content = get_file_content_from_svn(repository, commit.commit_id, commit.path)
            # 获取前一版本文件内容
            previous_content = None
            if previous_commit:
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
        diff_data = diff_service.process_diff(commit.path, current_content, previous_content)
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
                        previous_commit_id=previous_commit.commit_id if previous_commit else None,
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
