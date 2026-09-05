const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');
const script = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));

function declaration(name) {
  const start = script.indexOf(`function ${name}(`);
  assert.ok(start >= 0);
  const end = script.indexOf('\n}\n', start);
  assert.ok(end > start);
  return script.slice(start, end + 3);
}
function context(extra = {}) {
  const ctx = vm.createContext(extra);
  vm.runInContext(declaration('esc') + declaration('jsArg'), ctx);
  return ctx;
}
function decodeAttribute(text) {
  const entities = { '&quot;': '"', '&#39;': "'", '&lt;': '<', '&gt;': '>', '&amp;': '&' };
  return text.replace(/&quot;|&#39;|&lt;|&gt;|&amp;/g, e => entities[e]);
}

test('entire dashboard script parses', () => { new vm.Script(script); });

test('quotes and markup stay inside an HTML attribute', () => {
  const ctx = context();
  const attack = `" autofocus onfocus="alert(1)"><img src=x onerror=alert(1)> & ' text`;
  const escaped = ctx.esc(attack);
  assert.ok(!/[<>"']/.test(escaped));
  assert.equal(decodeAttribute(escaped), attack);
});

test('inline handler arguments survive HTML decoding without executing data', () => {
  const ctx = context({ calls: [], capture: value => ctx.calls.push(value) });
  const attack = `'); globalThis.compromised = true; // " & </script> \\`;
  const encoded = ctx.jsArg(attack);
  assert.ok(!/[<>"']/.test(encoded));
  vm.runInContext(`capture(${decodeAttribute(encoded)})`, ctx);
  assert.deepEqual(ctx.calls, [attack]);
  assert.equal(ctx.compromised, undefined);
});

test('routing editor and preview escape imported fields', () => {
  const elements = {
    'routing-rules-list': { innerHTML: '' },
    'rt-test-input': { value: 'test' },
    'rt-test-result': { innerHTML: '' },
  };
  const attack = '"><img src=x onerror=alert(1)>';
  const ctx = context({
    routingRules: [{ name: attack, route: 'workflow', model: attack, workflow: attack,
                     escalation: attack, match: { keywords_any: [] } }],
    document: { getElementById: id => elements[id] },
  });
  vm.runInContext(declaration('renderRoutingRules') + declaration('runRoutingTest'), ctx);
  ctx.renderRoutingRules();
  ctx.runRoutingTest();
  for (const id of ['routing-rules-list', 'rt-test-result']) {
    assert.ok(!elements[id].innerHTML.includes('<img'));
    assert.ok(elements[id].innerHTML.includes('&lt;img'));
  }
});

test('agent-card chat handler treats a malicious name as data', () => {
  const attack = `'); globalThis.compromised = true; // "`;
  const calls = [];
  const agent = { name: attack };
  const ctx = context({ AGENTS: [agent], getAgentTools: () => [],
    statusDotClass: () => '', statusBadgeClass: () => '',
    renderStatusControls: () => '', renderTags: () => '', renderSupervision: () => '',
    openChatPanel: value => calls.push(value),
  });
  vm.runInContext(declaration('renderAgentCard'), ctx);
  const card = ctx.renderAgentCard(agent, false);
  const handler = card.match(/onclick="([^"]*)"/)[1];
  vm.runInContext(decodeAttribute(handler), ctx);
  assert.deepEqual(calls, [attack]);
  assert.equal(ctx.compromised, undefined);
});
