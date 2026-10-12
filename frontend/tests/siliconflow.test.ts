import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {hunyuanModel, glmModel, playableConnection} from '../src/account.ts';
import type {SavedConnection} from '../src/account.ts';
import {plannerGuide} from '../src/onboarding.ts';
import {applyConnection, emptyPlan} from '../src/table-plan.ts';
import {qualificationError, thinkingNotice} from '../src/qualification.ts';

test('only the explicitly connected Hunyuan model is enabled, not all saved providers/models', () => {
  assert.equal(playableConnection({provider:'siliconflow',model:hunyuanModel}),true);
  assert.equal(playableConnection({provider:'siliconflow',model:glmModel}),true);
  assert.equal(playableConnection({provider:'siliconflow',model:'tencent/hy4-preview'}),false);
  assert.equal(playableConnection({provider:'siliconflow',model:'other'}),false);
  assert.equal(playableConnection({provider:'dashscope',model:hunyuanModel}),false);
  assert.equal(playableConnection({provider:'deepseek',model:'selected'}),true);
});
test('Hunyuan-only setup leads to assignment and read-only admission, never auto calls', () => {
  const c = {id:'sf',provider:'siliconflow',model:hunyuanModel} as SavedConnection;
  assert.equal(plannerGuide(emptyPlan(),[c],null,true,false,false).action,'connections');
  const plan = applyConnection(emptyPlan(),c.id);
  const guide = plannerGuide(plan,[c],null,true,false,false);
  assert.equal(guide.action,'preflight'); assert.match(guide.label,/不调用模型/);
  assert.equal(plannerGuide(plan,[{...c,model:'other'}],null,true,false,false).action,'settings');
});
test('thinking control is a request rather than an unverified claim of no reasoning cost', () => {
  assert.match(thinkingNotice('requested_disabled'),/待真实验证/);
  assert.match(thinkingNotice('requested_disabled'),/费用以账单/);
  assert.match(qualificationError('unsupported_model'),/tencent\/Hy4-preview/);
});
