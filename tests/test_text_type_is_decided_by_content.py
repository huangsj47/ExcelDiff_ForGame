# -*- coding: utf-8 -*-
"""文件类型**由内容兜底判定**：扩展名不认识时，别把纯文本说成二进制。

## 这条是被一次真实报告逼出来的

一次周版本 AI 分析的「信息缺口」里写着「**未读到任何代码 diff 与协议 diff**，
`code_logic`、`version_branch` 两个维度无法判断」。协议定义文件（`.proto`）不在
`DiffService.TEXT_EXTENSIONS` 里，而那个清单是**唯一**的判据，于是：

* `get_file_type('x.proto')` → `binary`
* `_process_binary_diff` → 载荷是「二进制文件无法显示差异内容」
* AI 的渲染层（`services/ai/platform_provider.py::_render_binary`）照实转述给模型

**页面上与 AI 眼里同时看不见这份 diff**，而它其实是纯文本、逐行可比。往清单里补一个
`.proto` 只是把同一类问题推到下一次（`.toml`、`.ts`… 迟早还会有），所以判据换成
**内容**：「没有 NUL，且能按 UTF-8/GBK 严格解码」就是文本。

## 三条不能破的既有性质

1. 认识的扩展名一律按清单走 —— 内容兜底**只对认不出来的扩展名生效**，
   免得一个恰好能解码的 `.bin` 被当文本。
2. 只拿得到路径的调用点（页面元数据）行为**一个字不变**：不传 `content` 就没有第二眼。
3. 真二进制仍然判二进制（`tests/test_business_chain_integration.py` 与
   `test_business_flow_comprehensive.py` 早就钉着 `.bin`/`.zip`/`.tar.gz`/`.woff2`）。
"""
from __future__ import annotations

import pytest

from services.diff_service import DiffService, looks_like_text

PROTO_BEFORE = b'''syntax = "proto3";
package game;

message LoginReq {
  string account = 1;
  int32 channel = 2;
}
'''

PROTO_AFTER = b'''syntax = "proto3";
package game;

message LoginReq {
  string account = 1;
  int32 channel = 2;
  string token = 3;
}
'''


@pytest.fixture()
def service() -> DiffService:
    return DiffService()


class TestTheUnknownExtensionGetsASecondLook:
    def test_a_proto_file_is_text_so_its_diff_is_visible(self, service):
        """**这一条就是那个「未读到协议 diff」的回归。**

        断言分两层：类型判对（text），以及**产物里真的有差异内容** —— 只断言类型的话，
        某天渲染层再把 `text` 载荷丢成空串，这条测试照样是绿的。
        """
        assert service.get_file_type('proto/login.proto', PROTO_AFTER) == 'text'

        payload = service.process_diff('proto/login.proto', PROTO_AFTER, PROTO_BEFORE)

        assert payload['type'] == 'text', payload
        assert 'token' in payload['raw_diff'], payload['raw_diff']
        # 二进制那条分支的话术一个字都不许出现。
        assert '二进制' not in str(payload.get('message') or '')

    def test_the_ai_renderer_no_longer_calls_it_binary(self, service):
        """端到端：AI 读的就是这份载荷。

        `render_diff_payload` 按载荷里的 `type` 分派 —— 类型判错时模型收到的是一句
        「二进制文件无法显示差异内容」，它只能据此写「未读到协议 diff」。
        """
        from services.ai.platform_provider import render_diff_payload

        payload = service.process_diff('proto/login.proto', PROTO_AFTER, PROTO_BEFORE)
        rendered = render_diff_payload(payload, path='proto/login.proto')

        assert rendered, '渲染不出来 —— 模型会写「取数失败」'
        assert 'token' in rendered
        assert '二进制' not in rendered

    def test_content_beats_the_extension_list_only_for_unknown_types(self, service):
        """认识的扩展名**一律按清单走**，清单外的才看内容。

        `.bin` 是清单外的扩展名，所以判据是它的**字节**：纯 ASCII 的 `.bin` 就是文本
        （行级 diff 它有意义的），含 NUL 的 `.bin` 才是二进制。这不是漏洞 ——
        「按内容判」的题中之义就是不认名字。真二进制由 NUL 兜住（下一条用例）。
        """
        # 认识的扩展名：内容说什么都不影响（`.png` 是图片、`.xlsx` 是配表）。
        assert service.get_file_type('x.png', b'\x89PNG\r\n\x1a\n') == 'image'
        assert service.get_file_type('x.xlsx', b'anything at all') == 'excel'
        assert service.get_file_type('x.lua', b'return 1') == 'text'
        # 清单外：看内容。
        assert service.get_file_type('x.bin', b'hello world\n' * 10) == 'text'
        assert service.get_file_type('x.bin', b'hello\x00world') == 'binary'
        assert service.get_file_type('x.zip', b'PK\x03\x04\x00\x00\x00\x00') == 'binary'

    def test_a_path_only_call_site_keeps_the_old_answer(self, service):
        """不传 `content` 就没有第二眼（页面元数据那些调用点的行为不变）。"""
        assert service.get_file_type('proto/login.proto') == 'binary'
        assert service.get_file_type('a/b/c.unknown') == 'binary'

    def test_gbk_chinese_source_is_text(self, service):
        """中文项目的 GBK 源码/配置很常见，与 `_decode_text` 的编码清单同源。"""
        gbk = 'message 登录请求 {\n  string 账号 = 1;\n}\n'.encode('gbk')

        assert service.get_file_type('proto/login.proto', gbk) == 'text'
        assert service.process_diff('proto/login.proto', gbk, None)['type'] == 'text'

    def test_a_multibyte_char_cut_by_the_sniff_window_is_still_text(self, service):
        """样本正好切在一个汉字中间时，严格解码会抛错 —— 于是「明明是文本却判成二进制」。

        用增量解码器（`final=False`）容忍结尾不完整，只对真正非法的字节报错。
        """
        # 让一个三字节的汉字正好跨在 8192 的边界上。
        filler = 'a' * 8191
        content = (filler + '中' + 'x' * 100).encode('utf-8')
        assert content[8191] != 0  # 汉字确实跨在边界上，测试的前提成立

        assert looks_like_text(content) is True
        assert service.get_file_type('conf/未知.txtx', content) == 'text'

    def test_binary_junk_stays_binary_even_with_a_recognizable_prefix(self, service):
        """NUL 是二进制最可靠的信号：开头是文本、后面是二进制的内容仍然是二进制。

        反过来（只看开头几个字节能不能解码）会把这类文件判成文本，然后拿二进制乱码
        去跑行级 diff —— 模型与评审者都会看到一段像代码、实际不存在的内容。
        """
        content = b'# generated file\n' + b'\x00\x01\x02\x03' * 100

        assert looks_like_text(content) is False
        assert service.get_file_type('out/generated.unknown', content) == 'binary'

    def test_empty_and_missing_content_do_not_crash(self, service):
        """空内容与 `None` 都没有「是不是文本」可言，按既有语义留在二进制分支。"""
        assert looks_like_text(None) is False
        assert looks_like_text(b'') is False
        assert service.get_file_type('proto/login.proto', b'') == 'binary'
        assert service.get_file_type('proto/login.proto', None) == 'binary'
