import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {readFileSync} from 'node:fs';
import {connectionBody, defaultGeneration, playableConnection, qwenModel, qwenModels, supportsThinking} from '../src/account.ts';
import {plannerGuide} from '../src/onboarding.ts';
import {emptyPlan, applyConnection, thinkingLimitsText} from '../src/table-plan.ts';
import type {SavedConnection} from '../src/account.ts';

test('connected Qwen models reach assignment and keep the same combined budget settings', () => {
  for (const model of qwenModels) assert.equal(playableConnection({provider:'dashscope',model}),true);
  for (const model of ['unknown','qwen3-32b','qwen3.8-omni-flash']) {
    assert.equal(playableConnection({provider:'dashscope',model}),false);
  }
  assert.equal(supportsThinking('dashscope'),true);
  assert.equal(supportsThinking('siliconflow'),false);
  const connection={id:'qwen',provider:'dashscope',model:qwenModel} as SavedConnection;
  assert.equal(plannerGuide(applyConnection(emptyPlan(),connection.id),[connection],null,true,false,false).action,'preflight');
  const body=connectionBody('千问','dashscope',qwenModel,{...defaultGeneration(),thinking_mode:'low'},'fake-key',false);
  assert.equal(body.generation.thinking_output_tokens,4096);
  assert.equal(body.generation.thinking_mode,'low');
  const edited=connectionBody('千问','dashscope',qwenModel,{...defaultGeneration(),thinking_mode:'low'},'',true);
  assert.equal('api_key' in edited,false);
});

test('Qwen admission shows the existing round and role caps, without requiring a model call', () => {
  const label=thinkingLimitsText({thinking_output_tokens:4096,thinking_limits:{opening:4096,
    first_regular:4096,first_god:8192,later_regular:8192,later_god:16384}} as Parameters<typeof thinkingLimitsText>[0]);
  assert.match(label,/首轮普通席 4096 \/ 神职 8192/);
  assert.match(label,/第二晚起普通席 8192 \/ 神职 16384/);
  assert.match(label,/拿牌准备 4096/);
});

test('frontend model catalogue agrees with the bounded backend capability catalogue', () => {
  const source=readFileSync(new URL('../../backend/werewolf/qwen.py',import.meta.url),'utf8');
  const catalogue=source.split('DEFAULT_MODEL =')[1].split('def validate_connection')[0];
  const models=[...catalogue.matchAll(/"(qwen[\w.\-]+)"/g)].map(m=>m[1]);
  assert.deepEqual([...new Set(models)].sort(),[...qwenModels].sort());
});
