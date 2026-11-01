const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { parseHTML } = require('linkedom');
const vm = require('node:vm');
const { execFileSync } = require('node:child_process');
const root = resolve(__dirname, '../..');
const source = readFileSync(resolve(root, 'static/js/ai-report-markdown.js'), 'utf8');

function setup(markdown, configure) {
    const { window } = parseHTML('<html><body><div id="report"></div></body></html>');
    const runtime = { document: window.document, navigator: window.navigator, HTMLTextAreaElement: window.HTMLTextAreaElement };
    if (configure) configure(runtime);
    vm.runInNewContext(source, { window: runtime });
    window.AiReportMarkdown = runtime.AiReportMarkdown;
    const output = window.document.getElementById('report');
    output.innerHTML = window.AiReportMarkdown.renderReader(markdown);
    window.AiReportMarkdown.bindReader(output);
    return { output, window };
}

test('Markdown tables preserve escaped pipes and keep hostile cells as text', () => {
    const { output } = setup('# 风险评估\n| 字段 | 对比 |\n| :--- | ---: |\n| 名称\\|类型 | <img src=x onerror=bad()> |');
    const table = output.querySelector('table');
    assert.ok(table);
    assert.equal(table.querySelectorAll('th').length, 2);
    assert.equal(table.querySelector('td').textContent, '名称|类型');
    assert.equal(table.querySelectorAll('td')[1].textContent, '<img src=x onerror=bad()>');
    assert.equal(output.querySelector('img,[onerror]'), null);
    assert.equal(table.closest('.ai-reader-table-scroll').getAttribute('tabindex'), '0');
});

test('links allow http and https while code, dangerous schemes and escaped Markdown stay literal', () => {
    const { output } = setup('# 风险评估\n[文档](https://example.com/rules?a=1&b=2) [错](javascript:bad()) [错](data:text/html,bad) `**code** [x](https://example.com)` \\[文字\\](https://example.com)');
    const links = [...output.querySelector('.ai-reader-risk').querySelectorAll('a')];
    assert.equal(links.length, 1);
    assert.equal(links[0].getAttribute('href'), 'https://example.com/rules?a=1&b=2');
    assert.equal(links[0].getAttribute('rel'), 'noopener noreferrer');
    const code = output.querySelector('.ai-reader-risk code');
    assert.equal(code.textContent, '**code** [x](https://example.com)');
    assert.equal(code.querySelector('a,strong'), null);
});

test('fenced code preserves literal entities, backslashes and embedded shorter fences', () => {
    const literal = '&lt;tag&gt;\\path **not bold**\n```\n# 风险评估';
    const { output } = setup('# 风险评估\n## R1（高）\n````json\n' + literal + '\n````\n# 测试建议\n~~~lua\nreturn 1\n~~~');
    const blocks = [...output.querySelectorAll('.ai-reader-section .ai-reader-code')];
    assert.equal(blocks.length, 2);
    assert.equal(blocks[0].querySelector('code').textContent, literal);
    assert.equal(blocks[0].querySelector('.ai-reader-code-language').textContent, 'json');
    assert.equal(output.querySelectorAll('.ai-reader-risk-card').length, 1);
    assert.equal(blocks[1].querySelector('code').textContent, 'return 1');
});

test('inline code preserves entities and backslashes before prose decoding', () => {
    const { output } = setup('# 风险评估\n`&lt;tag&gt;` 与 `C:\\path\\*`，以及 ``a`b``；\\`普通文字\\`');
    const codes = [...output.querySelectorAll('.ai-reader-risk code')];
    assert.deepEqual(codes.map(code => code.textContent), ['&lt;tag&gt;', 'C:\\path\\*', 'a`b']);
    assert.ok(output.querySelector('.ai-reader-risk').textContent.includes('`普通文字`'));
});

test('copy is explicit, copies only raw code and reports completion', async () => {
    const copied = [];
    const { output, window } = setup('# 风险评估\n```json\n{"count":1}\n```', window => {
        Object.defineProperty(window, 'navigator', { value: { clipboard: { writeText: async value => copied.push(value) } }, configurable: true });
    });
    assert.deepEqual(copied, []);
    const button = output.querySelector('.ai-copy-code');
    assert.ok(button);
    button.dispatchEvent(new window.Event('click', { bubbles: true }));
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(copied, ['{"count":1}']);
    assert.equal(button.closest('.ai-reader-code').querySelector('[role="status"]').textContent, '已复制');
});

test('copy failure stays honest and leaves no temporary editable node', async () => {
    const { output, window } = setup('# 风险评估\n```\nunsafe()\n```', window => {
        Object.defineProperty(window, 'navigator', { value: {}, configurable: true });
        window.document.execCommand = () => false;
        window.HTMLTextAreaElement.prototype.select = function () {};
    });
    output.querySelector('.ai-copy-code').dispatchEvent(new window.Event('click', { bubbles: true }));
    await new Promise(resolve => setImmediate(resolve));
    assert.match(output.querySelector('[role="status"]').textContent, /请手动选择代码复制/);
    assert.equal(window.document.querySelector('textarea'), null);
});

test('internal HTTP clipboard fallback copies exact text and repeated binding stays idempotent', async () => {
    const copied = [];
    const { output, window } = setup('# 风险评估\n```lua\nreturn "&lt;value&gt;"\n```', runtime => {
        Object.defineProperty(runtime, 'navigator', { value: {}, configurable: true });
        runtime.HTMLTextAreaElement.prototype.select = function () {};
        runtime.document.execCommand = command => {
            assert.equal(command, 'copy');
            copied.push(runtime.document.querySelector('textarea').value);
            return true;
        };
    });
    window.AiReportMarkdown.bindReader(output);
    output.querySelector('.ai-copy-code').dispatchEvent(new window.Event('click', { bubbles: true }));
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(copied, ['return "&lt;value&gt;"']);
    assert.equal(output.querySelector('[role="status"]').textContent, '已复制');
    assert.equal(window.document.querySelector('textarea'), null);
});

test('images and entity-obfuscated protocols never become network-bearing elements', () => {
    const { output } = setup('# 风险评估\n![remote](https://example.com/pixel) [bad](jav&#97;script:bad) [bad](https://x/&quot;onmouseover=bad)');
    const risk = output.querySelector('.ai-reader-risk');
    assert.equal(risk.querySelector('img,a,iframe'), null);
    assert.match(risk.textContent, /!\[remote\]/);
});

test('real structured report roundtrips escaped coordinates, punctuation and HTML as inert text', () => {
    const literal = 'Config/item_list.xlsx:Sheet1!A1 **literal** `code` <img src=x onerror="bad()"> &lt;raw&gt; & value';
    const script = [
        'import json, sys',
        'from services.ai.report_document import render_structured_report',
        'value = json.loads(sys.argv[1])',
        'item = {"id": "R-012345abcdef", "severity": "medium", "business_area": "测试", "change_fact": value,',
        '"impact_hypothesis": value, "evidence_status": "unverified", "decision_question": value,',
        '"evidence_refs": [{"path": value}], "test_actions": [{"conditions": value, "steps": [value], "expected": value}]}',
        'sys.stdout.buffer.write(render_structured_report([item], {"overview": value}).encode("utf-8"))'
    ].join('\n');
    const markdown = execFileSync('python', ['-c', script, JSON.stringify(literal)], { cwd: root, encoding: 'utf8' });
    const { output } = setup(markdown);
    const change = [...output.querySelectorAll('p')].find(p => p.textContent.startsWith('变化：'));
    assert.equal(change.textContent, '变化：' + literal);
    assert.equal(change.querySelector('strong,code,img'), null);
    assert.equal(output.querySelector('script,img,svg,[onerror],[onload]'), null);
    assert.ok(output.querySelector('.ai-reader-archive').textContent.includes(literal));
});

test('real structured information gaps stay visible in a separate chapter immediately after risks', () => {
    const gap = '共享表 Config/item_list.xlsx 未取回，发布前需确认字段语义';
    const script = [
        'import json, sys',
        'from services.ai.report_document import render_structured_report',
        'sections = {"overview": "人数调整", "release_actions": ["人工确认发布"], "gaps": [json.loads(sys.argv[1])]}',
        'sys.stdout.buffer.write(render_structured_report([], sections).encode("utf-8"))'
    ].join('\n');
    const markdown = execFileSync('python', ['-c', script, JSON.stringify(gap)], { cwd: root, encoding: 'utf8' });
    const { output } = setup(markdown);
    const gaps = output.querySelector('.ai-reader-gaps');
    assert.ok(gaps, 'the canonical H2 gap label must become an independent chapter');
    assert.equal(gaps.closest('details'), null);
    assert.equal(gaps.querySelector('details'), null);
    assert.ok(gaps.textContent.includes(gap));
    assert.ok(gaps.previousElementSibling.classList.contains('ai-reader-risk'));
    const release = [...output.querySelectorAll('details.ai-reader-context')]
        .find(section => section.textContent.includes('上线与回滚关注点'));
    assert.ok(release);
    assert.equal(release.hasAttribute('open'), false);
    assert.ok(!release.textContent.includes(gap));
    assert.ok(output.querySelector('.ai-reader-archive').textContent.includes(gap));
});

test('canonical evidence index is folded separately and never swallowed by visible gaps', () => {
    const identifier = '0123456789abcdef0123';
    const script = [
        'from services.ai.report_document import render_structured_report',
        'item = {"id": "R-012345abcdef", "severity": "high", "business_area": "活动", "change_fact": "日期格式变化",',
        '"impact_hypothesis": "若解析失败则活动无法开启", "evidence_status": "partial", "decision_question": "解析规则是什么？",',
        `"evidence_refs": [{"evidence_id": "${identifier}", "path": "tables/Event.xlsx", "available": True}],`,
        '"test_actions": [{"conditions": "新配置", "steps": ["检查活动入口"], "expected": "待确认"}]}',
        'sysout = render_structured_report([item], {"overview": "活动调整", "gaps": ["解析实现未读取"]})',
        'import sys; sys.stdout.buffer.write(sysout.encode("utf-8"))'
    ].join('\n');
    const markdown = execFileSync('python', ['-c', script], { cwd: root, encoding: 'utf8' });
    const { output } = setup(markdown);
    const evidence = output.querySelector('details.ai-reader-evidence');
    assert.ok(evidence);
    assert.equal(evidence.hasAttribute('open'), false);
    assert.ok(evidence.textContent.includes(identifier));
    assert.ok(!output.querySelector('.ai-reader-risk').textContent.includes(identifier));
    const gaps = output.querySelector('.ai-reader-gaps');
    assert.ok(gaps.textContent.includes('解析实现未读取'));
    assert.ok(!gaps.textContent.includes(identifier));
    assert.equal(output.querySelectorAll('.ai-reader-risk-card').length, 1);
    assert.equal(output.querySelectorAll('.ai-reader-action-card').length, 1);
});

const report = `## 覆盖摘要（平台）
- 本次覆盖：输入 98 项；已取证 17 项；未取证 81 项（仓库、提交、路径）
- 缺口：证据不足，不能推断其余项安全
# 变更理解
${'背景说明与长路径/'.repeat(100)}
# 风险评估
## R2（中，活动）语义待确认
变化：上限 4 → 0
影响：是否不限量尚待确认
依据状态：配置已取证，需求未确认
处理或待确认：请确认 0 的含义
### 测试建议
- 验证 R2：构造边界 → 执行 → 预期未知，先确认需求
## R3（低，展示）显示文本
变化：文本替换
# 测试建议
## 验证 R2
条件：使用边界值
步骤：操作入口
预期：待需求确认
# 待确认事项
- R2：0 是否表示不限量？
# 上线与回滚关注点
需人工判断`;

test('canonical hashed risk IDs produce risk and test cards', () => {
    const canonical = report.replaceAll('R2', 'R-abc123def456').replaceAll('R3', 'R-123456abcdef');
    const { output } = setup(canonical);
    assert.equal(output.querySelectorAll('.ai-reader-risk-card').length, 2);
    assert.equal(output.querySelectorAll('.ai-reader-action-card').length, 1);
});

test('risks precede long folded context and coverage stays last without losing text', () => {
    const { output } = setup(report);
    const sections = [...output.querySelectorAll('.ai-reader-section')];
    assert.match(sections[sections.length - 1].textContent, /覆盖摘要/);
    assert.match(sections[0].textContent, /风险评估/);
    assert.match(sections[1].textContent, /待确认事项/);
    const background = sections.find(section => section.textContent.includes('变更理解'));
    assert.equal(background.tagName, 'DETAILS');
    assert.equal(background.hasAttribute('open'), false);
    assert.match(output.textContent, /未取证 81 项/);
    assert.match(background.textContent, /背景说明与长路径/);
    assert.equal(output.querySelectorAll('.ai-reader-risk-card').length, 2);
    assert.match(output.querySelector('.ai-reader-risk-card').textContent, /变化：上限 4 → 0/);
    assert.match(output.querySelector('.ai-reader-risk-card').textContent, /验证 R2/);
});

test('navigation opens the selected closed chapter and moves keyboard focus to it', () => {
    const { output, window } = setup(report);
    const nav = [...output.querySelectorAll('.ai-reader-nav a')].find(a => a.textContent === '变更理解');
    const section = output.querySelector(nav.getAttribute('href'));
    let focused = false;
    section.focus = () => { focused = true; };
    nav.dispatchEvent(new window.Event('click', { bubbles: true, cancelable: true }));
    assert.equal(section.hasAttribute('open'), true);
    assert.equal(focused, true);
});

test('reader keeps a full archive in original order, with fenced headings left as code', () => {
    const md = '# 变更理解\n原序第一\n```\n# 风险评估\n## R99 这是代码\n```\n# 风险评估\n## R1（高）\n原序第二';
    const { output } = setup(md);
    assert.equal(output.querySelectorAll('.ai-reader-risk-card').length, 1);
    const archive = output.querySelector('.ai-reader-archive');
    assert.equal(archive.hasAttribute('open'), false);
    assert.ok(archive.textContent.indexOf('原序第一') < archive.textContent.indexOf('原序第二'));
    assert.match(archive.querySelector('pre').textContent, /## R99 这是代码/);
});

test('hostile headings, attributes, links and code stay inert in reader and archive', () => {
    const { output } = setup('# 风险评估\n## R1（高）<img src=x onerror="bad()">\n<script>bad()</script>\n# 待确认事项\n**<svg onload=bad()>**\n[jump](javascript:bad())');
    assert.equal(output.querySelector('script,img,svg,[onerror],[onload]'), null);
    assert.equal(output.querySelector('[href^="javascript:"]'), null);
    assert.match(output.textContent, /<img src=x onerror="bad\(\)">/);
    assert.match(output.textContent, /<script>bad\(\)<\/script>/);
});

test('multiple readers use distinct navigation targets and headings without facts remain plain', () => {
    const { output, window } = setup('# 随手记录\nR8（高）只是原文，没有规范标题');
    assert.equal(output.querySelector('.ai-reader-risk-card'), null);
    const second = window.document.createElement('div');
    second.innerHTML = window.AiReportMarkdown.renderReader('# 随手记录\n第二份');
    window.document.body.appendChild(second);
    assert.notEqual(output.querySelector('.ai-reader-nav a').getAttribute('href'), second.querySelector('.ai-reader-nav a').getAttribute('href'));
});

test('coverage details fold at the bottom while degradation stays visible', () => {
    const { window } = parseHTML('<html><body><div id="report"></div></body></html>');
    const sandbox = { window };
    vm.runInNewContext(source, sandbox);
    vm.runInNewContext(readFileSync(resolve(root, 'static/js/ai_context_notice.js'), 'utf8'), sandbox);
    const md = window.AiContextNotice.withContextNotice('# 变更理解\n说明\n# 上线与回滚关注点\n人工判断', {
        coverage_notice: '**本次覆盖与缺口（平台补充）**\n\n- 本次覆盖：输入 98 → 取证 17\n- 没有取到证据：81 项',
        degradation: 'requests_exhausted', degradation_label: '上下文索取额度用尽',
        context: { budget_note: '窗口 999999 字符' }
    });
    const output = window.document.getElementById('report');
    output.innerHTML = window.AiReportMarkdown.renderReader(md);
    const notice = output.querySelector('.ai-reader-notice');
    assert.ok(notice, 'degradation needs its own visible section');
    assert.match(notice.textContent, /本次分析未完整跑完/);
    assert.equal(notice.closest('details'), null);
    assert.doesNotMatch(output.textContent, /999999/);
    const coverage = output.querySelector('.ai-reader-coverage');
    assert.match(coverage.textContent, /81 项/);
    assert.equal(coverage.tagName, 'DETAILS');
    assert.equal(coverage.hasAttribute('open'), false);
    assert.ok(md.indexOf('本次覆盖与缺口') > md.indexOf('上线与回滚关注点'));
    const sections = output.querySelectorAll('.ai-reader-section');
    assert.equal(sections[sections.length - 1].classList.contains('ai-reader-coverage'), true);
});

function functionSource(template, name) {
    const code = template.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');
    const start = code.indexOf('function ' + name + '(');
    assert.ok(start >= 0, name);
    let depth = 0;
    for (let i = code.indexOf('{', start); i < code.length; i++) {
        if (code[i] === '{') depth++;
        if (code[i] === '}' && --depth === 0) return code.slice(start, i + 1);
    }
    throw new Error('unclosed ' + name);
}

for (const file of ['weekly_version_diff.html', 'merged_project_view.html', 'commit_diff_new.html']) {
    test(file + ' cached report uses the reader and working navigation in a real DOM', () => {
        const isCommit = file === 'commit_diff_new.html';
        const outputId = isCommit ? 'aiAnalysisOutput' : 'weeklyAiOutput';
        const setter = isCommit ? 'setAiReport' : 'setWeeklyAiReport';
        const renderer = isCommit ? 'renderAi' : 'renderWeeklyAi';
        const { window } = parseHTML('<html><body><div class="is-empty is-busy" id="' + outputId + '"></div></body></html>');
        const sandbox = { window, document: window.document, aiBuffer: '', weeklyAiBuffer: '' };
        vm.createContext(sandbox);
        vm.runInContext(source, sandbox);
        sandbox.AiReportMarkdown = window.AiReportMarkdown;
        const template = readFileSync(resolve(root, 'templates', file), 'utf8');
        vm.runInContext(functionSource(template, setter) + '\n' + functionSource(template, renderer), sandbox);
        sandbox[setter](report);
        const output = window.document.getElementById(outputId);
        assert.equal(output.classList.contains('is-empty'), false);
        assert.equal(output.classList.contains('is-busy'), false);
        assert.ok(output.querySelector('.ai-reader-risk-card'));
        const nav = [...output.querySelectorAll('.ai-reader-nav a')].find(a => a.textContent === '变更理解');
        nav.dispatchEvent(new window.Event('click', { bubbles: true, cancelable: true }));
        assert.equal(output.querySelector(nav.getAttribute('href')).hasAttribute('open'), true);
    });
}

test('history preview loads the same reader with native chapter controls', async () => {
    const { window } = parseHTML('<html><body><div id="aiReportHistoryModal"></div><div id="aiReportHistoryBody"></div></body></html>');
    const sandbox = { window, console };
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox);
    vm.runInContext(readFileSync(resolve(root, 'static/js/ai_context_notice.js'), 'utf8'), sandbox);
    vm.runInContext(readFileSync(resolve(root, 'static/js/ai_report_history.js'), 'utf8'), sandbox);
    const row = { run_id: 42, status: 'succeeded', status_label: '已有结论', summary: '示例', exportable: true };
    const fetchImpl = async url => ({ ok: true, json: async () => url.endsWith('/history')
        ? { success: true, runs: [row], total: 1 }
        : { success: true, result: { status: 'succeeded', response_text: report, result: {} } } });
    window.AiReportHistory.track({ historyUrl: '/ai-analysis/commit/7/history' });
    await window.AiReportHistory.open(fetchImpl);
    const output = window.document.getElementById('aiHistoryReportBody');
    assert.ok(output, window.document.body.textContent);
    assert.ok(output.querySelector('.ai-reader-risk-card'), output.textContent);
    const nav = [...output.querySelectorAll('.ai-reader-nav a')].find(a => a.textContent === '变更理解');
    nav.dispatchEvent(new window.Event('click', { bubbles: true, cancelable: true }));
    assert.equal(output.querySelector(nav.getAttribute('href')).hasAttribute('open'), true);
});
