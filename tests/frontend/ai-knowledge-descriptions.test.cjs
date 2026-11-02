const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { parseHTML } = require('linkedom');
const vm = require('node:vm');
const template = readFileSync(resolve(__dirname, '../../templates/merged_project_view.html'), 'utf8');

function extract(start, end) {
    return template.slice(template.indexOf(start), template.indexOf(end, template.indexOf(start)));
}

test('knowledge rows use distinct descriptions and treat markup as text', () => {
    const { document } = parseHTML('<html><body></body></html>').window;
    const ctx = { document, aiCanEdit: false, aiKnowFormatSize: () => '1KB', AI_KNOW_KINDS: { reference: { defaultUsage: 'generic' } } };
    vm.createContext(ctx);
    vm.runInContext(extract('function buildAiKnowledgeRow(', 'function renderAiKnowledge('), ctx);
    for (const description of ['解释回收物售价', '<img src=x onerror=bad()>']) {
        const row = ctx.buildAiKnowledgeRow({ kind: 'reference', name: 'pricing.md', description, usage: 'fixed boilerplate' });
        assert.equal(row.querySelector('.ai-know-row__usage').textContent, description);
        assert.equal(row.querySelector('img'), null);
    }
});

test('editing a reference restores the saved description alongside the full document', async () => {
    const nodes = { aiKnowledgeContentInput: { value: '' }, aiKnowledgeDescriptionInput: { value: '' } };
    nodes.aiKnowledgeContentInput.focus = () => {};
    const ctx = {
        aiEl: id => nodes[id], openAiKnowledgeEditor: () => {},
        readAiKnowledgeEntry: async () => ({ content: '---\ndescription: 价格规则\ngenerated_prefixes: Cfg\n---\n# 原文', description: '价格规则' }),
        refreshAiKnowledgeFrontmatter: () => {}, refreshAiKnowledgeCount: () => {},
    };
    vm.createContext(ctx);
    vm.runInContext(extract('async function startAiKnowledgeEdit(', 'async function saveAiKnowledgeEntry('), ctx);
    await ctx.startAiKnowledgeEdit('reference', 'pricing.md');
    assert.equal(nodes.aiKnowledgeDescriptionInput.value, '价格规则');
    assert.ok(nodes.aiKnowledgeContentInput.value.includes('generated_prefixes: Cfg'));
});
