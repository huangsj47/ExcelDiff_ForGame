#!/usr/bin/env python3
"""
增强的Git服务 - 专门处理大型仓库克隆和重试机制
支持Git LFS、大型仓库（40GB+）的克隆优化
"""

import os
import subprocess
import time
import urllib.parse
from typing import Tuple, Optional
import git
from .git_service import GitService
from utils.security_utils import sanitize_text, sanitize_url


class EnhancedGitService(GitService):
    """增强的Git服务，支持重试机制和大型仓库优化"""
    
    def __init__(self, repository_url: str, root_directory: str = None, 
                 username: str = None, token: str = None, repository=None):
        super().__init__(repository_url, root_directory, username, token, repository)
        self.repository = repository
        self.max_retries = 3  # 最大重试次数
        self.retry_delay = 30  # 重试间隔（秒）
        self.clone_timeout = 3600  # 克隆超时时间（1小时，适合大型仓库）

    def _run_git_command_safe(self, cmd, cwd=None, timeout=300):
        """安全执行Git命令，处理编码问题"""
        try:
            # 设置环境变量以处理中文编码
            env = os.environ.copy()
            env['LC_ALL'] = 'C.UTF-8'
            env['LANG'] = 'C.UTF-8'
            env['PYTHONIOENCODING'] = 'utf-8'

            result = subprocess.run(
                cmd,
                cwd=cwd or self.local_path,
                capture_output=True,
                text=False,  # 使用二进制模式避免编码问题
                env=env,
                timeout=timeout
            )

            # 手动处理编码
            stdout_text = self._decode_subprocess_output(result.stdout)
            stderr_text = self._decode_subprocess_output(result.stderr)

            # 创建一个模拟的result对象
            class SafeResult:
                def __init__(self, returncode, stdout, stderr):
                    self.returncode = returncode
                    self.stdout = stdout
                    self.stderr = stderr

            return SafeResult(result.returncode, stdout_text, stderr_text)

        except subprocess.TimeoutExpired:
            class TimeoutResult:
                def __init__(self):
                    self.returncode = -1
                    self.stdout = ""
                    self.stderr = "命令执行超时"
            return TimeoutResult()
        except Exception as e:
            class ErrorResult:
                def __init__(self, error):
                    self.returncode = -1
                    self.stdout = ""
                    self.stderr = f"命令执行失败: {str(error)}"
            return ErrorResult(e)

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
        
    def clone_or_update_repository_with_retry(self) -> Tuple[bool, str]:
        """带重试机制的克隆或更新仓库"""
        print(f"开始克隆/更新仓库，支持重试机制")
        print(f"仓库URL: {sanitize_url(self.repo_url)}")
        print(f"本地路径: {self.local_path}")
        
        # 如果本地仓库存在，尝试更新
        if os.path.exists(self.local_path):
            return self._update_repository_with_retry()
        else:
            return self._clone_repository_with_retry()
    
    def _clone_repository_with_retry(self) -> Tuple[bool, str]:
        """带重试机制的克隆仓库"""
        print(f"本地仓库不存在，开始克隆...")
        
        for attempt in range(1, self.max_retries + 1):
            print(f"\n=== 克隆尝试 {attempt}/{self.max_retries} ===")
            
            try:
                # 清理可能存在的不完整目录
                if os.path.exists(self.local_path):
                    print(f"清理不完整的克隆目录: {self.local_path}")
                    import shutil
                    shutil.rmtree(self.local_path)
                
                # 创建父目录
                os.makedirs(os.path.dirname(self.local_path), exist_ok=True)
                
                # 尝试克隆
                success, message = self._perform_clone()
                
                if success:
                    print(f"✅ 克隆成功！")
                    return True, message
                else:
                    print(f"❌ 克隆失败: {message}")
                    
                    # 如果不是最后一次尝试，等待后重试
                    if attempt < self.max_retries:
                        print(f"等待 {self.retry_delay} 秒后重试...")
                        time.sleep(self.retry_delay)
                    
            except Exception as e:
                error_msg = f"克隆过程异常: {str(e)}"
                print(f"❌ {error_msg}")
                
                if attempt < self.max_retries:
                    print(f"等待 {self.retry_delay} 秒后重试...")
                    time.sleep(self.retry_delay)
                else:
                    return False, f"克隆失败，已重试 {self.max_retries} 次。最后错误: {error_msg}"
        
        return False, f"克隆失败，已重试 {self.max_retries} 次"
    
    def _update_repository_with_retry(self) -> Tuple[bool, str]:
        """带重试机制的更新仓库"""
        print(f"本地仓库已存在，开始更新...")
        
        for attempt in range(1, self.max_retries + 1):
            print(f"\n=== 更新尝试 {attempt}/{self.max_retries} ===")
            
            try:
                # 尝试git pull
                success, message = self._perform_pull()
                
                if success:
                    print(f"✅ 更新成功！")
                    return True, message
                else:
                    print(f"❌ 更新失败: {message}")
                    
                    # 如果pull失败，尝试重置后再pull
                    if "conflict" in message.lower() or "merge" in message.lower():
                        print("检测到冲突，尝试重置后更新...")
                        reset_success, reset_msg = self._reset_and_pull()
                        if reset_success:
                            print(f"✅ 重置后更新成功！")
                            return True, reset_msg
                    
                    # 如果不是最后一次尝试，等待后重试
                    if attempt < self.max_retries:
                        print(f"等待 {self.retry_delay} 秒后重试...")
                        time.sleep(self.retry_delay)
                    
            except Exception as e:
                error_msg = f"更新过程异常: {str(e)}"
                print(f"❌ {error_msg}")
                
                if attempt < self.max_retries:
                    print(f"等待 {self.retry_delay} 秒后重试...")
                    time.sleep(self.retry_delay)
                else:
                    # 最后尝试：删除本地仓库重新克隆
                    print("所有更新尝试失败，删除本地仓库重新克隆...")
                    return self._fallback_to_fresh_clone()
        
        return False, f"更新失败，已重试 {self.max_retries} 次"
    
    def _perform_clone(self) -> Tuple[bool, str]:
        """执行实际的克隆操作"""
        try:
            # 构建克隆URL
            clone_url = self._build_clone_url()
            
            # 构建git clone命令，针对大型仓库优化
            cmd = self._build_clone_command(clone_url)
            safe_cmd = [sanitize_url(part) if idx == len(cmd) - 2 else part for idx, part in enumerate(cmd)]
            print(f"执行克隆命令: {sanitize_text(' '.join(safe_cmd))}")

            # 执行克隆命令，使用安全的编码处理
            result = self._run_git_command_safe(
                cmd,
                cwd=os.path.dirname(self.local_path),
                timeout=self.clone_timeout
            )
            
            if result.returncode == 0:
                # 检查是否为Git LFS仓库并拉取LFS文件
                self._handle_git_lfs()
                return True, "仓库克隆成功"
            else:
                error_msg = self._parse_git_error(sanitize_text(result.stderr))
                return False, error_msg
                
        except subprocess.TimeoutExpired:
            return False, f"克隆超时（超过 {self.clone_timeout/60:.1f} 分钟）"
        except Exception as e:
            return False, f"克隆异常: {sanitize_text(str(e))}"
    
    def _perform_pull(self) -> Tuple[bool, str]:
        """执行git pull操作"""
        try:
            # 使用 --no-rebase 避免rebase冲突
            cmd = ['git', 'pull', '--no-rebase', 'origin']
            if self.repository and self.repository.branch:
                cmd.append(self.repository.branch)
            
            print(f"执行更新命令: {' '.join(cmd)}")

            result = self._run_git_command_safe(
                cmd,
                cwd=self.local_path,
                timeout=600  # 10分钟超时
            )
            
            if result.returncode == 0:
                # 检查输出确认更新状态
                if "Already up to date" in result.stdout or "Already up-to-date" in result.stdout:
                    return True, "仓库已是最新状态"
                else:
                    # 处理Git LFS更新
                    self._handle_git_lfs()
                    return True, "仓库更新成功"
            else:
                # 如果还是失败，尝试使用fetch + reset策略
                print("pull失败，尝试使用fetch + reset策略")
                return self._fetch_and_reset()
                
        except subprocess.TimeoutExpired:
            return False, "更新超时"
        except Exception as e:
            return False, f"更新异常: {str(e)}"
    
    def _fetch_and_reset(self) -> Tuple[bool, str]:
        """使用fetch + reset策略更新仓库"""
        try:
            # 先fetch获取最新数据
            print("执行 git fetch origin")
            fetch_cmd = ['git', 'fetch', 'origin']
            fetch_result = self._run_git_command_safe(
                fetch_cmd,
                cwd=self.local_path,
                timeout=300
            )
            
            if fetch_result.returncode != 0:
                return False, f"fetch失败: {sanitize_text(fetch_result.stderr)}"
            
            # 重置到远程分支
            branch = self.repository.branch if self.repository and self.repository.branch else 'main'
            print(f"执行 git reset --hard origin/{branch}")
            reset_cmd = ['git', 'reset', '--hard', f'origin/{branch}']
            reset_result = self._run_git_command_safe(
                reset_cmd,
                cwd=self.local_path,
                timeout=60
            )
            
            if reset_result.returncode != 0:
                return False, f"重置失败: {sanitize_text(reset_result.stderr)}"
            
            # 清理未跟踪的文件
            print("执行 git clean -fd")
            clean_cmd = ['git', 'clean', '-fd']
            self._run_git_command_safe(clean_cmd, cwd=self.local_path, timeout=60)
            
            # 处理Git LFS
            self._handle_git_lfs()
            return True, "仓库更新成功（使用fetch+reset策略）"
            
        except Exception as e:
            return False, f"fetch+reset操作异常: {sanitize_text(str(e))}"

    def _reset_and_pull(self) -> Tuple[bool, str]:
        """重置本地更改后拉取"""
        try:
            print("执行 git reset --hard HEAD")
            reset_cmd = ['git', 'reset', '--hard', 'HEAD']
            reset_result = self._run_git_command_safe(
                reset_cmd,
                cwd=self.local_path,
                timeout=60
            )
            
            if reset_result.returncode != 0:
                return False, f"重置失败: {sanitize_text(reset_result.stderr)}"
            
            print("执行 git clean -fd")
            clean_cmd = ['git', 'clean', '-fd']
            clean_result = self._run_git_command_safe(
                clean_cmd,
                cwd=self.local_path,
                timeout=60
            )
            
            # 重新尝试pull
            return self._perform_pull()
            
        except Exception as e:
            return False, f"重置操作异常: {sanitize_text(str(e))}"
    
    def _fallback_to_fresh_clone(self) -> Tuple[bool, str]:
        """回退到重新克隆"""
        try:
            print("删除本地仓库，准备重新克隆...")
            import shutil
            shutil.rmtree(self.local_path)
            
            return self._perform_clone()
            
        except Exception as e:
            return False, f"重新克隆失败: {sanitize_text(str(e))}"
    
    def _build_clone_url(self) -> str:
        """构建带认证的克隆URL"""
        clone_url = self.repo_url
        
        if self.repository and self.repository.token:
            parsed_url = urllib.parse.urlparse(self.repo_url)
            if parsed_url.scheme in ['http', 'https']:
                clone_url = f"{parsed_url.scheme}://oauth2:{self.repository.token}@{parsed_url.netloc}{parsed_url.path}"
        
        return clone_url
    
    def _build_clone_command(self, clone_url: str) -> list:
        """构建针对大型仓库优化的克隆命令"""
        cmd = ['git', 'clone']
        
        # 大型仓库优化参数
        cmd.extend([
            '--progress',  # 显示进度
            # '--depth', '1',  # 浅克隆，只克隆最新提交 - 改为完整克隆
            '--single-branch',  # 只克隆指定分支
        ])
        
        # 指定分支
        if self.repository and self.repository.branch:
            cmd.extend(['-b', self.repository.branch])
        
        cmd.extend([clone_url, os.path.basename(self.local_path)])
        
        return cmd
    
    def _handle_git_lfs(self):
        """处理Git LFS文件"""
        try:
            # 检查是否为LFS仓库
            lfs_config = os.path.join(self.local_path, '.gitattributes')
            if os.path.exists(lfs_config):
                print("检测到Git LFS仓库，拉取LFS文件...")
                
                lfs_cmd = ['git', 'lfs', 'pull']
                result = self._run_git_command_safe(
                    lfs_cmd,
                    cwd=self.local_path,
                    timeout=1800  # 30分钟超时，适合大型LFS文件
                )
                
                if result.returncode == 0:
                    print("✅ Git LFS文件拉取成功")
                else:
                    print(f"⚠️ Git LFS拉取警告: {sanitize_text(result.stderr)}")
                    
        except subprocess.TimeoutExpired:
            print("⚠️ Git LFS拉取超时，但仓库克隆成功")
        except Exception as e:
            print(f"⚠️ Git LFS处理异常: {sanitize_text(str(e))}")
    
    def _parse_git_error(self, stderr: str) -> str:
        """解析Git错误信息，返回中文友好提示"""
        if not stderr:
            return "未知错误"
        
        stderr = sanitize_text(stderr)
        stderr_lower = stderr.lower()
        
        # 网络相关错误
        if any(keyword in stderr_lower for keyword in ['timeout', 'timed out', 'connection']):
            return f"网络连接超时，请检查网络状况。原始错误: {stderr}"
        
        # 认证相关错误
        if any(keyword in stderr_lower for keyword in ['authentication', 'permission denied', 'access denied']):
            return f"认证失败，请检查用户名、密码或访问令牌。原始错误: {stderr}"
        
        # 空间不足
        if any(keyword in stderr_lower for keyword in ['no space', 'disk full', 'insufficient']):
            return f"磁盘空间不足，请清理磁盘空间后重试。原始错误: {stderr}"
        
        # 仓库不存在
        if any(keyword in stderr_lower for keyword in ['not found', '404', 'does not exist']):
            return f"仓库不存在或无访问权限，请检查仓库URL。原始错误: {stderr}"
        
        # LFS相关错误
        if 'lfs' in stderr_lower:
            return f"Git LFS相关错误，可能需要安装Git LFS或检查LFS配置。原始错误: {stderr}"
        
        # 大文件相关
        if any(keyword in stderr_lower for keyword in ['large file', 'file too large', 'pack size']):
            return f"文件过大，建议使用Git LFS处理大文件。原始错误: {stderr}"
        
        # 默认返回原始错误
        return f"Git操作失败: {stderr}"
    
    def get_repository_size_info(self) -> dict:
        """获取仓库大小信息"""
        try:
            if not os.path.exists(self.local_path):
                return {"error": "本地仓库不存在"}
            
            # 获取.git目录大小
            git_dir = os.path.join(self.local_path, '.git')
            if os.path.exists(git_dir):
                git_size = self._get_directory_size(git_dir)
            else:
                git_size = 0
            
            # 获取工作目录大小
            work_size = self._get_directory_size(self.local_path) - git_size
            
            return {
                "git_size_mb": round(git_size / (1024 * 1024), 2),
                "work_size_mb": round(work_size / (1024 * 1024), 2),
                "total_size_mb": round((git_size + work_size) / (1024 * 1024), 2)
            }
            
        except Exception as e:
            return {"error": f"获取大小信息失败: {str(e)}"}
    
    def _get_directory_size(self, path: str) -> int:
        """获取目录大小（字节）"""
        total_size = 0
        try:
            for dirpath, dirnames, filenames in os.walk(path):
                for filename in filenames:
                    file_path = os.path.join(dirpath, filename)
                    if os.path.exists(file_path):
                        total_size += os.path.getsize(file_path)
        except (OSError, IOError):
            pass
        return total_size
