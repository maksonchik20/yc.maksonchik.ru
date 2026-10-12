// Run with: node --test welcome/ghost_note/test_viewer_text_sender.cjs
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const { webcrypto } = require('node:crypto');
const vm = require('node:vm');

const templateDirectory = path.join(__dirname, 'templates', 'ghost_note');
const source = readFileSync(path.join(templateDirectory, 'viewer_text_sender.html'), 'utf8')
  .replace(/^<script>\s*/, '').replace(/\s*<\/script>\s*$/, '');

function response(statusCode = 200, result = { ok: true }, retryAfter = null) {
  return {
    status: statusCode,
    ok: statusCode >= 200 && statusCode < 300,
    headers: { get: () => retryAfter },
    json: async () => result
  };
}

function scenario(replies, overrides = {}) {
  const calls = [];
  const delays = [];
  const timeoutIdToCallback = new Map();
  let nextTimeoutId = 0;
  const context = vm.createContext({
    crypto: overrides.crypto || webcrypto,
    AbortController,
    Uint8Array,
    setTimeout(callback, delay) {
      const id = ++nextTimeoutId;
      timeoutIdToCallback.set(id, callback);
      delays.push(delay);
      if (delay !== 8000) queueMicrotask(() => {
        if (timeoutIdToCallback.delete(id)) callback();
      });
      return id;
    },
    clearTimeout(id) { timeoutIdToCallback.delete(id); },
    async fetch(url, options) {
      calls.push({ url, options });
      const reply = replies.shift();
      if (reply instanceof Error) throw reply;
      if (typeof reply === 'function') return reply(url, options);
      assert.ok(reply, 'Unexpected extra request');
      return reply;
    }
  });
  vm.runInContext(source, context);
  const text = { value: '[Текст] 😀 "quotes"\nline', readOnly: false, focus() { this.focused = true; } };
  const button = { textContent: 'Отправить', disabled: false };
  const status = { textContent: '', dataset: {} };
  const send = context.createGhostTextSender({
    text, button, status,
    url: '/ghost/v2/text/', sessionId: 'fixture-session',
    clearOnSuccess: overrides.clearOnSuccess !== false,
    headerToValue: { 'Content-Type': 'application/json', 'X-Ghost-Viewer': 'viewer-secret-fixture' }
  });
  return { send, calls, delays, text, button, status, timeoutIdToCallback };
}

test('lost response retries the same intent, preserving payload and viewer authorization', async () => {
  const keyToMessage = new Map();
  let attempts = 0;
  const server = async (_url, options) => {
    const payload = JSON.parse(options.body);
    keyToMessage.set(payload.idempotency_key, payload.text);
    if (++attempts === 1) throw new TypeError('Network disconnected after commit');
    return response();
  };
  const fixture = scenario([server, server]);
  const original = fixture.text.value;
  await fixture.send();
  assert.equal(fixture.calls.length, 2);
  assert.equal(keyToMessage.size, 1);
  assert.equal(fixture.calls[0].options.body, fixture.calls[1].options.body);
  assert.equal([...keyToMessage.values()][0], original);
  const options = fixture.calls[0].options;
  assert.equal(options.headers['X-Ghost-Viewer'], 'viewer-secret-fixture');
  assert.equal(options.credentials, 'omit');
  assert.equal(options.redirect, 'error');
  assert.equal(options.cache, 'no-store');
  assert.equal(fixture.text.value, '');
  assert.equal(fixture.text.readOnly, false);
  assert.equal(fixture.button.disabled, false);
  assert.match(fixture.status.textContent, /^Принято сервером/);
  assert.match(fixture.status.textContent, /ещё не подтверждение/);
  assert.equal(fixture.timeoutIdToCallback.size, 0);
});

test('exhausted retries retain draft and key for a safe manual retry', async () => {
  const fixture = scenario([new Error('offline'), new Error('offline'), new Error('offline'), response()]);
  const original = fixture.text.value;
  await fixture.send();
  assert.equal(fixture.calls.length, 3);
  assert.equal(fixture.text.value, original);
  assert.equal(fixture.text.readOnly, true);
  assert.equal(fixture.button.textContent, 'Повторить отправку');
  assert.equal(fixture.button.disabled, false);
  assert.equal(fixture.status.dataset.state, 'error');
  await fixture.send();
  assert.equal(fixture.calls.length, 4);
  assert.equal(new Set(fixture.calls.map(call => call.options.body)).size, 1);
  assert.equal(fixture.text.value, '');
  assert.equal(fixture.button.textContent, 'Отправить');
  assert.equal(fixture.text.readOnly, false);
});

test('double clicks and keyboard submits cannot race', async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const fixture = scenario([() => pending]);
  let prevented = 0;
  const first = fixture.send({ preventDefault() { prevented += 1; } });
  assert.equal(fixture.button.disabled, true);
  assert.equal(fixture.text.readOnly, true);
  await fixture.send({ preventDefault() { prevented += 1; } });
  assert.equal(fixture.calls.length, 1);
  assert.equal(prevented, 2);
  release(response());
  await first;
});

for (const statusCode of [400, 401, 403, 404, 409, 413]) {
  test('HTTP ' + statusCode + ' does not auto-retry or clear the draft', async () => {
    const fixture = scenario([response(statusCode), response(statusCode), response()]);
    const original = fixture.text.value;
    await fixture.send();
    assert.equal(fixture.calls.length, 1);
    assert.equal(fixture.text.value, original);
    assert.equal(fixture.text.readOnly, false);
    assert.equal(fixture.status.dataset.state, 'error');
    await fixture.send();
    assert.equal(fixture.calls.length, 2);
    assert.equal(fixture.calls[0].options.body, fixture.calls[1].options.body);
    fixture.text.value = 'Edited intent';
    await fixture.send();
    assert.notEqual(JSON.parse(fixture.calls[1].options.body).idempotency_key,
      JSON.parse(fixture.calls[2].options.body).idempotency_key);
  });
}

for (const statusCode of [400, 409, 413]) {
  test('definitive HTTP ' + statusCode + ' after an uncertain attempt unlocks the draft without losing its key', async () => {
    const fixture = scenario([new Error('response lost'), response(statusCode), response(statusCode), response()]);
    const original = fixture.text.value;
    await fixture.send();
    assert.equal(fixture.calls.length, 2);
    assert.equal(fixture.text.value, original);
    assert.equal(fixture.text.readOnly, false);
    assert.equal(fixture.button.disabled, false);
    assert.equal(fixture.button.textContent, 'Отправить');
    assert.match(fixture.status.textContent, /Предыдущая попытка могла быть принята/);
    await fixture.send();
    assert.equal(fixture.calls.length, 3);
    assert.equal(new Set(fixture.calls.map(call => call.options.body)).size, 1);
    fixture.text.value = 'Corrected text';
    await fixture.send();
    assert.notEqual(JSON.parse(fixture.calls[2].options.body).idempotency_key,
      JSON.parse(fixture.calls[3].options.body).idempotency_key);
  });
}

test('429 and server failures retry without changing the idempotency key', async () => {
  const fixture = scenario([response(429, {}, '2'), response(503), response()]);
  await fixture.send();
  assert.equal(fixture.calls.length, 3);
  assert.equal(new Set(fixture.calls.map(call => call.options.body)).size, 1);
  assert.ok(fixture.delays.includes(2000));
  assert.equal(fixture.status.dataset.state, 'sent');
});

test('long Retry-After stops bounded retrying without violating the server delay', async () => {
  const fixture = scenario([response(429, {}, '60')]);
  await fixture.send();
  assert.equal(fixture.calls.length, 1);
  assert.equal(fixture.text.readOnly, true);
  assert.equal(fixture.status.dataset.state, 'error');
  assert.equal(fixture.timeoutIdToCallback.size, 0);
});

test('timeout abort retries exactly the same body', async () => {
  let fixture;
  fixture = scenario([(_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('aborted')));
    queueMicrotask(() => [...fixture.timeoutIdToCallback.values()][0]());
  }), response()]);
  await fixture.send();
  assert.equal(fixture.calls.length, 2);
  assert.equal(fixture.calls[0].options.signal.aborted, true);
  assert.equal(fixture.calls[0].options.body, fixture.calls[1].options.body);
  assert.equal(fixture.status.dataset.state, 'sent');
});

test('malformed success response is uncertain, never cleared or assigned a new key', async () => {
  const fixture = scenario([response(200, {}), response(200, {}), response(200, {})]);
  const original = fixture.text.value;
  await fixture.send();
  assert.equal(fixture.calls.length, 3);
  assert.equal(fixture.text.value, original);
  assert.equal(fixture.text.readOnly, true);
  assert.equal(new Set(fixture.calls.map(call => call.options.body)).size, 1);
  assert.equal(fixture.status.dataset.state, 'error');
});

test('accepted repeated text is an explicit new intent with a fresh key', async () => {
  const fixture = scenario([response(), response()], { clearOnSuccess: false });
  const original = fixture.text.value;
  await fixture.send();
  assert.equal(fixture.text.value, original);
  await fixture.send();
  assert.notEqual(JSON.parse(fixture.calls[0].options.body).idempotency_key,
    JSON.parse(fixture.calls[1].options.body).idempotency_key);
});

test('empty drafts are not submitted', async () => {
  const fixture = scenario([]);
  fixture.text.value = ' \n ';
  await fixture.send();
  assert.equal(fixture.calls.length, 0);
  assert.equal(fixture.text.focused, true);
  assert.equal(fixture.status.dataset.state, 'error');
});

test('secure UUID fallback works in browsers without randomUUID', async () => {
  const fixture = scenario([response()], { crypto: { getRandomValues: value => webcrypto.getRandomValues(value) } });
  await fixture.send();
  assert.match(JSON.parse(fixture.calls[0].options.body).idempotency_key,
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
});

test('all hosted viewers include the shared sender and retain version-specific endpoints', () => {
  for (const name of ['viewer.html', 'viewer_v2.html', 'viewer_text_v2.html']) {
    const template = readFileSync(path.join(templateDirectory, name), 'utf8');
    assert.match(template, /include "ghost_note\/viewer_text_sender.html"/);
    assert.match(template, /createGhostTextSender\(/);
    assert.ok(template.includes(name === 'viewer.html' ? "url: '/ghost/text/'" : "url: '/ghost/v2/text/'"));
    assert.doesNotMatch(template, /Текст отправлен|Сообщение отправлено/);
    if (name !== 'viewer.html') assert.match(template, /'X-Ghost-Viewer': VIEWER_KEY/);
  }
  assert.doesNotMatch(source, /localStorage|sessionStorage/);
});
