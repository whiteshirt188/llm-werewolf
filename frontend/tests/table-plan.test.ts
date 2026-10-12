import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { admissionStatus, applyConnection, changeHuman, emptyPlan, planBody, realStartBody, thinkingLimitsText } from '../src/table-plan.ts';
import type { Admission, AdmissionSeat } from '../src/table-plan.ts';

test('thinking allowance preview explains opening, round and god budgets and supports older reports', () => {
  const seat = {thinking_output_tokens:4096} as AdmissionSeat;
  assert.equal(thinkingLimitsText(seat), '思考与回答合计最多 4096 tokens');
  const text = thinkingLimitsText({...seat, thinking_limits:{opening:4096,first_regular:4096,first_god:8192,later_regular:8192,later_god:16384}});
  assert.match(text, /首轮普通席 4096 \/ 神职 8192/);
  assert.match(text, /第二晚起普通席 8192 \/ 神职 16384/);
  assert.match(text, /拿牌准备 4096/);
});

test('one connection fills five independent persona objects and only references enter the body', () => {
  const original = emptyPlan(), plan = applyConnection(original, 'connection-a');
  assert.deepEqual(plan.ai_profiles.map(p => p.connection_id), Array(5).fill('connection-a'));
  plan.ai_profiles[0].persona.description = '独立个性';
  assert.notEqual(plan.ai_profiles[0].persona.description, plan.ai_profiles[1].persona.description);
  assert.equal(original.ai_profiles[0].connection_id, '');
  const input = {...plan, api_key:'canary', qualified:true};
  assert.equal(JSON.stringify(planBody(input, '38')).includes('canary'), false);
  assert.deepEqual(Object.keys(planBody(input, '38')).sort(), ['ai_profiles','human_seat','playback_mode','seed']);
  assert.equal(planBody(plan, '38').seed, 38);
});
test('changing human seat preserves four profiles and creates a fresh fifth without inheriting secrets', () => {
  const original = applyConnection(emptyPlan(), 'a');
  original.ai_profiles[0].nickname = '独立二号';
  const changed = changeHuman(original, 6, 'b');
  assert.deepEqual(changed.ai_profiles.map(p => p.seat), [1,2,3,4,5]);
  assert.equal(changed.ai_profiles[0].connection_id, 'b');
  assert.equal(changed.ai_profiles[1].nickname, '独立二号');
  assert.equal(original.human_seat, 1);
  assert.equal(changeHuman(original, 1), original);
  assert.throws(() => changeHuman(original, 0));
});
test('seed precision, missing connections and duplicate AI seats fail before submission', () => {
  assert.throws(() => planBody(emptyPlan(), ''));
  const plan = applyConnection(emptyPlan(), 'a');
  for (const seed of ['1.1', 'NaN', '9007199254740992', ' 2', '1e2']) assert.throws(() => planBody(plan, seed));
  assert.equal(planBody(plan, '-9007199254740991').seed, -9007199254740991);
  assert.equal(planBody(plan, '').seed, null);
  assert.throws(() => planBody({...plan, ai_profiles:plan.ai_profiles.slice(1)}, ''));
  assert.throws(() => planBody({...plan, ai_profiles:plan.ai_profiles.map(p => ({...p,seat:2}))}, ''));
});
test('only effective qualification is green; simulated, missing, stale and foreign remain invalid', () => {
  assert.equal(admissionStatus(), '待预检');
  assert.equal(admissionStatus({status:'qualified'} as AdmissionSeat), '历史资格');
  for (const status of ['simulated', 'not_tested', 'not_qualified', 'unavailable', 'failed', 'unknown']) {
    assert.notEqual(admissionStatus({status} as AdmissionSeat), '资格有效');
  }
});

test('real start requires explicit cost confirmation, five matching admitted seats, and exact server versions', () => {
  const plan = applyConnection(emptyPlan(), 'a');
  const admission = {all_qualified:true, game_start_enabled:true, seats:plan.ai_profiles.map(p => ({seat:p.seat,connection_id:'a',qualified:true,configuration_version:'v1'}))} as Admission;
  assert.throws(() => realStartBody(plan, '', admission, 'op', false));
  assert.throws(() => realStartBody(plan, '', {...admission,all_qualified:false}, 'op', true));
  assert.throws(() => realStartBody(plan, '', {...admission,seats:admission.seats.slice(1)}, 'op', true));
  assert.throws(() => realStartBody(changeHuman(plan,6,'a'), '', admission, 'op', true));
  const body = realStartBody(plan, '38', admission, 'same-op', true);
  assert.equal(body.operation_id, 'same-op');
  assert.equal(body.confirm_cost, true);
  assert.deepEqual(body.expected_versions, {'2':'v1','3':'v1','4':'v1','5':'v1','6':'v1'});
  assert.equal(JSON.stringify(body).includes('api_key'),false);
});
