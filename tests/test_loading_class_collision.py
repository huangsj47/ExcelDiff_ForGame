# -*- coding: utf-8 -*-
"""`.loading` 这个「圆点 spinner」工具类被当成容器状态类用的守卫。

## 缺陷形态（线上可见）

`static/css/style.css` 里的 `.loading` 是给**空的指示元素**用的圆点：

    .loading { display:inline-block; width:20px; height:20px;
               border:3px solid #f3f3f3; border-top-color:#3498db;
               border-radius:50%; animation: spin 1s linear infinite; }

`templates/commit_diff.html` 却把它加到了整个 diff 容器上
（`#excel-diff-container`）。容器于是变成一颗 20×20 的圆点并**持续旋转**；
而容器里的内容远超 20px、又没有任何裁剪，就无遮挡地跟着一起转 ——
点「上一个版本」时整页内容像一条斜着的条带在打转（用户报的就是这个）。

容器自己的 display 保得住（`hideExcelLoadingIndicator` 用内联
`style.display='block'`），所以看起来「页面还在」，但 width/height/border-radius
和 **animation** 全都吃到了。

## 为什么必须专项钉住

两件事都**不报错**：CSS 少一个 `}` 会让它后面所有规则一起失效；类名拼错只是
「少了个反馈」，没有任何异常。而 `.loading` 是全局工具类，将来任何人把它加到
任何容器上都会复发。这里把「容器状态类」与「spinner 工具类」的分工钉死。
"""
from __future__ import annotations

import os
import re

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STYLE_CSS = os.path.join(PROJECT_ROOT, 'static', 'css', 'style.css')
COMMIT_DIFF = os.path.join(PROJECT_ROOT, 'templates', 'commit_diff.html')
SCROLL_FIX_CSS = os.path.join(PROJECT_ROOT, 'static', 'css', 'excel-scroll-fix.css')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _strip_comments(css: str) -> str:
    """先去掉 /* */ 注释再断言。

    注释里常常**举例**写了要禁掉的那个写法（本仓库的注释风格如此），
    不剥离就会把说明文字误判成真实声明。
    """
    return re.sub(r'/\*.*?\*/', ' ', css, flags=re.S)


def _rules(css: str):
    """产出 (选择器, 声明字典)。够用即可：本仓库的 CSS 没有嵌套规则。"""
    out = []
    for selector, body in re.findall(r'([^{}]+)\{([^{}]*)\}', _strip_comments(css)):
        decls = {}
        for decl in body.split(';'):
            if ':' not in decl:
                continue
            prop, _, value = decl.partition(':')
            decls[prop.strip().lower()] = value.strip()
        out.append((selector.strip(), decls))
    return out


def _all_css_text() -> str:
    css_dir = os.path.join(PROJECT_ROOT, 'static', 'css')
    return '\n'.join(_read(os.path.join(css_dir, name)) for name in sorted(os.listdir(css_dir))
                     if name.endswith('.css'))


def test_no_rule_gives_the_diff_container_a_spinner_animation():
    """容器级规则不得带 `animation: spin`。

    这是本缺陷的核心：容器的状态规则只该调 opacity / transform 这类观感，
    一旦带上 spinner 的旋转动画，整个容器就会打转。
    """
    offenders = []
    for selector, decls in _rules(_read(STYLE_CSS)):
        if 'excel-diff-container' not in selector:
            continue
        animation = decls.get('animation', '')
        if 'spin' in animation:
            offenders.append((selector, animation))
    assert not offenders, (
        '给 .excel-diff-container 的规则里出现了 spinner 旋转动画，'
        f'容器会整个转起来：{offenders}'
    )


def test_no_rule_shrinks_the_diff_container_to_a_spinner_dot():
    """同理：容器不得被压成 20px 的小圆点。

    `.loading` 的 width/height/border-radius 会一起命中容器，把内容挤成一个点。
    """
    offenders = []
    for selector, decls in _rules(_read(STYLE_CSS)):
        if 'excel-diff-container' not in selector:
            continue
        if decls.get('width', '').replace(' ', '') == '20px' or \
           decls.get('height', '').replace(' ', '') == '20px':
            offenders.append((selector, decls.get('width'), decls.get('height')))
    assert not offenders, f'.excel-diff-container 被设成了 spinner 的尺寸：{offenders}'


def test_the_container_state_uses_a_non_colliding_class_name():
    """容器状态类必须存在，且不与 `.loading` 撞名。"""
    rules = _rules(_read(STYLE_CSS))
    has_state = any('excel-diff-container.is-loading' in selector for selector, _ in rules)
    assert has_state, (
        'style.css 里没有 .excel-diff-container.is-loading 规则 —— '
        '容器状态类被删掉了，或者又被改回与 spinner 撞名的 loading'
    )


def test_the_old_colliding_selector_is_gone_everywhere():
    """半途而废的改名最容易漏：还有任何一处 `.excel-diff-container.loading` 就算没改完。"""
    leftovers = []
    for selector, _ in _rules(_all_css_text()):
        # 独立成词地匹配 `.excel-diff-container.loading`，避免误伤 `.is-loading`
        if re.search(r'\.excel-diff-container\.loading(?![\w-])', selector):
            leftovers.append(selector)
    assert not leftovers, f'仍有规则在用撞名的 .excel-diff-container.loading：{leftovers}'


def test_commit_diff_puts_the_state_class_on_the_container_not_the_spinner_class():
    """模板侧：加到容器上的必须是 is-loading。

    这条拦的是「CSS 改对了、模板没跟着改」这种半边修复 —— 那样容器既拿不到
    状态样式，又仍然会（若类名未变）吃到 spinner 规则。
    """
    src = _read(COMMIT_DIFF)
    assert "classList.add('is-loading')" in src, (
        'commit_diff.html 没有给容器加 is-loading 状态类'
    )
    assert not re.search(r"classList\.add\(['\"]loading['\"]\)", src), (
        'commit_diff.html 仍然把 spinner 工具类 loading 加到元素上；'
        '加到 #excel-diff-container 上会让整个 diff 容器旋转'
    )
    assert "classList.remove('is-loading')" in src, (
        '加载完成后没有把 is-loading 摘掉，容器会一直停在半透明下移的状态'
    )


def test_the_spinner_utility_itself_is_still_intact():
    """修的是「误用」，不是「把工具类废掉」。

    将来有人为了修这个问题直接把 .loading 的 geometry/animation 删掉，
    会让真正需要圆点 spinner 的地方变成一个静止的方块 —— 这条拦住那种改法。
    """
    target = None
    for selector, decls in _rules(_read(STYLE_CSS)):
        if selector.strip() == '.loading':
            target = decls
            break
    assert target is not None, 'style.css 里找不到 .loading 规则了'
    assert 'spin' in target.get('animation', ''), (
        '.loading 不再旋转了 —— 它是圆点 spinner，动效被误删'
    )
    assert target.get('border-radius') == '50%', '.loading 不再是圆形，spinner 形状被破坏'
    assert target.get('width') == '20px' and target.get('height') == '20px', (
        '.loading 的尺寸被改动，它应当仍是 20×20 的圆点'
    )
