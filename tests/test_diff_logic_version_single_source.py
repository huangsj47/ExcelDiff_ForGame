# -*- coding: utf-8 -*-
"""`DIFF_LOGIC_VERSION` 的两个字面量必须一致。

## 为什么需要这个测试

这个版本号在本仓库里有**两处独立的字面量**：

* `app.py` —— **驱动缓存失效**的那一份。`app.py` 用它构造
  `DiffCache` / `ExcelHtmlCache` / `WeeklyVersionExcelCache` 的 `diff_version`，
  缓存的查询条件里带着它（见 `services/excel_diff_cache_service.py:254`、
  `services/excel_html_cache_service.py:84`）。改了它，旧缓存才会失效重算。
* `config.py` —— **版本升级的变更记录**。它原先的生产读者是
  `tasks/cache_cleanup.py`（用它删除版本不匹配的缓存），而那个模块连带整个
  `tasks/` 包没有任何调用者，已整包删除。

**所以本测试现在守的是「防漂移」，不是「防静默错误」。** 两份不一致不会再造成运行时
后果（没有代码按 config.py 那一份做任何判断），它只会在本文件上报红。留着这条断言是
因为它便宜，且那个数字是全仓唯一一处按版本号记着每次口径变化的地方 —— 漂了会误导人。
先前这里写着「只改 config.py → 缓存不失效、修复看不出效果」，那句话在
`tasks/cache_cleanup.py` 被删之后已经不成立了，别再照着它推理。

## 这个测试断言什么

1. 两处字面量相等；
2. 版本号是可比较的三段式（`X.Y.Z`），避免写成 `1.9` / `v1.9.0` 导致
   后续想按数值比较时静默失配；
3. `app.py` 确实是驱动缓存的那一份（它把这个常量传给了缓存服务的构造），
   否则上面的推理前提就不成立 —— 这条能发现「有人把缓存改成读 config.py 那份、
   却漏改了本测试的假设」。
"""
import os
import re
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _read_literal(filename):
    """从源码里取出 `DIFF_LOGIC_VERSION = "..."` 的字面量（不 import，避免副作用）。"""
    path = os.path.join(PROJECT_ROOT, filename)
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            match = re.match(r'^DIFF_LOGIC_VERSION\s*=\s*["\']([^"\']+)["\']', line)
            if match:
                return match.group(1)
    raise AssertionError(f'{filename} 里找不到 DIFF_LOGIC_VERSION 字面量')


def test_two_literals_are_identical():
    app_version = _read_literal('app.py')
    config_version = _read_literal('config.py')
    assert app_version == config_version, (
        f'DIFF_LOGIC_VERSION 两处不一致：app.py={app_version!r}，config.py={config_version!r}。\n'
        f'app.py 那份驱动缓存失效（缓存键、界面展示、启动清理都用它）；'
        f'config.py 那份是版本变更记录、没有生产读者了。\n'
        f'不一致不会再造成运行时后果（见本文件 docstring），但那个数字是全仓唯一一处'
        f'按版本号记着每次口径变化的地方，漂了会误导后来者。请同时修改这两处。'
    )


def test_version_is_three_part_numeric():
    version = _read_literal('app.py')
    assert re.fullmatch(r'\d+\.\d+\.\d+', version), (
        f'DIFF_LOGIC_VERSION={version!r} 不是 X.Y.Z 三段式。\n'
        f'写成 1.9 / v1.9.0 这类形式会让后续想按数值比较版本时静默失配。'
    )


def test_app_py_literal_is_the_one_driving_cache():
    """锁定「app.py 那份才是缓存键」这一前提，防止推理前提被改掉而测试仍绿。"""
    with open(os.path.join(PROJECT_ROOT, 'app.py'), encoding='utf-8') as fh:
        source = fh.read()
    assert 'diff_logic_version=DIFF_LOGIC_VERSION' in source, (
        'app.py 里不再用 DIFF_LOGIC_VERSION 构造缓存服务了。\n'
        '本测试文件的前提是「app.py 那份驱动缓存失效」；'
        '若缓存键改由别处提供，请同步更新本文件的说明与断言。'
    )
