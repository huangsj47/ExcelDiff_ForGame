/* AI 分析报告的 Markdown 渲染（安全子集）。
 *
 * ---------------------------------------------------------------------------
 * 为什么是自己写的、而不是引 marked / markdown-it
 * ---------------------------------------------------------------------------
 * 1) 汇入的是**外部模型返回的不可信文本**，而这个仓库没有 CSP、也没有任何消毒器
 *    （`static/js/` 下只有 main.js / diff-handlers.js，全仓 grep 不到 DOMPurify）。
 *    把未消毒的渲染结果写进 innerHTML 就是开一个 XSS 口子。
 * 2) 报告的结构是**固定子集**：七个一级标题 + `-` 列表 + 少量 `**加粗**`。为这点
 *    需求引一个完整 Markdown 解析器 + 一个消毒器，是把攻击面换成了「相信第三方」。
 *
 * 这里的做法是**先整体转义、再在白名单上套标签**：输出里的每一个 `<` 都来自本文件
 * 自己写的字面量，模型给的内容永远以实体形式出现。XSS 由构造保证，不靠消毒器兜底。
 *
 * 只覆盖用到的子集：`#`~`######` 标题、`-`/`*`/`+` 与 `1.` 列表、`>` 引用、
 * ``` 围栏代码、`**粗体**`、`` `行内码` ``。**刻意不支持的**：链接、图片、原始 HTML、
 * 表格。不支持的写法会退化成普通文字显示（不会消失、也不会变成标签）。
 */
(function (global) {
    'use strict';

    function escapeHtml(text) {
        return String(text === null || text === undefined ? '' : text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // 行内标记。入参**必须已经转义**，因此 `$1` 里不会出现原始尖括号。
    function inline(escaped) {
        return escaped
            .replace(/`([^`]+)`/g, '<code>$1</code>')
            .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    }

    function render(markdown) {
        var source = String(markdown === null || markdown === undefined ? '' : markdown)
            .replace(/\r\n?/g, '\n');
        // 先整体转义，再按行解析。转义不会动 `#`/`-`/`>`/`|`/数字这些行首字符，
        // 所以块的判定不受影响；唯一的例外是 `>` 会变成 `&gt;`，引用分支按转义后的
        // 形式匹配（见下面 quote 那条正则）。
        var lines = escapeHtml(source).split('\n');

        var out = [];
        var list = null;   // { tag: 'ul' | 'ol', items: [] }
        var para = [];
        var fence = null;  // 围栏代码块累积中

        function flushPara() {
            if (!para.length) return;
            // 段落内的软换行用空格接起来：中文多一个空格只是观感，而西文若用空串
            // 拼接会把两个单词粘成一个（那是真的改变了内容）。
            out.push('<p>' + inline(para.join(' ')) + '</p>');
            para = [];
        }
        function flushList() {
            if (!list) return;
            var items = list.items.map(function (item) {
                return '<li>' + inline(item) + '</li>';
            }).join('');
            out.push('<' + list.tag + '>' + items + '</' + list.tag + '>');
            list = null;
        }
        function flushAll() {
            flushPara();
            flushList();
        }

        for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            var trimmed = line.trim();

            // 围栏代码块：内容原样输出（已转义），不参与块解析
            if (/^```/.test(trimmed)) {
                if (fence === null) {
                    flushAll();
                    fence = [];
                } else {
                    out.push('<pre><code>' + fence.join('\n') + '</code></pre>');
                    fence = null;
                }
                continue;
            }
            if (fence !== null) {
                fence.push(line);
                continue;
            }

            if (!trimmed) {
                flushAll();
                continue;
            }

            var heading = /^(#{1,6})\s+(.+)$/.exec(trimmed);
            if (heading) {
                flushAll();
                var level = heading[1].length;
                out.push('<h' + level + '>' + inline(heading[2]) + '</h' + level + '>');
                continue;
            }

            var bullet = /^[-*+]\s+(.+)$/.exec(trimmed);
            if (bullet) {
                flushPara();
                if (!list || list.tag !== 'ul') {
                    flushList();
                    list = { tag: 'ul', items: [] };
                }
                list.items.push(bullet[1]);
                continue;
            }

            var ordered = /^\d+[.)]\s+(.+)$/.exec(trimmed);
            if (ordered) {
                flushPara();
                if (!list || list.tag !== 'ol') {
                    flushList();
                    list = { tag: 'ol', items: [] };
                }
                list.items.push(ordered[1]);
                continue;
            }

            // 转义后 `>` 已经是 `&gt;`
            var quote = /^&gt;\s?(.*)$/.exec(trimmed);
            if (quote) {
                flushAll();
                out.push('<blockquote>' + inline(quote[1]) + '</blockquote>');
                continue;
            }

            flushList();
            para.push(trimmed);
        }

        // 未闭合的围栏：当作代码块收尾，不要把它整段吞掉
        if (fence !== null && fence.length) {
            out.push('<pre><code>' + fence.join('\n') + '</code></pre>');
        }
        flushAll();
        return out.join('');
    }

    global.AiReportMarkdown = { escapeHtml: escapeHtml, render: render };
}(typeof window !== 'undefined' ? window : globalThis));
