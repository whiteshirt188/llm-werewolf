import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { ApiError, request } from '../src/api.ts';

test('API uses same-origin cookie credentials and serializes exact operation IDs', async () => {
  const original = globalThis.fetch;
  const body = {operation_id: 'same-id', action: {kind: 'vote', target: 2}};
  globalThis.fetch = async (input, options) => {
    assert.equal(input, '/api/games/a/actions');
    assert.equal(options?.credentials, 'same-origin');
    assert.equal(options?.method, 'POST');
    assert.equal(options?.body, JSON.stringify(body));
    return new Response(JSON.stringify({status: 'running'}), {status: 200});
  };
  try { assert.deepEqual(await request('/api/games/a/actions', body), {status: 'running'}); }
  finally { globalThis.fetch = original; }
});
test('HTTP errors preserve actionable status but never render untrusted validation objects', async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response(JSON.stringify({detail: '旧回合'}), {status: 409});
    await assert.rejects(request('/api/games/a'), e => e instanceof ApiError && e.status === 409 && e.message === '旧回合');
    globalThis.fetch = async () => new Response(JSON.stringify({detail: [{input: 'secret'}]}), {status: 422});
    await assert.rejects(request('/api/games/a'), e => e instanceof ApiError && !e.message.includes('secret'));
  } finally { globalThis.fetch = original; }
});
test('network failure explicitly warns that action may already be stored', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => { throw new TypeError('private transport error'); };
  try { await assert.rejects(request('/api/games/a'), /结果可能已保存/); }
  finally { globalThis.fetch = original; }
});
