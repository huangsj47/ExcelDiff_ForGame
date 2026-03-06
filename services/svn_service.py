import os
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
import tempfile
import shutil
from utils.path_security import build_repository_local_path
from services.model_loader import get_runtime_models

# 延迟导入数据库模块，避免循环导入
def get_db_models():
    """获取数据库模块，避免循环导入"""
    try:
        db, Commit = get_runtime_models("db", "Commit")
        return db, Commit
    except Exception:
        return None, None

class SVNService:
    def __init__(self, repository):
        self.repository = repository

        # 缓存仓库信息，避免后续SQLAlchemy会话问题
        try:
            self.repository_id = repository.id
            self.repository_name = repository.name
            self.repository_url = repository.url
            self.repository_username = getattr(repository, 'username', None)
            self.repository_password = getattr(repository, 'password', None)
            self.repository_token = getattr(repository, 'token', None)
            self.repository_current_version = getattr(repository, 'current_version', None)
            self.repository_commit_filter = getattr(repository, 'commit_filter', None)
            self.repository_log_filter_regex = getattr(repository, 'log_filter_regex', None)
            self.repository_path_regex = getattr(repository, 'path_regex', None)

            # 使用统一的安全路径构建，确保路径位于repos目录内
            self.local_path = build_repository_local_path(
                repository.project.code,
                repository.name,
                repository.id,
                strict=False
            )
        except Exception as e:
            from utils.safe_print import log_print
            log_print(f"⚠️ SVN服务初始化时缓存仓库信息失败: {e}", 'SVN', force=True)
            # 设置默认值
            self.repository_id = None
            self.repository_name = 'unknown'
            self.repository_url = ''
            self.repository_username = None
            self.repository_password = None
            self.repository_token = None
            self.repository_current_version = None
            self.repository_commit_filter = None
            self.repository_log_filter_regex = None
            self.repository_path_regex = None
            self.local_path = build_repository_local_path('unknown', 'unknown', 0, strict=False)

        # 设置SVN可执行文件路径
        self.svn_executable = self._find_svn_executable()

    def _find_svn_executable(self):
        """查找SVN可执行文件路径"""
        # 首先尝试系统PATH中的svn命令
        try:
            result = subprocess.run(['svn', '--version'],
                                  capture_output=True, text=False, timeout=5)
            if result.returncode == 0:
                from utils.safe_print import log_print
                log_print("找到SVN可执行文件: svn (系统PATH)", 'SVN')
                return 'svn'
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            # 系统PATH中没有svn命令，继续查找其他路径
            pass

        # 如果系统PATH中没有svn，则查找常见的SVN安装路径
        possible_paths = [
            'C:\\Program Files\\TortoiseSVN\\bin\\svn.exe',
            'C:\\Program Files (x86)\\TortoiseSVN\\bin\\svn.exe',
            'C:\\Program Files\\SlikSvn\\bin\\svn.exe',
            'C:\\Program Files (x86)\\SlikSvn\\bin\\svn.exe',
            'C:\\Program Files\\CollabNet\\Subversion Client\\svn.exe',
            'C:\\Program Files (x86)\\CollabNet\\Subversion Client\\svn.exe'
        ]

        for path in possible_paths:
            try:
                # 测试SVN命令是否可用
                result = subprocess.run([path, '--version'],
                                      capture_output=True, text=False, timeout=5)
                if result.returncode == 0:
                    from utils.safe_print import log_print
                    log_print(f"找到SVN可执行文件: {path}", 'SVN')
                    return path
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                continue

        # 如果都找不到，返回默认值并记录错误
        from utils.safe_print import log_print
        log_print("❌ 未找到SVN可执行文件，请安装SVN客户端", 'SVN', force=True)
        return 'svn'  # 返回默认值，让后续操作报错时提供更明确的错误信息

    def _decode_subprocess_output(self, byte_output):
        """安全解码subprocess输出，处理编码问题"""
        if not byte_output:
            return ""

        # 尝试多种编码方式
        encodings = ['utf-8', 'gbk', 'cp936', 'latin1']

        for encoding in encodings:
            try:
                return byte_output.decode(encoding)
            except UnicodeDecodeError:
                continue

        # 如果所有编码都失败，使用错误忽略模式
        return byte_output.decode('utf-8', errors='ignore')

    def _build_auth_args(self):
        """构建SVN认证与非交互参数。"""
        args = []
        username = self.repository_username or getattr(self.repository, 'username', None)
        password = self.repository_password or getattr(self.repository, 'password', None)
        if username and password:
            args.extend(['--username', username, '--password', password])
        args.extend(['--non-interactive', '--trust-server-cert'])
        return args

    def _run_svn_cleanup(self):
        from utils.safe_print import log_print

        cleanup_cmd = [self.svn_executable, 'cleanup', self.local_path] + self._build_auth_args()
        try:
            result = subprocess.run(cleanup_cmd, capture_output=True, text=False, timeout=120)
            stderr_text = self._decode_subprocess_output(result.stderr)
            if result.returncode == 0:
                log_print(f"✅ SVN cleanup 成功: {self.local_path}", 'SVN')
                return True, "cleanup success"
            return False, f"cleanup failed: {stderr_text}"
        except Exception as exc:
            return False, f"cleanup exception: {exc}"

    def _run_svn_revert(self):
        revert_cmd = [self.svn_executable, 'revert', '-R', self.local_path] + self._build_auth_args()
        try:
            result = subprocess.run(revert_cmd, capture_output=True, text=False, timeout=180)
            stderr_text = self._decode_subprocess_output(result.stderr)
            if result.returncode == 0:
                return True, "revert success"
            return False, f"revert failed: {stderr_text}"
        except Exception as exc:
            return False, f"revert exception: {exc}"

    @staticmethod
    def _is_lock_related_error(stderr_text):
        text = str(stderr_text or "").lower()
        lock_keywords = [
            "e155004",
            "working copy locked",
            "is locked",
            "run 'svn cleanup'",
            "cleanup",
        ]
        return any(keyword in text for keyword in lock_keywords)

    def _run_svn_update_once(self):
        cmd = [self.svn_executable, 'update', self.local_path] + self._build_auth_args()
        try:
            result = subprocess.run(cmd, capture_output=True, text=False, cwd=self.local_path, timeout=300)
        except subprocess.TimeoutExpired:
            return False, "SVN更新超时"

        stdout_text = self._decode_subprocess_output(result.stdout)
        stderr_text = self._decode_subprocess_output(result.stderr)
        if result.returncode == 0:
            return True, stdout_text
        return False, stderr_text
        
    def checkout_or_update_repository(self):
        """检出或更新本地SVN仓库"""
        try:
            if os.path.exists(self.local_path):
                # 如果本地仓库已存在，则更新
                from utils.safe_print import log_print
                cleanup_ok, cleanup_msg = self._run_svn_cleanup()
                if not cleanup_ok:
                    log_print(f"⚠️ SVN cleanup 预处理失败，继续尝试update: {cleanup_msg}", 'SVN', force=True)

                log_print(f"执行SVN update命令: {self.svn_executable} update {self.local_path}", 'SVN')
                success, output = self._run_svn_update_once()
                if success:
                    log_print(f"SVN仓库已更新: {self.local_path}", 'SVN')
                    return True, "SVN仓库更新成功"

                if self._is_lock_related_error(output):
                    log_print("检测到SVN工作副本锁冲突，执行cleanup+revert后重试", 'SVN', force=True)
                    self._run_svn_cleanup()
                    self._run_svn_revert()
                    retry_success, retry_output = self._run_svn_update_once()
                    if retry_success:
                        log_print(f"SVN仓库重试更新成功: {self.local_path}", 'SVN')
                        return True, "SVN仓库更新成功（cleanup重试）"
                    return False, f"SVN更新失败(重试后): {retry_output}"

                return False, f"SVN更新失败: {output}"
            else:
                # 检出新仓库
                os.makedirs(os.path.dirname(self.local_path), exist_ok=True)

                cmd = [self.svn_executable, 'checkout', self.repository_url, self.local_path]
                cmd.extend(self._build_auth_args())

                from utils.safe_print import log_print
                log_print(f"执行SVN checkout命令: {' '.join(cmd[:3])} [认证信息已隐藏] {self.local_path}", 'SVN')

                # 使用二进制模式避免编码问题，添加超时
                try:
                    result = subprocess.run(cmd, capture_output=True, text=False, timeout=600)
                    log_print(f"SVN checkout命令完成，返回码: {result.returncode}", 'SVN')
                except subprocess.TimeoutExpired:
                    log_print("SVN checkout命令超时（10分钟）", 'SVN', force=True)
                    return False, "SVN检出超时"
                stdout_text = self._decode_subprocess_output(result.stdout)
                stderr_text = self._decode_subprocess_output(result.stderr)

                if result.returncode == 0:
                    from utils.safe_print import log_print
                    log_print(f"SVN仓库已检出: {self.local_path}", 'SVN')
                    return True, "SVN仓库检出成功"
                else:
                    return False, f"SVN检出失败: {stderr_text}"
                    
        except Exception as e:
            return False, f"SVN操作失败: {str(e)}"
    
    def get_commits(self, since_revision=None, limit=100):
        """获取SVN提交记录"""
        try:
            if not os.path.exists(self.local_path):
                success, message = self.checkout_or_update_repository()
                if not success:
                    return []

            # 使用缓存的仓库信息，避免SQLAlchemy会话问题
            current_version = self.repository_current_version
            username = self.repository_username
            password = self.repository_password

            # 构建svn log命令
            cmd = [self.svn_executable, 'log', '--xml', '-v', f'-l{limit}']

            if since_revision:
                cmd.extend(['-r', f'{since_revision}:HEAD'])
            elif current_version:
                cmd.extend(['-r', f'{current_version}:HEAD'])

            if username and password:
                cmd.extend(['--username', username, '--password', password])

            # 添加非交互模式参数，避免等待用户输入
            cmd.extend(['--non-interactive', '--trust-server-cert'])

            from utils.safe_print import log_print
            log_print(f"执行SVN log命令: {' '.join(cmd[:3])} [认证信息已隐藏] {' '.join(cmd[6:])}", 'SVN')
            log_print(f"工作目录: {self.local_path}", 'SVN')

            # 使用二进制模式避免编码问题，添加超时
            try:
                result = subprocess.run(cmd, capture_output=True, text=False, cwd=self.local_path, timeout=60)
                log_print(f"SVN log命令完成，返回码: {result.returncode}", 'SVN')
            except subprocess.TimeoutExpired:
                log_print("SVN log命令超时（60秒）", 'SVN', force=True)
                log_print(f"🔄 SVN操作超时，退出当前操作，不影响后续处理", 'SVN')
                return []

            # 使用通用解码函数处理输出
            stdout_text = self._decode_subprocess_output(result.stdout)
            stderr_text = self._decode_subprocess_output(result.stderr)

            # 创建一个模拟的result对象用于后续处理
            class MockResult:
                def __init__(self, returncode, stdout, stderr):
                    self.returncode = returncode
                    self.stdout = stdout
                    self.stderr = stderr

            result = MockResult(result.returncode, stdout_text, stderr_text)
            
            if result.returncode != 0:
                from utils.safe_print import log_print
                log_print(f"获取SVN日志失败: {result.stderr}", 'SVN', force=True)
                log_print(f"🔄 SVN日志获取失败，退出当前操作，不影响后续处理", 'SVN')
                return []

            return self._parse_svn_log(result.stdout)

        except Exception as e:
            from utils.safe_print import log_print
            log_print(f"获取SVN提交记录失败: {str(e)}", 'SVN', force=True)
            log_print(f"🔄 SVN操作异常，退出当前操作，不影响后续处理", 'SVN')
            return []
    
    def _parse_svn_log(self, xml_content):
        """解析SVN日志XML"""
        commits = []

        try:
            # 检查xml_content是否为None或空
            if not xml_content:
                from utils.safe_print import log_print
                log_print("SVN日志XML内容为空", 'SVN', force=True)
                return commits

            root = ET.fromstring(xml_content)
            
            for logentry in root.findall('logentry'):
                revision = logentry.get('revision')
                author = logentry.find('author').text if logentry.find('author') is not None else 'unknown'
                date_str = logentry.find('date').text if logentry.find('date') is not None else ''
                message = logentry.find('msg').text if logentry.find('msg') is not None else ''
                
                # 解析日期
                commit_time = None
                if date_str:
                    try:
                        commit_time = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                    except:
                        commit_time = datetime.now(timezone.utc)
                
                # 使用缓存的过滤配置，避免SQLAlchemy会话问题
                commit_filter = self.repository_commit_filter
                log_filter_regex = self.repository_log_filter_regex
                path_regex = self.repository_path_regex

                # 过滤提交人
                if commit_filter:
                    filter_authors = [name.strip() for name in commit_filter.split(',')]
                    if author in filter_authors:
                        continue

                # 过滤日志
                if log_filter_regex and message:
                    import re
                    if re.match(log_filter_regex, message):
                        continue
                
                # 解析文件路径
                paths = logentry.find('paths')
                if paths is not None:
                    for path in paths.findall('path'):
                        file_path = path.text
                        operation = path.get('action', 'M')
                        
                        # 路径过滤
                        if path_regex:
                            import re
                            if not re.match(path_regex, file_path):
                                continue
                        
                        commits.append({
                            'commit_id': f'r{revision}',
                            'path': file_path,
                            'version': revision,
                            'operation': operation,
                            'author': author,
                            'commit_time': commit_time,
                            'message': message.strip() if message else ''
                        })
        
        except ET.ParseError as e:
            from utils.safe_print import log_print
            log_print(f"解析SVN日志XML失败: {str(e)}", 'SVN', force=True)
        except Exception as e:
            from utils.safe_print import log_print
            log_print(f"处理SVN日志失败: {str(e)}", 'SVN', force=True)
        
        return commits
    
    def get_file_history(self, file_path, limit=100):
        """获取指定文件的SVN提交历史"""
        try:
            import os
            import subprocess
            from datetime import datetime, timezone

            if not os.path.exists(self.local_path):
                print(f"❌ [SVN_SERVICE] 仓库路径不存在: {self.local_path}")
                return []

            # 构建SVN log命令
            cmd = [
                self.svn_executable, 'log',
                '--xml',
                '--limit', str(limit),
                os.path.join(self.local_path, file_path)
            ]

            # 使用缓存的认证信息，避免SQLAlchemy会话问题
            username = self.repository_username
            token = self.repository_token

            # 如果有认证信息，添加认证参数
            if username:
                cmd.extend(['--username', username])
            if token:
                cmd.extend(['--password', token])

            # 使用二进制模式避免编码问题
            result = subprocess.run(cmd, capture_output=True, text=False, timeout=60)

            if result.returncode != 0:
                stderr_text = self._decode_subprocess_output(result.stderr)
                from utils.safe_print import log_print
                log_print(f"❌ SVN log命令失败: {stderr_text}", 'SVN', force=True)
                log_print(f"🔄 SVN文件历史获取失败，退出当前操作", 'SVN')
                return []

            # 解码stdout
            stdout_text = self._decode_subprocess_output(result.stdout)

            # 解析XML输出
            import xml.etree.ElementTree as ET
            root = ET.fromstring(stdout_text)

            commits_data = []
            for logentry in root.findall('logentry'):
                revision = logentry.get('revision')
                author = logentry.find('author')
                date = logentry.find('date')
                msg = logentry.find('msg')

                # 解析时间
                if date is not None and date.text:
                    try:
                        # SVN时间格式: 2025-09-10T10:08:56.123456Z
                        commit_time = datetime.fromisoformat(date.text.replace('Z', '+00:00'))
                    except:
                        commit_time = datetime.now(timezone.utc)
                else:
                    commit_time = datetime.now(timezone.utc)

                # 检查文件操作类型（SVN log不直接提供，默认为修改）
                operation = 'M'

                commit_data = {
                    'commit_id': f"r{revision}",
                    'author': author.text if author is not None else 'Unknown',
                    'commit_time': commit_time,
                    'message': msg.text if msg is not None else '',
                    'operation': operation
                }

                commits_data.append(commit_data)

            from utils.safe_print import log_print
            log_print(f"✅ 获取到文件 {file_path} 的 {len(commits_data)} 个提交记录", 'SVN')
            return commits_data

        except subprocess.TimeoutExpired:
            from utils.safe_print import log_print
            log_print(f"❌ 获取文件提交历史超时: {file_path}", 'SVN', force=True)
            log_print(f"🔄 SVN操作超时，退出当前操作，不影响后续处理", 'SVN')
            return []
        except Exception as e:
            from utils.safe_print import log_print
            log_print(f"❌ 获取文件提交历史失败: {e}", 'SVN', force=True)
            log_print(f"🔄 SVN操作异常，退出当前操作，不影响后续处理", 'SVN')
            return []

    def get_file_diff(self, revision, file_path):
        """获取文件的SVN diff"""
        try:
            if not os.path.exists(self.local_path):
                return None
            
            # 获取当前版本和前一版本的diff
            prev_revision = str(int(revision) - 1) if revision.isdigit() else revision

            cmd = [self.svn_executable, 'diff', f'-r{prev_revision}:{revision}', file_path]
            if self.repository.username and self.repository.password:
                cmd.extend(['--username', self.repository.username, '--password', self.repository.password])

            result = subprocess.run(cmd, capture_output=True, text=True, cwd=self.local_path)
            
            if result.returncode == 0:
                return {
                    'diff_text': result.stdout,
                    'old_content': self._get_file_content(prev_revision, file_path),
                    'new_content': self._get_file_content(revision, file_path)
                }
            
            return None
            
        except Exception as e:
            from utils.safe_print import log_print
            log_print(f"获取SVN文件diff失败: {str(e)}", 'SVN', force=True)
            return None
    
    def _get_file_content(self, revision, file_path):
        """获取指定版本的文件内容"""
        try:
            cmd = [self.svn_executable, 'cat', f'-r{revision}', file_path]
            if self.repository.username and self.repository.password:
                cmd.extend(['--username', self.repository.username, '--password', self.repository.password])

            result = subprocess.run(cmd, capture_output=True, text=True, cwd=self.local_path)
            
            if result.returncode == 0:
                return result.stdout
            
            return ''
            
        except Exception as e:
            from utils.safe_print import log_print
            log_print(f"获取SVN文件内容失败: {str(e)}", 'SVN', force=True)
            return ''
    
    def parse_excel_diff(self, revision, file_path):
        """解析Excel文件的SVN差异（简化版本）"""
        try:
            if not os.path.exists(self.local_path):
                return None
            
            # 获取当前版本文件
            current_file = os.path.join(self.local_path, file_path)
            if not os.path.exists(current_file):
                return None
            
            # 获取文件大小信息
            current_size = os.path.getsize(current_file)
            
            # 尝试获取上一版本的文件大小
            try:
                prev_revision = str(int(revision) - 1)
                cmd = [
                    self.svn_executable, 'info',
                    f"{self.repository.url}/{file_path}@{prev_revision}"
                ]
                
                if self.repository.username and self.repository.password:
                    cmd.extend(['--username', self.repository.username])
                    cmd.extend(['--password', self.repository.password])
                
                result = subprocess.run(cmd, capture_output=True, text=True)
                
                if result.returncode == 0:
                    # 解析文件大小（简化处理）
                    diff_data = {
                        'file_type': 'Excel',
                        'current_size': current_size,
                        'message': f'Excel文件当前大小: {current_size} 字节'
                    }
                else:
                    diff_data = {
                        'file_type': 'Excel',
                        'current_size': current_size,
                        'message': '新增的Excel文件'
                    }
                
                return diff_data
                
            except Exception as e:
                return {
                    'file_type': 'Excel',
                    'current_size': current_size,
                    'message': f'Excel文件大小: {current_size} 字节'
                }
                
        except Exception as e:
            from utils.safe_print import log_print
            log_print(f"解析Excel差异失败: {str(e)}", 'SVN', force=True)
            return None
    
    def _get_file_binary_content(self, revision, file_path):
        """获取指定版本的二进制文件内容"""
        try:
            # 使用缓存的认证信息，避免SQLAlchemy会话问题
            username = self.repository_username
            password = self.repository_password

            cmd = [self.svn_executable, 'cat', f'-r{revision}', file_path]
            if username and password:
                cmd.extend(['--username', username, '--password', password])

            result = subprocess.run(cmd, capture_output=True, cwd=self.local_path)

            if result.returncode == 0:
                return result.stdout

            return None

        except Exception as e:
            from utils.safe_print import log_print
            log_print(f"获取SVN二进制文件内容失败: {str(e)}", 'SVN', force=True)
            log_print(f"🔄 SVN操作异常，退出当前操作，不影响后续处理", 'SVN')
            return None
    
    def _compare_dataframes(self, old_df, new_df, sheet_name):
        """比较两个DataFrame的差异"""
        changes = []
        
        try:
            # 转换为字符串以便比较
            old_df = old_df.astype(str).fillna('')
            new_df = new_df.astype(str).fillna('')
            
            # 比较行数变化
            old_rows, old_cols = old_df.shape
            new_rows, new_cols = new_df.shape
            
            if new_rows > old_rows:
                for i in range(old_rows, new_rows):
                    row_data = {}
                    for j, col in enumerate(new_df.columns):
                        if j < len(new_df.columns):
                            row_data[chr(65 + j)] = new_df.iloc[i, j] if i < len(new_df) else ''
                    
                    changes.append({
                        'type': 'added',
                        'sheet_name': sheet_name,
                        'row': i + 1,
                        'data': row_data,
                        'message': f'{sheet_name} 第{i+1}行新增'
                    })
            
            # 比较现有行的变化
            min_rows = min(old_rows, new_rows)
            for i in range(min_rows):
                row_changed = False
                old_row_data = {}
                new_row_data = {}
                
                for j in range(max(old_cols, new_cols)):
                    col_name = chr(65 + j)
                    old_val = old_df.iloc[i, j] if i < len(old_df) and j < old_cols else ''
                    new_val = new_df.iloc[i, j] if i < len(new_df) and j < new_cols else ''
                    
                    old_row_data[col_name] = old_val
                    new_row_data[col_name] = new_val
                    
                    if old_val != new_val:
                        row_changed = True
                
                if row_changed:
                    changes.append({
                        'type': 'modified',
                        'sheet_name': sheet_name,
                        'row': i + 1,
                        'old_data': old_row_data,
                        'new_data': new_row_data,
                        'message': f'{sheet_name} 第{i+1}行修改'
                    })
            
        except Exception as e:
            from utils.safe_print import log_print
            log_print(f"比较DataFrame失败: {str(e)}", 'SVN', force=True)
        
        return changes
    
    def get_version_range_diff(self, from_version, to_version, file_path):
        """获取版本范围内的diff"""
        try:
            if not os.path.exists(self.local_path):
                return None
            
            # 构建SVN diff命令
            cmd = [self.svn_executable, 'diff', '-r', f"{from_version}:{to_version}", file_path]
            if self.repository.username and self.repository.password:
                cmd.extend(['--username', self.repository.username, '--password', self.repository.password])

            result = subprocess.run(cmd, capture_output=True, text=True, cwd=self.local_path)
            
            if result.returncode == 0 and result.stdout:
                # 解析diff输出
                diff_data = self._parse_unified_diff(result.stdout)
                diff_data['file_path'] = file_path
                return diff_data
            else:
                return None
                
        except Exception as e:
            from utils.safe_print import log_print
            log_print(f"获取版本范围diff失败: {str(e)}", 'SVN', force=True)
            return None

    def get_commit_info(self, commit_id):
        """获取单个SVN提交的详细信息"""
        try:
            import subprocess
            from datetime import datetime

            # 移除 'r' 前缀（如果存在）
            revision = commit_id.replace('r', '') if commit_id.startswith('r') else commit_id

            # 使用缓存的仓库信息，避免SQLAlchemy会话问题
            repository_url = self.repository_url
            repository_username = self.repository_username
            repository_token = self.repository_token

            # 构建SVN log命令获取特定版本信息
            cmd = [
                self.svn_executable, 'log',
                '--xml',
                '-r', revision,
                repository_url
            ]

            # 添加认证信息
            if repository_username:
                cmd.extend(['--username', repository_username])
            if repository_token:
                cmd.extend(['--password', repository_token])

            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', timeout=30)

            if result.returncode != 0:
                from utils.safe_print import log_print
                log_print(f"✗ SVN log命令失败: {result.stderr}", 'SVN', force=True)
                log_print(f"🔄 SVN操作失败，退出当前操作，不影响后续处理", 'SVN')
                return None

            # 解析XML输出
            import xml.etree.ElementTree as ET
            root = ET.fromstring(result.stdout)

            logentry = root.find('logentry')
            if logentry is None:
                from utils.safe_print import log_print
                log_print(f"✗ 未找到版本 {revision} 的信息", 'SVN', force=True)
                log_print(f"🔄 SVN版本信息不存在，退出当前操作", 'SVN')
                return None

            # 提取信息
            author_elem = logentry.find('author')
            date_elem = logentry.find('date')
            msg_elem = logentry.find('msg')

            author = author_elem.text if author_elem is not None else 'Unknown'
            message = msg_elem.text if msg_elem is not None else ''

            # 解析时间
            commit_time_str = '未知时间'
            commit_timestamp = None
            if date_elem is not None and date_elem.text:
                try:
                    commit_timestamp = datetime.fromisoformat(date_elem.text.replace('Z', '+00:00'))
                    commit_time_str = commit_timestamp.strftime('%Y-%m-%d %H:%M:%S')
                except:
                    commit_time_str = '时间解析失败'

            commit_info = {
                'commit_id': f"r{revision}",
                'short_id': f"r{revision}",
                'author': author,
                'author_email': '',  # SVN通常不包含邮箱信息
                'message': message,
                'commit_time': commit_time_str,
                'commit_timestamp': commit_timestamp
            }

            from utils.safe_print import log_print
            log_print(f"✓ 获取SVN提交信息成功: {commit_info['short_id']} - {commit_info['author']} - {commit_info['message'][:50]}", 'SVN')
            return commit_info

        except subprocess.TimeoutExpired:
            from utils.safe_print import log_print
            log_print(f"✗ 获取SVN提交信息超时: {commit_id}", 'SVN', force=True)
            log_print(f"🔄 SVN操作超时，退出当前操作，不影响后续处理", 'SVN')
            return None
        except Exception as e:
            from utils.safe_print import log_print
            log_print(f"✗ 获取SVN提交信息失败: {e}", 'SVN', force=True)
            log_print(f"🔄 SVN操作异常，退出当前操作，不影响后续处理", 'SVN')
            return None

    def sync_repository_commits(self, db=None, Commit=None):
        """同步SVN仓库的提交记录到数据库"""
        try:
            from utils.safe_print import log_print

            # 使用缓存的仓库信息，避免SQLAlchemy会话问题
            repository_name = self.repository_name
            repository_id = self.repository_id

            log_print(f"🔄 开始同步SVN仓库提交记录: {repository_name}", 'SVN')

            # 获取SVN提交记录
            log_print("📥 调用get_commits获取提交记录", 'SVN')
            commits = self.get_commits(limit=1000)  # 获取最近1000个提交
            log_print(f"📥 get_commits返回了 {len(commits) if commits else 0} 个提交记录", 'SVN')

            if not commits:
                log_print("❌ 未获取到SVN提交记录", 'SVN')
                return 0

            # 如果没有传入数据库模块，尝试获取
            if db is None or Commit is None:
                log_print("📦 获取数据库模块", 'SVN')
                db, Commit = get_db_models()
                if db is None or Commit is None:
                    log_print("❌ 无法获取数据库模块", 'SVN', force=True)
                    return 0
            log_print("📦 数据库模块准备完成", 'SVN')

            commits_added = 0
            log_print(f"🔄 开始处理 {len(commits)} 个提交记录", 'SVN')

            # 检查第一个提交记录的结构
            if commits:
                first_commit = commits[0]
                log_print(f"🔍 第一个提交记录结构: {list(first_commit.keys())}", 'SVN')
                log_print(f"🔍 第一个提交记录内容: commit_id={first_commit.get('commit_id', 'N/A')}, author={first_commit.get('author', 'N/A')}", 'SVN')

            for i, commit_data in enumerate(commits):
                try:
                    if i < 3:  # 只为前3个提交记录打印详细日志
                        log_print(f"🔄 处理提交 {i+1}/{len(commits)}: {commit_data.get('commit_id', 'unknown')}", 'SVN')

                    # 检查提交是否已存在
                    existing_commit = Commit.query.filter_by(
                        repository_id=repository_id,
                        commit_id=commit_data['commit_id']  # 修复字段名
                    ).first()

                    if existing_commit:
                        if i < 3:
                            log_print(f"⏭️ 跳过已存在的提交: {commit_data['commit_id']}", 'SVN')
                        continue  # 跳过已存在的提交

                    # 创建新的提交记录
                    if i < 3:
                        log_print(f"➕ 创建新提交记录: {commit_data['commit_id']}", 'SVN')
                    commit = Commit(
                        repository_id=repository_id,
                        commit_id=commit_data['commit_id'],  # 修复字段名
                        author=commit_data['author'],
                        message=commit_data['message'],
                        commit_time=commit_data['commit_time'],  # 修复字段名
                        path=commit_data.get('path', ''),  # 修复字段名
                        version=commit_data.get('version', ''),
                        operation=commit_data.get('operation', 'M'),
                        status='pending'
                    )

                    db.session.add(commit)
                    commits_added += 1
                    if i < 3:
                        log_print(f"✅ 提交记录已添加到会话: {commit_data['commit_id']}", 'SVN')

                except Exception as commit_error:
                    log_print(f"❌ 处理单个提交记录失败: {commit_data.get('commit_id', 'unknown')} - {str(commit_error)}", 'SVN', force=True)
                    continue

            # 提交数据库更改
            log_print(f"💾 准备提交数据库更改，共添加 {commits_added} 个提交", 'SVN')
            try:
                db.session.commit()
                log_print(f"✅ 数据库提交成功", 'SVN')
            except Exception as db_error:
                log_print(f"❌ 数据库提交失败: {str(db_error)}", 'SVN', force=True)
                db.session.rollback()
                log_print(f"🔄 数据库回滚完成", 'SVN')
                raise db_error

            log_print(f"✅ SVN仓库提交记录同步完成: {repository_name}, 添加了 {commits_added} 个提交记录", 'SVN')
            return commits_added

        except Exception as e:
            from utils.safe_print import log_print
            log_print(f"❌ 同步SVN仓库提交记录失败: {str(e)}", 'SVN', force=True)
            log_print(f"🔄 SVN同步操作异常，退出当前操作，不影响后续处理", 'SVN')
            try:
                if db:
                    db.session.rollback()
                    log_print(f"🔄 异常处理中的数据库回滚完成", 'SVN')
            except:
                pass
            return 0
