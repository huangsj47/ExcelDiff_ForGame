# -*- coding: utf-8 -*-
"""凭据不得进入日志 / 异常信息 / 回传平台的错误文本。

## 为什么需要这个测试

`utils/security_utils.py::sanitize_text` 长时间以来是**唯一**的脱敏器，而它只认
「URL 内嵌凭据」这一种形态（`scheme://user:pass@host`）。于是下面这些全部原样落盘：

    'svn log --username alice --password s3cr3t'                → 原样（参数形态）
    'svn -r alice:s3cr3t https://svn.example.com/repo'          → 原样
    'https://oauth2:ghp_AAA/BBB@github.com/x/y.git'             → 原样（token 含 /）
    'https://oauth2:p@ss@git.example.com/x.git'                 → 漏出尾部 'ss'

而 `subprocess.TimeoutExpired.__str__` 会把**整条命令行**拼进异常信息：

    Command '['svn','cleanup','C:/repos/x','--username','alice',
              '--password','S3CR3T','--non-interactive']' timed out after 120 seconds

`services/svn_service.py::_run_svn_cleanup` 原先用裸 `except Exception` 接住它并
`f"cleanup exception: {exc}"`，调用方再以 `force=True` 写进 `logs/runlog.log`
（该文件是持久化的，且保留 10 个备份）—— 明文仓库口令就此长期驻留磁盘。

`services/svn_service.py` 的 SVN log 分支更直接：用它自己的日志切片
`' '.join(cmd[6:])`，而 `--username u --password p` 的实际下标使 **`--password`
的值正好落在 `cmd[6]`** —— 标记写着「认证信息已隐藏」，实际把口令打了出来。

## 本文件断言什么

1. `redact_command_args` 按**参数名**脱敏，与下标无关（这正是原缺陷的成因）；
2. `sanitize_text` 覆盖参数形态与 URL 形态，含口令里带 `@`、`/` 的边界；
3. `svn_service._run_svn_cleanup` 超时时**不把异常对象插值进消息**；
4. Agent 侧的 `_redact_secrets` 能挡住 URL 内嵌 token（该文本会回传平台落库并上 UI）。
"""
import os
import re
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from utils.security_utils import (  # noqa: E402
    redact_command_args,
    sanitize_text,
)

# 测试用的假口令。刻意带上 @ 与 / —— 这正是原实现漏掉的边界。
FAKE_PWD = 'p@ss/w0rd'
FAKE_TOKEN = 'ghp_AAA/BBB+CCC'


class TestRedactCommandArgs:
    """按参数名脱敏，与参数位置无关。"""

    def test_password_flag_value_is_masked(self):
        cmd = ['svn', 'log', '--xml', '-r', '100:HEAD',
               '--username', 'alice', '--password', FAKE_PWD,
               '--non-interactive', '--trust-server-cert']
        out = redact_command_args(cmd)
        assert FAKE_PWD not in out, (
            f'口令泄漏到日志文本里了：{out}\n'
            f'原实现用 `cmd[6:]` 做切片，而 --password 的**值**正好落在下标 6 —— '
            f'标记写着「认证信息已隐藏」，实际打出了口令。'
        )
        assert '***' in out

    def test_password_equals_form_is_masked(self):
        out = redact_command_args(['git', 'clone', f'--password={FAKE_PWD}', 'repo'])
        assert FAKE_PWD not in out, out

    def test_order_independence(self):
        """凭据出现在任何位置都必须被脱敏（原实现失败的根本原因就是依赖位置）。"""
        variants = [
            ['svn', 'cleanup', '/p', '--username', 'u', '--password', FAKE_PWD],
            ['svn', 'cleanup', '--password', FAKE_PWD, '--username', 'u', '/p'],
            ['svn', '--password', FAKE_PWD, 'cleanup', '/p'],
            ['x', 'y', 'z', 'w', 'v', '--password', FAKE_PWD],
        ]
        for cmd in variants:
            out = redact_command_args(cmd)
            assert FAKE_PWD not in out, f'口令泄漏（cmd={cmd}）→ {out}'

    def test_url_embedded_credentials_are_masked(self):
        out = redact_command_args(
            ['git', 'clone', f'https://oauth2:{FAKE_TOKEN}@git.example.com/g/r.git', 'dst']
        )
        assert FAKE_TOKEN not in out, out
        assert 'oauth2:***@' in out

    def test_token_flag_is_masked(self):
        out = redact_command_args(['tool', '--token', FAKE_TOKEN])
        assert FAKE_TOKEN not in out, out

    def test_non_secret_args_are_kept_for_debugging(self):
        """反向保险：非敏感参数必须保留 —— 全打码会让日志失去排查价值。"""
        cmd = ['svn', 'cleanup', '/repos/proj_repo_1', '--non-interactive']
        out = redact_command_args(cmd)
        assert '/repos/proj_repo_1' in out
        assert '--non-interactive' in out
        assert 'cleanup' in out

    def test_username_is_kept(self):
        """用户名不算敏感，保留它才能排查「是谁的凭据过期了」。"""
        out = redact_command_args(['svn', 'log', '--username', 'alice', '--password', 'x'])
        assert 'alice' in out

    def test_none_and_string_inputs(self):
        assert redact_command_args(None) == ''
        out = redact_command_args(f'--password {FAKE_PWD}')
        assert FAKE_PWD not in out, out


class TestSanitizeText:
    """自由文本脱敏（日志行、异常字符串）。"""

    @pytest.mark.parametrize('text', [
        'svn log --username alice --password s3cr3t',
        'svn log --username=alice --password=s3cr3t -r 1:HEAD',
        '--config-option servers:global:http-proxy-password=s3cr3t',
        # TimeoutExpired 的消息就是 Python 列表 repr 形态：'--password', 's3cr3t'
        "Command '['svn', 'cleanup', '/p', '--password', 's3cr3t']' timed out after 120 seconds",
        'tool --token s3cr3t --other x',
    ])
    def test_parameter_form_secrets_are_masked(self, text):
        out = sanitize_text(text)
        assert 's3cr3t' not in out, f'口令未被脱敏：{out!r}'

    def test_bare_user_colon_pass_is_a_known_limitation(self):
        """裸 `alice:s3cr3t`（既无 --flag 也无 URL 包裹）**不**脱敏 —— 这是刻意的。

        日志里到处是 `12:30:45` 这类时间戳，用「单词:单词」去匹配口令会把所有时间
        都打码，反而毁掉排查能力。处理 argv 请用 redact_command_args（按参数名判定）。
        本用例把这个取舍固定下来，免得后来者以为漏了。
        """
        text = 'svn -r alice:s3cr3t https://svn.example.com/repo'
        assert 's3cr3t' in sanitize_text(text), (
            '如果这里开始脱敏了，请确认不会误伤日志时间戳（12:30:45），'
            '并同步更新本用例与 sanitize_text 的文档。'
        )

    def test_url_with_slash_in_token(self):
        out = sanitize_text(f'https://oauth2:{FAKE_TOKEN}@github.com/x/y.git')
        assert 'ghp_AAA' not in out, out

    def test_url_with_at_in_password(self):
        """口令含 @ 时不得漏出尾部片段（原实现会输出 `...:***@ss@host`）。"""
        out = sanitize_text('https://oauth2:p@ss@git.example.com/x.git')
        assert 'ss@git.example.com' not in out, (
            f'口令尾部片段泄漏：{out}\n'
            f'原正则 `[^@/\\s]+` 无法跨越口令里的 @，只吃掉了第一段。'
        )
        assert 'git.example.com' in out, '主机名要保留（否则日志没用了）'

    def test_plain_text_is_unchanged(self):
        assert sanitize_text('svn update /repos/proj_repo_1') == 'svn update /repos/proj_repo_1'


class TestSvnTimeoutDoesNotLeak:
    """超时路径不得把异常对象插值进消息 —— TimeoutExpired 自带整条命令行。"""

    def test_cleanup_timeout_message_has_no_command_line(self, monkeypatch):
        import subprocess

        from services import svn_service

        class _Repo:
            id = 1
            name = 'r'
            url = 'https://user:secret@svn.example.com/svn'
            username = 'alice'
            password = 's3cr3t'
            project = type('P', (), {'code': 'P'})()

        svc = svn_service.SVNService.__new__(svn_service.SVNService)
        svc.repository = _Repo()
        svc.repository_username = 'alice'
        svc.repository_password = 's3cr3t'
        svc.repository_url = _Repo.url
        svc.local_path = '/repos/P_r_1'
        svc.svn_executable = 'svn'

        def _boom(*a, **k):
            raise subprocess.TimeoutExpired(
                ['svn', 'cleanup', '/repos/P_r_1', '--username', 'alice',
                 '--password', 's3cr3t', '--non-interactive'],
                120,
            )

        monkeypatch.setattr(subprocess, 'run', _boom)

        ok, msg = svc._run_svn_cleanup()

        assert ok is False
        assert 's3cr3t' not in msg, (
            f'超时消息里带出了明文口令：{msg!r}\n'
            f'TimeoutExpired.__str__ 会把整条命令行拼进异常信息，'
            f'而调用方以 force=True 把它写进 logs/runlog.log（持久化、保留 10 份备份）。'
        )


class TestAgentSideRedaction:
    """Agent 侧的回传文本会落进 AgentTask.error_message 并显示在管理页面上。"""

    def test_git_url_token_is_masked(self):
        import importlib.util

        path = os.path.join(PROJECT_ROOT, 'agent', 'handlers', 'auto_sync.py')
        spec = importlib.util.spec_from_file_location('agent_auto_sync_probe', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        msg = module._format_git_failure(
            ['git', 'clone', '--branch', 'master',
             f'https://alice:{FAKE_TOKEN}@git.example.com/g/ConfigRepo.git', 'repos/x'],
            'fatal: Authentication failed',
        )
        assert FAKE_TOKEN not in msg, (
            f'Agent 回传文本里带出了明文 token：{msg}\n'
            f'该文本会作为 AgentTask.error_message 落库并显示在管理页面上，'
            f'绕过了 models/repository.py 的落库加密。'
        )
        assert 'git.example.com' in msg, '主机名应保留以便排查'

    def test_svn_cmd_url_is_masked(self):
        """svn 失败消息里的 URL 同样不得带凭据。"""
        import importlib.util

        path = os.path.join(PROJECT_ROOT, 'agent', 'handlers', 'auto_sync.py')
        spec = importlib.util.spec_from_file_location('agent_auto_sync_probe2', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        text = f'svn cmd failed: svn cat https://u:{FAKE_TOKEN}@svn.example.com/r ...'
        assert FAKE_TOKEN not in module._redact_secrets(text)
