import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { defaultPlayStyles } from '../src/game.ts';
import { nineRoster } from '../src/roster.ts';
import { applyConnection, changeHuman, changeRoster, diversifyPlayStyles, diversifyStrategies, emptyPlan, planBody } from '../src/table-plan.ts';

test('new tables mix play styles by AI order with separate persona objects', () => {
  const six = emptyPlan();
  assert.deepEqual(six.ai_profiles.map(p => p.persona.play_style), defaultPlayStyles.slice(0,5));
  const nine = changeRoster(six, nineRoster());
  assert.deepEqual(nine.ai_profiles.map(p => p.persona.play_style), defaultPlayStyles);
  nine.ai_profiles[0].persona.play_style = 'playful';
  assert.equal(nine.ai_profiles[1].persona.play_style, 'intuitive');
  assert.equal(six.ai_profiles[0].persona.play_style, 'serious');
});

test('mixing play styles preserves all other settings and serializes exact choices', () => {
  const plan = applyConnection(emptyPlan(), 'my-api');
  plan.ai_profiles[0].nickname = '独立昵称';
  plan.ai_profiles[0].persona = {...plan.ai_profiles[0].persona, label:'自定义', description:'自己的语气', risk:'cautious', bluff:'low', strategy:'relationships', play_style:'follower'};
  const copy = structuredClone(plan);
  const mixed = diversifyPlayStyles(plan);
  assert.deepEqual(plan, copy);
  for (let i=0;i<plan.ai_profiles.length;i++) {
    const {play_style: _old, ...oldPersona} = plan.ai_profiles[i].persona;
    const {play_style: _new, ...newPersona} = mixed.ai_profiles[i].persona;
    assert.deepEqual(oldPersona, newPersona);
    assert.equal(mixed.ai_profiles[i].connection_id, plan.ai_profiles[i].connection_id);
    assert.equal(mixed.ai_profiles[i].nickname, plan.ai_profiles[i].nickname);
  }
  assert.deepEqual(planBody(mixed,'').ai_profiles.map(p=>p.persona.play_style), defaultPlayStyles.slice(0,5));
  assert.deepEqual(diversifyStrategies(plan).ai_profiles.map(p=>p.persona.play_style), plan.ai_profiles.map(p=>p.persona.play_style));
});

test('legacy saved personas keep their settings and use the neutral fallback', () => {
  const saved = applyConnection(emptyPlan(), 'saved-api');
  for (const p of saved.ai_profiles) delete p.persona.play_style;
  saved.ai_profiles[0].persona.description = '旧配置语气';
  const changed = changeHuman(saved, 6, 'new-api');
  assert.equal(changed.ai_profiles.find(p=>p.seat===2)?.persona.description, '旧配置语气');
  assert.equal(changed.ai_profiles.find(p=>p.seat===2)?.persona.play_style, undefined);
  const body = planBody(saved,'');
  assert.ok(body.ai_profiles.every(p=>p.persona.play_style==='serious'));
  assert.ok(saved.ai_profiles.every(p=>p.persona.play_style===undefined));
});
