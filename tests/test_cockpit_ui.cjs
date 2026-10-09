// Offline tests for the workbench's actual JavaScript, using an in-memory DOM.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../fenjue/cockpit.html'), 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];

function setup(fetch) {
  function node() {
    return { value: '', textContent: '', disabled: false, options: [], children: [],
      classList: { toggle() {} }, remove() {},
      append(...items) { this.children.push(...items); this.options.push(...items); if (!this.value && items[0]?.value) this.value = items[0].value; },
      replaceChildren() { this.children = []; this.options = []; this.value = ''; } };
  }
  const elements = new Map();
  const document = { getElementById(id) { if (!elements.has(id)) elements.set(id, node()); return elements.get(id); }, createElement: node };
  const context = vm.createContext({ document, fetch, crypto: require('node:crypto').webcrypto, performance, AbortController, TextDecoder });
  vm.runInContext(script, context);
  vm.runInContext("key='fixture';", context);
  const model = document.getElementById('model');
  model.append({ value: 'model-a' });
  return { context, element: document.getElementById.bind(document), run: code => vm.runInContext(code, context) };
}

test('model list failure allows refresh and recovery', async () => {
  let fail = true;
  const ui = setup(async () => fail ? Response.json({ error: { message: 'offline' } }, { status: 502 }) : Response.json({ data: [{ id: 'model-a' }], upstream: 'loopback' }));
  await ui.run('models()');
  assert.equal(ui.element('send').disabled, true);
  assert.equal(ui.element('refresh').disabled, false);
  fail = false;
  await ui.run('models()');
  assert.equal(ui.element('send').disabled, false);
  assert.equal(ui.element('model').value, 'model-a');
});

test('fragmented SSE, CRLF and UTF-8 preserve answer and full text history', async () => {
  const calls = [];
  const ui = setup(async (url, options) => {
    calls.push(JSON.parse(options.body));
    const events = 'data: ' + JSON.stringify({ type: 'response.output_text.delta', delta: '你好' }) + '\r\n\r\n' +
      'data: ' + JSON.stringify({ type: 'response.completed', response: { status: 'completed', output: [{ type: 'message', content: [{ type: 'output_text', text: '你好' }] }], usage: { output_tokens: 2 } } }) + '\r\n\r\n';
    const bytes = new TextEncoder().encode(events);
    return new Response(new ReadableStream({ start(controller) { for (const byte of bytes) controller.enqueue(Uint8Array.of(byte)); controller.close(); } }), { headers: { 'Content-Type': 'text/event-stream' } });
  });
  ui.element('prompt').value = 'first';
  await ui.run('send()');
  assert.equal(ui.run('history[1].content'), '你好');
  ui.element('prompt').value = 'second';
  await ui.run('send()');
  assert.equal(calls[1].input.length, 3);
  assert.equal(calls[1].input[1].content, '你好');
  assert.equal(calls[1].reasoning, undefined);
  assert.equal(ui.run('history.length'), 4);
});

test('incomplete response does not become conversation history', async () => {
  const ui = setup(async () => Response.json({ status: 'incomplete', output: [] }));
  ui.element('prompt').value = 'hello';
  await ui.run('send()');
  assert.equal(ui.run('history.length'), 0);
  assert.match(ui.element('status').textContent, /未完整结束/);
  assert.equal(ui.element('prompt').value, 'hello');
});

test('cancelled request is not retried or added to history', async () => {
  let calls = 0;
  const ui = setup(async () => { calls++; const error = new Error('cancelled'); error.name = 'AbortError'; throw error; });
  ui.element('prompt').value = 'hello';
  await ui.run('send()');
  assert.equal(calls, 1);
  assert.equal(ui.run('history.length'), 0);
  assert.match(ui.element('status').textContent, /已停止/);
  assert.equal(ui.element('cancel').disabled, true);
});
