import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { connectionBody, defaultGeneration } from '../src/account.ts';
import { request } from '../src/api.ts';

test('new connection requires a key; blank edit omits it instead of overwriting', () => {
  assert.throws(() => connectionBody('new', 'deepseek', 'model', defaultGeneration(), '', false));
  const edited = connectionBody(' new ', 'deepseek', ' model ', defaultGeneration(), '', true);
  assert.equal(edited.label, 'new'); assert.equal(edited.model, 'model');
  assert.equal('api_key' in edited, false);
  assert.equal(connectionBody('new', 'deepseek', 'model', defaultGeneration(), 'fake-key', false).api_key, 'fake-key');
});

test('invalid credentials and generation values are rejected, not silently trimmed', () => {
  assert.throws(() => connectionBody('new', 'deepseek', 'model', defaultGeneration(), ' key ', false));
  for (const generation of [{...defaultGeneration(), temperature: NaN}, {...defaultGeneration(), max_output_tokens: 1.5}, {...defaultGeneration(), reasoning_budget: 0}]) {
    assert.throws(() => connectionBody('new', 'deepseek', 'model', generation, 'fake-key', false));
  }
});

test('legacy connections keep thinking disabled and low mode preserves an explicit combined cap', () => {
  const legacy = {temperature: .6, max_output_tokens: 768, target_output_tokens: 256, reasoning_budget: null};
  const old = connectionBody('old', 'deepseek', 'deepseek-flash', legacy, '', true);
  assert.equal(old.generation.thinking_mode, 'disabled');
  assert.equal(old.generation.thinking_output_tokens, 4096);
  assert.equal('thinking_mode' in legacy, false);
  const low = connectionBody('low', 'deepseek', 'deepseek-flash', {...legacy, thinking_mode:'low', thinking_output_tokens:2048}, '', true);
  assert.equal(low.generation.thinking_mode, 'low');
  assert.equal(low.generation.thinking_output_tokens, 2048);
  assert.equal('api_key' in low, false);
});

test('thinking limits and provider mismatch cannot be submitted', () => {
  for (const cap of [256, 16385, NaN, 2048.5]) {
    assert.throws(() => connectionBody('low', 'deepseek', 'deepseek-flash', {...defaultGeneration(), thinking_mode:'low', thinking_output_tokens:cap}, '', true));
  }
  assert.throws(() => connectionBody('low', 'siliconflow', 'zai-org/GLM-5.3', {...defaultGeneration(), thinking_mode:'low'}, '', true));
});

test('account mutations include CSRF while cookie stays managed by browser', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async (input, options) => {
    assert.equal(input, '/api/account/logout');
    assert.deepEqual(options?.headers, {'Content-Type': 'application/json', 'X-CSRF-Token': 'csrf'});
    assert.equal(options?.credentials, 'same-origin');
    assert.equal(options?.body, '{}');
    return new Response(JSON.stringify({user: null}));
  };
  try { assert.deepEqual(await request('/api/account/logout', {}, {'X-CSRF-Token': 'csrf'}), {user: null}); }
  finally { globalThis.fetch = original; }
});
