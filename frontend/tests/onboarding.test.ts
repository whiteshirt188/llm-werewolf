import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { plannerGuide } from '../src/onboarding.ts';
import { applyConnection, emptyPlan } from '../src/table-plan.ts';
import type { Admission } from '../src/table-plan.ts';
import type { SavedConnection } from '../src/account.ts';

const deepseek = {id:'saved', provider:'deepseek'} as SavedConnection;
const other = {id:'other', provider:'dashscope'} as SavedConnection;
const readyPlan = () => applyConnection(emptyPlan(), deepseek.id);
const admitted = () => ({all_qualified:true, game_start_enabled:true,
  seats:readyPlan().ai_profiles.map(p => ({seat:p.seat,qualified:true}))}) as Admission;

test('setup first distinguishes loading, no API and unsupported-only connections', () => {
  assert.equal(plannerGuide(emptyPlan(), [], null, false, false, false).action, null);
  for (const connections of [[], [other]]) {
    const guide = plannerGuide(emptyPlan(), connections, null, true, false, false);
    assert.equal(guide.step,1); assert.equal(guide.action,'settings');
    assert.match(guide.detail,/保存 API 本身不收费/);
  }
});
test('missing, deleted and unsupported seat references direct to connection assignment', () => {
  for (const plan of [emptyPlan(), applyConnection(emptyPlan(),'deleted'), applyConnection(emptyPlan(),'other')]) {
    const guide = plannerGuide(plan, [deepseek, other], null, true, false, false);
    assert.equal(guide.step,2); assert.equal(guide.action,'connections');
    assert.match(guide.detail,/2 号/); assert.match(guide.detail,/应用到五个 AI/);
  }
  const partial = readyPlan(); partial.ai_profiles[1].connection_id = '';
  const guide = plannerGuide(partial, [deepseek], null, true, false, false);
  assert.match(guide.detail,/3 号/); assert.doesNotMatch(guide.detail,/2 号/);
});
test('five saved connections lead to free preflight, never an automatic exam or game', () => {
  const guide = plannerGuide(readyPlan(), [deepseek], null, true, false, false);
  assert.equal(guide.step,3); assert.equal(guide.action,'preflight');
  assert.match(guide.detail,/资格测试已取消/); assert.match(guide.label,/不调用模型/);
});
test('failed or stale admission names blocked seats and directs to one-click preview without starting calls', () => {
  const admission = admitted(); admission.all_qualified = false; admission.seats[0].qualified = false;
  const guide = plannerGuide(readyPlan(), [deepseek], admission, true, false, false);
  assert.equal(guide.step,3); assert.equal(guide.action,'connections'); assert.match(guide.title,/2 号/);
  assert.match(guide.detail,/不需要考试/);
  assert.equal(plannerGuide(readyPlan(), [deepseek], {...admitted(), game_start_enabled:false}, true, false, false).action,'connections');
});
test('qualified seats still require separate consent and edits return to preflight', () => {
  const guide = plannerGuide(readyPlan(), [deepseek], admitted(), true, false, false);
  assert.equal(guide.step,4); assert.equal(guide.action,'consent');
  assert.match(guide.detail,/勾选费用确认/); assert.match(guide.detail,/预取一个行动/);
  assert.equal(plannerGuide(readyPlan(), [deepseek], null, true, false, false).step,3);
});
test('uncertain starts and existing games never direct a second new start', () => {
  const uncertain = plannerGuide(readyPlan(), [deepseek], admitted(), true, true, true);
  assert.equal(uncertain.action,'retry'); assert.match(uncertain.detail,/原请求/);
  const existing = plannerGuide(emptyPlan(), [], null, true, true, false);
  assert.equal(existing.action,'existing'); assert.match(existing.detail,/读取不调用模型/);
});
