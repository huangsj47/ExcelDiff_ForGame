import threading
import concurrent.futures
from datetime import datetime
import time
import git
import re
from services.git_service import GitService
from utils.path_security import build_repository_local_path

class ThreadedGitService(GitService):
    """多线程优化的Git服务，专门优化前一次提交查找性能"""
    
    def __init__(self, repo_url=None, root_directory=None, username=None, token=None, repository=None, active_processes=None, max_workers=6):
        super().__init__(repo_url, root_directory, username, token, repository, active_processes)
        self.max_workers = max_workers
        
    def _collect_previous_commits_threaded(self, repo, commits):
        """多线程版本的前一次提交收集，显著提升性能"""
        try:
            print(f"🚀 [THREADED_GIT] 开始多线程收集前一次提交记录 (工作线程: {self.max_workers})...")
            start_time = time.time()
            
            # 按文件路径分组
            files_commits = {}
            for commit_data in commits:
                file_path = commit_data['path']
                if file_path not in files_commits:
                    files_commits[file_path] = []
                files_commits[file_path].append(commit_data)
            
            additional_commits = []
            thread_lock = threading.Lock()
            
            def process_file_commits(item):
                """处理单个文件的提交记录，查找前一次提交"""
                file_path, file_commits = item
                local_additional_commits = []
                
                try:
                    if not file_commits:
                        return local_additional_commits
                    
                    # 按时间排序，确保我们处理的是最早的提交
                    def get_commit_time(commit):
                        commit_time = commit['commit_time']
                        if isinstance(commit_time, str):
                            # 如果是字符串，尝试解析为datetime
                            try:
                                from datetime import datetime
                                return datetime.fromisoformat(commit_time.replace('Z', '+00:00'))
                            except:
                                # 如果解析失败，返回一个很早的时间
                                return datetime.min
                        return commit_time
                    
                    file_commits.sort(key=get_commit_time, reverse=True)
                    
                    # 获取最早的提交
                    earliest_commit = file_commits[-1]
                    
                    # 查找这个文件的前一次提交，使用线程安全的Git操作
                    try:
                        # 在多线程环境下，使用subprocess直接调用git命令更安全
                        import subprocess
                        git_cmd = [
                            'git', 'log', '--follow',
                            '--format=%H|%an|%ae|%ct|%s',
                            '--max-count=50',
                            '--', file_path
                        ]
                        
                        result = subprocess.run(
                            git_cmd,
                            cwd=repo.working_dir,
                            capture_output=True,
                            text=True,
                            encoding='utf-8',  # 明确指定UTF-8编码
                            errors='ignore',   # 忽略编码错误
                            timeout=30  # 30秒超时
                        )
                        
                        if result.returncode != 0:
                            print(f"⚠️ [THREADED_GIT] Git命令执行失败 {file_path}: {result.stderr}")
                            return local_additional_commits
                            
                        log_output = result.stdout
                        
                    except Exception as git_error:
                        print(f"⚠️ [THREADED_GIT] Git log命令失败 {file_path}: {git_error}")
                        return local_additional_commits
                        
                    if log_output:
                        log_lines = log_output.strip().split('\n')
                        
                        # 找到当前最早提交在历史中的位置
                        earliest_commit_index = -1
                        for i, line in enumerate(log_lines):
                            if line.startswith(earliest_commit['commit_id']):
                                earliest_commit_index = i
                                break
                        
                        # 如果找到了，并且还有更早的提交
                        if earliest_commit_index >= 0 and earliest_commit_index < len(log_lines) - 1:
                            prev_line = log_lines[earliest_commit_index + 1]
                            parts = prev_line.split('|')
                            if len(parts) >= 5:
                                prev_commit_id = parts[0]
                                prev_author = parts[1]
                                prev_author_email = parts[2]
                                prev_timestamp = int(parts[3])
                                prev_message = '|'.join(parts[4:])
                                
                                # 检查这个前一次提交是否已经存在（需要线程安全检查）
                                with thread_lock:
                                    already_exists = any(
                                        c['commit_id'] == prev_commit_id and c['path'] == file_path 
                                        for c in commits + additional_commits
                                    )
                                
                                if not already_exists:
                                    # 确定操作类型，简化逻辑避免线程安全问题
                                    operation = 'M'  # 默认为修改
                                    # 在多线程环境下，避免复杂的commit对象操作
                                    # 简单判断：如果是8位commit ID，通常表示修改操作
                                    
                                    local_additional_commits.append({
                                        'commit_id': prev_commit_id,
                                        'path': file_path,
                                        'version': prev_commit_id[:8],
                                        'operation': operation,
                                        'author': prev_author,
                                        'author_email': prev_author_email,
                                        'commit_time': datetime.fromtimestamp(prev_timestamp),
                                        'message': prev_message.strip()
                                    })
                                    
                                    print(f"📝 [THREADED_GIT] 线程找到前一次提交: {file_path} -> {prev_commit_id[:8]}")
                
                except Exception as e:
                    print(f"⚠️ [THREADED_GIT] 处理文件 {file_path} 时出错: {e}")
                
                return local_additional_commits
            
            # 使用线程池并行处理
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                # 提交所有任务
                future_to_file = {
                    executor.submit(process_file_commits, item): item[0] 
                    for item in files_commits.items()
                }
                
                # 收集结果，使用更健壮的超时机制
                completed_count = 0
                total_tasks = len(future_to_file)
                
                try:
                    # 使用as_completed迭代器，但添加整体超时控制
                    for future in concurrent.futures.as_completed(future_to_file, timeout=60):
                        completed_count += 1
                        file_path = future_to_file[future]
                        
                        try:
                            result = future.result(timeout=10)  # 单个任务结果获取超时
                            with thread_lock:
                                additional_commits.extend(result)
                            print(f"✅ [THREADED_GIT] 完成处理文件 {file_path} ({completed_count}/{total_tasks})")
                        except concurrent.futures.TimeoutError:
                            print(f"⚠️ [THREADED_GIT] 处理文件 {file_path} 超时 ({completed_count}/{total_tasks})")
                        except Exception as e:
                            print(f"⚠️ [THREADED_GIT] 处理文件 {file_path} 失败: {e} ({completed_count}/{total_tasks})")
                            
                except concurrent.futures.TimeoutError:
                    print(f"⚠️ [THREADED_GIT] 整体处理超时，已完成 {completed_count}/{total_tasks} 个任务")
                    # 取消所有未完成的任务
                    for future in future_to_file:
                        if not future.done():
                            future.cancel()
                            file_path = future_to_file[future]
                            print(f"🚫 [THREADED_GIT] 取消未完成任务: {file_path}")
                except Exception as e:
                    print(f"❌ [THREADED_GIT] 任务收集过程中出现异常: {e}")
                    # 取消所有未完成的任务
                    for future in future_to_file:
                        if not future.done():
                            future.cancel()
            
            end_time = time.time()
            processing_time = end_time - start_time
            
            print(f"✅ [THREADED_GIT] 多线程收集完成!")
            print(f"📊 [THREADED_GIT] 处理文件数: {len(files_commits)}")
            print(f"📊 [THREADED_GIT] 找到前一次提交: {len(additional_commits)}")
            print(f"⏱️ [THREADED_GIT] 处理耗时: {processing_time:.2f}秒")
            print(f"🚀 [THREADED_GIT] 平均每文件: {(processing_time/len(files_commits)*1000):.1f}ms")
            
            return commits + additional_commits
            
        except Exception as e:
            print(f"❌ [THREADED_GIT] 多线程收集前一次提交失败: {e}")
            # 降级到原始方法
            print("🔄 [THREADED_GIT] 降级到串行处理...")
            return super()._collect_previous_commits(repo, commits)
    
    def _get_commits_base_threaded(self, since_date=None, limit=100):
        """多线程版本的基础提交记录获取方法"""
        try:
            import os
            if not os.path.exists(self.local_path):
                success, message = self.clone_or_update_repository()
                if not success:
                    return []
            
            # 配置Git仓库以处理编码
            repo = git.Repo(self.local_path)
            # 设置Git配置以处理中文编码
            try:
                repo.git.config('core.quotepath', 'false')
                repo.git.config('i18n.commitencoding', 'utf-8')
                repo.git.config('i18n.logoutputencoding', 'utf-8')
            except:
                pass  # 忽略配置错误
            
            commits = []
            
            # 获取指定分支的提交记录
            branch = repo.heads[self.repository.branch] if self.repository.branch in [h.name for h in repo.heads] else repo.head
            
            # 构建iter_commits参数
            iter_kwargs = {'max_count': limit}
            if since_date:
                iter_kwargs['since'] = since_date
                print(f"🔍 [THREADED_GIT] 增量同步，从 {since_date} 开始获取最多 {limit} 个提交")
            else:
                print(f"🔍 [THREADED_GIT] 全量同步，获取最多 {limit} 个提交")
            
            commit_iter = repo.iter_commits(branch, **iter_kwargs)
            
            processed_count = 0
            for commit in commit_iter:
                # 确保不超过limit限制
                if processed_count >= limit:
                    print(f"🔍 [THREADED_GIT] 已达到限制数量 {limit}，停止获取")
                    break
                processed_count += 1
                try:
                    # 验证提交对象是否有效
                    _ = commit.hexsha
                    _ = commit.author.name
                    _ = commit.message
                except Exception as commit_error:
                    print(f"⚠️ [THREADED_GIT] 跳过无效提交: {commit_error}")
                    continue
                
                # 过滤提交人
                if self.repository.commit_filter:
                    filter_emails = [email.strip() for email in self.repository.commit_filter.split(',')]
                    if commit.author.email in filter_emails:
                        continue
                
                # 过滤日志
                if self.repository.log_filter_regex:
                    import re
                    if re.match(self.repository.log_filter_regex, commit.message):
                        continue
                
                # 获取文件变更 - 使用diff来正确检测操作类型
                if commit.parents:
                    # 有父提交，比较差异
                    try:
                        parent = commit.parents[0]
                        # 验证父提交是否有效
                        try:
                            _ = parent.hexsha
                            _ = parent.tree
                        except Exception as parent_error:
                            print(f"⚠️ [THREADED_GIT] 提交 {commit.hexsha[:8]} 的父提交无效: {parent_error}")
                            raise Exception(f"父提交无效: {parent_error}")
                        
                        diffs = parent.diff(commit)
                    except Exception as diff_error:
                        print(f"⚠️ [THREADED_GIT] 提交 {commit.hexsha[:8]} diff比较失败: {diff_error}")
                        # 使用stats作为备选方案
                        try:
                            for file_path in commit.stats.files:
                                if self.repository.path_regex:
                                    import re
                                    if not re.search(self.repository.path_regex, file_path):
                                        continue
                                
                                commits.append({
                                    'commit_id': commit.hexsha,
                                    'path': file_path,
                                    'version': commit.hexsha[:8],
                                    'operation': 'M',  # 默认为修改
                                    'author': commit.author.name,
                                    'author_email': commit.author.email,
                                    'commit_time': datetime.fromtimestamp(commit.committed_date),
                                    'message': commit.message.strip()
                                })
                        except Exception as stats_error:
                            print(f"⚠️ [THREADED_GIT] 提交 {commit.hexsha[:8]} stats获取也失败: {stats_error}")
                        continue
                    
                    for diff in diffs:
                        file_path = diff.b_path or diff.a_path
                        
                        # 路径过滤
                        if self.repository.path_regex:
                            import re
                            if not re.search(self.repository.path_regex, file_path):
                                continue
                        
                        # 确定操作类型
                        if diff.new_file:
                            operation = 'A'  # 新增
                        elif diff.deleted_file:
                            operation = 'D'  # 删除
                        else:
                            operation = 'M'  # 其他情况默认为修改
                        
                        commits.append({
                            'commit_id': commit.hexsha,
                            'path': file_path,
                            'version': commit.hexsha[:8],
                            'operation': operation,
                            'author': commit.author.name,
                            'author_email': commit.author.email,
                            'commit_time': datetime.fromtimestamp(commit.committed_date),
                            'message': commit.message.strip()
                        })
                else:
                    # 初始提交，所有文件都是新增
                    for item in commit.stats.files:
                        file_path = item
                        
                        # 路径过滤
                        if self.repository.path_regex:
                            import re
                            if not re.search(self.repository.path_regex, file_path):
                                continue
                        
                        commits.append({
                            'commit_id': commit.hexsha,
                            'path': file_path,
                            'version': commit.hexsha[:8],
                            'operation': 'A',  # 初始提交都是新增
                            'author': commit.author.name,
                            'author_email': commit.author.email,
                            'commit_time': datetime.fromtimestamp(commit.committed_date),
                            'message': commit.message.strip()
                        })
            
            print(f"🔍 [THREADED_GIT] 基础提交记录获取完成，共 {len(commits)} 个记录")
            return commits
            
        except Exception as e:
            print(f"❌ [THREADED_GIT] 基础提交记录获取失败: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    def get_commits_threaded(self, since_date=None, limit=100):
        """多线程优化版本的获取提交记录方法"""
        try:
            print(f"🔍 [THREADED_GIT] 开始获取提交记录 (多线程优化版本)...")
            
            # 直接使用多线程版本获取基础提交记录，不调用父类方法
            commits = self._get_commits_base_threaded(since_date, limit)
            
            if not commits:
                print(f"🔍 [THREADED_GIT] 没有找到提交记录")
                return []
            
            print(f"🔍 [THREADED_GIT] 获取到 {len(commits)} 个基础提交记录")
            
            # 确保有repo对象，安全获取仓库信息
            if not hasattr(self, '_repo_obj'):
                import os
                try:
                    project_code = self.repository.project.code
                    repository_name = self.repository.name
                    repository_id = self.repository.id
                    repo_path = build_repository_local_path(
                        project_code,
                        repository_name,
                        repository_id,
                        strict=False
                    )
                    self._repo_obj = git.Repo(repo_path)
                except Exception as session_error:
                    print(f"❌ [THREADED_GIT] 获取仓库信息失败: {session_error}")
                    print(f"🔄 Git操作因会话问题退出，不影响后续操作")
                    return []
            
            # 使用多线程版本收集前一次提交
            commits = self._collect_previous_commits_threaded(self._repo_obj, commits)
            
            print(f"🔍 [THREADED_GIT] 多线程提交记录获取完成，共 {len(commits)} 个记录")
            return commits
            
        except Exception as e:
            print(f"❌ [THREADED_GIT] 获取提交记录失败: {e}")
            print(f"🔄 Git操作异常，退出当前操作，不影响后续处理")
            # 如果是会话问题，直接返回空列表，不尝试降级
            if "not bound to a Session" in str(e):
                return []
            print(f"🔄 [THREADED_GIT] 降级到单线程处理...")
            # 降级到原始方法
            try:
                return super().get_commits(since_date, limit)
            except Exception as fallback_error:
                print(f"❌ [THREADED_GIT] 降级处理也失败: {fallback_error}")
                print(f"🔄 Git操作完全失败，退出当前操作")
                return []
    
    def get_commits(self, since_date=None, limit=100):
        """重写父类的get_commits方法，使用多线程版本"""
        return self.get_commits_threaded(since_date, limit)
    
    def _collect_previous_commits(self, repo, commits):
        """重写父类的_collect_previous_commits方法，使用多线程版本"""
        return self._collect_previous_commits_threaded(repo, commits)
    
    def get_file_commit_history(self, file_path, limit=100):
        """获取指定文件的提交历史"""
        try:
            import os
            from datetime import datetime, timezone

            if not os.path.exists(self.local_path):
                print(f"❌ [THREADED_GIT] 仓库路径不存在: {self.local_path}")
                return []

            repo = git.Repo(self.local_path)

            # 获取文件的提交历史
            commits_data = []
            try:
                # 使用git log --follow来跟踪文件重命名
                commits = list(repo.iter_commits(paths=file_path, max_count=limit))

                for commit in commits:
                    # 检查文件在这个提交中的状态
                    operation = 'M'  # 默认为修改

                    # 检查是否是新增文件
                    if len(commit.parents) == 0:
                        # 初始提交
                        operation = 'A'
                    else:
                        # 检查文件在父提交中是否存在
                        parent = commit.parents[0]
                        try:
                            parent.tree[file_path]
                            # 文件在父提交中存在，检查是否被删除
                            try:
                                commit.tree[file_path]
                                operation = 'M'  # 修改
                            except KeyError:
                                operation = 'D'  # 删除
                        except KeyError:
                            # 文件在父提交中不存在
                            try:
                                commit.tree[file_path]
                                operation = 'A'  # 新增
                            except KeyError:
                                continue  # 文件在这个提交中也不存在，跳过

                    # 转换提交时间
                    commit_time = datetime.fromtimestamp(commit.committed_date, tz=timezone.utc)

                    commit_data = {
                        'commit_id': commit.hexsha,
                        'author': commit.author.name,
                        'commit_time': commit_time,
                        'message': commit.message.strip(),
                        'operation': operation
                    }

                    commits_data.append(commit_data)

                print(f"✅ [THREADED_GIT] 获取到文件 {file_path} 的 {len(commits_data)} 个提交记录")
                return commits_data

            except git.exc.GitCommandError as e:
                print(f"❌ [THREADED_GIT] Git命令执行失败: {e}")
                return []

        except Exception as e:
            print(f"❌ [THREADED_GIT] 获取文件提交历史失败: {e}")
            import traceback
            traceback.print_exc()
            return []

    def get_commits_in_date_range_base(self, repository, start_date, end_date, path_regex=None):
        """基础提交记录获取方法（不包含前一次提交收集）"""
        try:
            import os
            # 安全获取仓库信息，避免SQLAlchemy会话问题
            try:
                project_code = repository.project.code
                repository_name = repository.name
                repository_id = repository.id
            except Exception as session_error:
                print(f"❌ [THREADED_GIT] 获取仓库信息失败: {session_error}")
                print(f"🔄 Git操作因会话问题退出，不影响后续操作")
                return []

            repo_path = build_repository_local_path(
                project_code,
                repository_name,
                repository_id,
                strict=False
            )
            repo = git.Repo(repo_path)
            repository._repo_obj = repo  # 缓存repo对象供后续使用
            
            print(f"🔍 [THREADED_GIT] 获取 {start_date} 到 {end_date} 的提交记录...")
            
            # 获取指定日期范围内的提交
            since_date = start_date.strftime('%Y-%m-%d')
            until_date = end_date.strftime('%Y-%m-%d')
            
            commits = []
            
            # 使用git log获取提交记录
            for commit in repo.iter_commits(since=since_date, until=until_date):
                # 获取该提交涉及的文件
                if commit.parents:
                    # 有父提交，比较差异
                    parent = commit.parents[0]
                    diffs = parent.diff(commit)
                else:
                    # 初始提交，所有文件都是新增
                    diffs = commit.diff(git.NULL_TREE)
                
                for diff in diffs:
                    file_path = diff.b_path or diff.a_path
                    if not file_path:
                        continue
                    
                    # 应用路径过滤
                    if path_regex and not re.match(path_regex, file_path):
                        continue
                    
                    # 确定操作类型
                    if diff.new_file:
                        operation = 'A'  # 新增
                    elif diff.deleted_file:
                        operation = 'D'  # 删除
                    else:
                        operation = 'M'  # 修改
                    
                    commits.append({
                        'commit_id': commit.hexsha,
                        'path': file_path,
                        'version': commit.hexsha[:8],
                        'operation': operation,
                        'author': commit.author.name,
                        'author_email': commit.author.email,
                        'commit_time': datetime.fromtimestamp(commit.committed_date),
                        'message': commit.message.strip()
                    })
            
            print(f"📊 [THREADED_GIT] 找到 {len(commits)} 个基础提交记录")
            return commits
            
        except Exception as e:
            print(f"❌ [THREADED_GIT] 获取基础提交记录失败: {e}")
            return []

# 性能测试和对比函数
def performance_test_previous_commits(repository, commits_sample):
    """性能测试：对比串行和多线程版本的性能"""
    print("🧪 [性能测试] 开始对比串行vs多线程前一次提交查找性能...")
    
    # 确保repository有_repo_obj属性
    if not hasattr(repository, '_repo_obj'):
        import os
        repo_path = build_repository_local_path(
            repository.project.code,
            repository.name,
            repository.id,
            strict=False
        )
        repository._repo_obj = git.Repo(repo_path)
    
    # 测试串行版本
    serial_service = GitService(
        repo_url=repository.url,
        root_directory=getattr(repository, 'root_directory', None),
        username=getattr(repository, 'username', None),
        token=getattr(repository, 'token', None),
        repository=repository
    )
    start_time = time.time()
    serial_result = serial_service._collect_previous_commits(repository._repo_obj, commits_sample)
    serial_time = time.time() - start_time
    
    # 测试多线程版本
    threaded_service = ThreadedGitService(
        repo_url=repository.url,
        root_directory=getattr(repository, 'root_directory', None),
        username=getattr(repository, 'username', None),
        token=getattr(repository, 'token', None),
        repository=repository,
        max_workers=6
    )
    start_time = time.time()
    threaded_result = threaded_service._collect_previous_commits_threaded(repository._repo_obj, commits_sample)
    threaded_time = time.time() - start_time
    
    # 输出对比结果
    print(f"📊 [性能对比] 串行版本耗时: {serial_time:.2f}秒")
    print(f"📊 [性能对比] 多线程版本耗时: {threaded_time:.2f}秒")
    print(f"🚀 [性能提升] 速度提升: {(serial_time/threaded_time):.1f}x")
    print(f"📈 [性能提升] 时间节省: {((serial_time-threaded_time)/serial_time*100):.1f}%")
    
    return {
        'serial_time': serial_time,
        'threaded_time': threaded_time,
        'speedup': serial_time/threaded_time,
        'time_saved_percent': (serial_time-threaded_time)/serial_time*100
    }
