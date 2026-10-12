import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {readFileSync} from 'node:fs';
import {admissionReady, applyConnection, emptyPlan, realStartBody} from '../src/table-plan.ts';
import type {Admission} from '../src/table-plan.ts';
import {plannerGuide} from '../src/onboarding.ts';
import type {SavedConnection} from '../src/account.ts';
import {glmModel} from '../src/account.ts';

const plan = applyConnection(emptyPlan(), 'glm');
const connection = {id:'glm',provider:'siliconflow',model:glmModel} as SavedConnection;
const checked = (): Admission => ({plan_fingerprint:'version',unique_connections:1,qualification_configurations:0,
  qualification_required:false,all_ready:true,all_qualified:false,game_start_enabled:true,model_calls:0,
  seats:plan.ai_profiles.map(p => ({...p,ready:true,qualified:false,status:'ready',error:null,
    connection_label:'GLM',provider:'siliconflow',model:glmModel,report_id:null,configuration_version:'frozen'}))});

test('untested GLM seats can start only after free config check and separate explicit consent', () => {
  assert.equal(plannerGuide(plan,[connection],null,true,false,false).action,'preflight');
  const a = checked();
  assert.equal(admissionReady(a),true);
  assert.equal(plannerGuide(plan,[connection],a,true,false,false).action,'consent');
  assert.throws(() => realStartBody(plan,'',a,'start',false));
  const body = realStartBody(plan,'',a,'start',true);
  assert.equal(body.confirm_cost,true);
  assert.equal(body.expected_versions['2'],'frozen');
  assert.equal('qualified' in body,false);
});
test('historical exam pass cannot override a failed modern config check', () => {
  const a = checked(); a.all_qualified = true; a.seats[0].qualified = true; a.seats[0].ready = false;
  assert.equal(admissionReady(a),false);
  assert.throws(() => realStartBody(plan,'',a,'start',true));
  assert.equal(plannerGuide(plan,[connection],a,true,false,false).action,'connections');
});
test('normal account and table UI cannot load or launch qualification panels', () => {
  for (const name of ['AccountSettings.tsx','TablePlanner.tsx']) {
    const source = readFileSync(new URL('../src/' + name,import.meta.url),'utf8');
    assert.doesNotMatch(source,/QualificationPanel|QualificationBatch|\/qualifications|\/qualification-batches/);
  }
});
