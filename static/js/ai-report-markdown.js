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

    function escapeProse(text) {
        // The structured writer HTML-escapes prose before storing Markdown. Decode
        // one standard entity layer, then escape the complete text again so an
        // encoded HTML tag remains text. Never decode recursively or after escaping.
        var entities = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", '#39': "'" };
        return escapeHtml(String(text).replace(/&(amp|lt|gt|quot|apos|#39);/g, function (_, name) {
            return entities[name];
        }));
    }

    // 行内标记。入参**必须已经转义**，因此 `$1` 里不会出现原始尖括号。
    function inline(escaped) {
        return escaped
            // Encode escaped punctuation as character references before matching
            // emphasis/code. This both displays the original character and keeps
            // literal \* / \` from becoming formatting or executable markup.
            .replace(/\\([\\`*_{}\[\]<>#+.!|~-])/g, function (_, character) {
                return '&#' + character.charCodeAt(0) + ';';
            })
            .replace(/`([^`]+)`/g, '<code>$1</code>')
            .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    }

    function render(markdown) {
        var source = String(markdown === null || markdown === undefined ? '' : markdown)
            .replace(/\r\n?/g, '\n');
        // 先整体转义，再按行解析。转义不会动 `#`/`-`/`>`/`|`/数字这些行首字符，
        // 所以块的判定不受影响；唯一的例外是 `>` 会变成 `&gt;`，引用分支按转义后的
        // 形式匹配（见下面 quote 那条正则）。
        var lines = escapeProse(source).split('\n');

        var out = [];
        var list = null;   // { tag: 'ul' | 'ol', items: [] }
        var para = [];
        var fence = null;  // 围栏代码块累积中

        function flushPara() {
            if (!para.length) return;
            // 段落内的换行**保留成换行**（`<br>`），不要按标准 Markdown 的软换行规则
            // 并成一段。这份报告的作者（模型与平台）就是按「一行一条」写的：一行一条
            // 风险、一行一条测试建议、一行一条断言。并起来的后果是真机 run 70 实测的
            // 「上次遗留（仍成立）」—— **25 行 / 2195 字并成一段**，读者拿到的是连成
            // 一片的长文（用户报的正是「很多报告内容没有换行，不方便阅读」）。
            //
            // 也不能用空串拼接：中文多一个空格只是观感，而西文若用空串会把两个单词
            // 粘成一个（那是真的改变了内容）。保留换行则两种语言都对。
            //
            // 与「`white-space: pre-wrap` 不许用」不冲突：那条禁的是把**标签之间的
            // 空白**也当正文渲染，而这里是渲染器自己产出的换行。
            out.push('<p>' + inline(para.join('<br>')) + '</p>');
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

            // 列表项之后的**缩进行**（且它自己不是另一种块标记）属于上面那一条 ——
            // 这是标准 Markdown 的续行语义，之前被丢掉了：`trim()` 之后缩进没了，
            // 那一行于是变成同级条目，读者分不清哪几行属于同一条结论。
            //
            // 平台拼的报告正是这么写的（`## 复核标注` 的「理由 / 依据 / 平台说明 /
            // 复核方式」各占一行、缩进两格）；渲染成同一个 `<li>`，行与行之间用 `<br>`，
            // 一整条结论因此是一个块，而不是散在列表里的五个同级项。
            if (list && /^\s+\S/.test(line)) {
                list.items[list.items.length - 1] += '<br>' + trimmed;
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

    // 阅读层只识别 Markdown 章节边界与明确的标题，不把正文解析成风险事实、数量或处置状态。
    // 原文仍可按原顺序展开，render() 的安全子集和既有调用保持不变。
    var readerSequence = 0;

    function headingText(title) {
        return title.replace(/\*\*/g, '').replace(/^\s*(?:[一二三四五六七八九十]+[、.．]|\d+[、.．)）])\s*/, '').trim();
    }

    function chapterKind(title) {
        var text = headingText(title);
        if (/^(?:本次覆盖与缺口|覆盖摘要|覆盖情况)(?:[（(].*[）)])?$/.test(text)) return 'coverage';
        if (/^运行提示（平台）$/.test(text)) return 'notice';
        if (/^(?:风险评估|风险清单|风险与待确认事项)$/.test(text)) return 'risk';
        if (/^信息缺口与待确认$/.test(text)) return 'gaps';
        if (/^(?:待确认事项|待确认问题|待确认)$/.test(text)) return 'questions';
        if (/^测试建议$/.test(text)) return 'tests';
        return 'context';
    }

    function readerHeadings(lines) {
        var fenced = false;
        var headings = [];
        lines.forEach(function (line, index) {
            if (/^\s*```/.test(line)) { fenced = !fenced; return; }
            if (fenced) return;
            var match = /^(#{1,6})\s+(.+)$/.exec(line.trim());
            // 覆盖段是平台现有的粗体块标签；只接受这一个完整标签，不能扫描普通正文猜事实。
            var coverage = /^\*\*(本次覆盖与缺口(?:（平台补充）)?)\*\*$/.exec(line.trim());
            if (match || coverage) {
                headings.push({ index: index, level: match ? match[1].length : 1,
                    title: match ? match[2] : coverage[1], kind: chapterKind(match ? match[2] : coverage[1]) });
            }
        });
        return headings;
    }

    function renderCards(lines, kind) {
        if (kind !== 'risk' && kind !== 'questions' && kind !== 'tests') return render(lines.join('\n'));
        var headings = readerHeadings(lines).filter(function (heading) {
            // 只给规范风险标题加卡片，不根据 R 编号所在的普通段落生成业务数据。
            return /^(?:\*\*)?R(?:\d+|-[0-9a-f]{12})(?=[\s（(:：、.])/i.test(heading.title)
                || (kind === 'tests' && /^验证\s+R(?:\d+|-[0-9a-f]{12})\b/i.test(heading.title));
        });
        if (!headings.length) return render(lines.join('\n'));
        var html = render(lines.slice(0, headings[0].index).join('\n'));
        headings.forEach(function (heading, index) {
            var end = index + 1 < headings.length ? headings[index + 1].index : lines.length;
            html += '<article class="' + (kind === 'tests' ? 'ai-reader-action-card' : 'ai-reader-risk-card') + '">'
                + render(lines.slice(heading.index, end).join('\n')) + '</article>';
        });
        return html;
    }

    function renderReader(markdown) {
        var source = String(markdown === null || markdown === undefined ? '' : markdown).replace(/\r\n?/g, '\n');
        if (!source.trim()) return '';
        var lines = source.split('\n');
        var headings = readerHeadings(lines);
        var ordinary = headings.filter(function (heading) {
            return heading.kind === 'context' && !/^R\d+(?=[\s（(:：])/i.test(heading.title);
        });
        var level = Math.min.apply(null, (ordinary.length ? ordinary : headings).map(function (heading) { return heading.level; }));
        var chapters = headings.filter(function (heading) {
            return heading.level <= level || heading.kind === 'coverage' || heading.kind === 'notice' || heading.kind === 'gaps';
        });
        if (!chapters.length) return '<div class="ai-report-reader">' + render(source) + '</div>';
        var prefix = 'ai-reader-' + (++readerSequence) + '-';
        var sections = chapters.map(function (chapter, index) {
            return { title: chapter.title, kind: chapter.kind, order: index, id: prefix + index,
                lines: lines.slice(chapter.index + 1, index + 1 < chapters.length ? chapters[index + 1].index : lines.length) };
        });
        var priority = { coverage: 0, notice: 1, risk: 2, gaps: 3, questions: 4, tests: 5, context: 6 };
        sections.sort(function (a, b) { return priority[a.kind] - priority[b.kind] || a.order - b.order; });
        var nav = sections.map(function (section) {
            return '<a href="#' + section.id + '">' + inline(escapeProse(section.title)) + '</a>';
        }).join('');
        var html = '<div class="ai-report-reader"><nav class="ai-reader-nav" aria-label="报告章节">' + nav + '</nav>';
        html += render(lines.slice(0, chapters[0].index).join('\n'));
        sections.forEach(function (section) {
            var title = inline(escapeProse(section.title));
            // 覆盖和缺口整体常显；仅压缩排版，不把未取证量藏进折叠层。
            var body = renderCards(section.lines, section.kind);
            var classes = 'ai-reader-section ai-reader-' + section.kind;
            // Only this explicit canonical gap chapter is promoted from a nested
            // heading; its contents remain verbatim and reuse the notice styling.
            if (section.kind === 'gaps') classes += ' ai-reader-notice';
            if (section.kind === 'coverage' || section.kind === 'notice' || section.kind === 'gaps') {
                html += '<section class="' + classes + '" id="' + section.id + '" tabindex="-1"><h2>' + title + '</h2>' + body + '</section>';
            } else {
                var expanded = section.kind === 'risk' || section.kind === 'questions' || section.kind === 'tests' || sections.length === 1;
                html += '<details class="' + classes + '" id="' + section.id + '" tabindex="-1"' + (expanded ? ' open' : '')
                    + '><summary>' + title + '</summary><div class="ai-reader-section-body">' + body + '</div></details>';
            }
        });
        html += '<details class="ai-reader-archive"><summary>查看存档原文（原顺序）</summary>' + render(source) + '</details></div>';
        return html;
    }

    function bindReader(container) {
        if (!container || !container.querySelectorAll) return;
        Array.prototype.forEach.call(container.querySelectorAll('.ai-reader-nav a'), function (link) {
            link.addEventListener('click', function (event) {
                // href 是本渲染器生成的数字 ID；查询限定在当前报告，不使用模型提供的选择器。
                var target = container.querySelector(link.getAttribute('href'));
                if (!target) return;
                event.preventDefault();
                if (target.tagName.toLowerCase() === 'details') target.setAttribute('open', '');
                if (target.focus) target.focus({ preventScroll: true });
                if (target.scrollIntoView) target.scrollIntoView({ block: 'start' });
            });
        });
    }

    global.AiReportMarkdown = { escapeHtml: escapeHtml, render: render, renderReader: renderReader, bindReader: bindReader };
}(typeof window !== 'undefined' ? window : globalThis));
