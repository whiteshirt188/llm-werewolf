import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { cleanRoster, nineRoster, rosterError, sixRoster } from '../src/roster.ts';
import { applyConnection, changeHuman, changeRoster, emptyPlan, planBody, realStartBody } from '../src/table-plan.ts';
import type { Admission } from '../src/table-plan.ts';
import { describeEvent, rosterText } from '../src/game.ts';
import { plannerGuide } from '../src/onboarding.ts';

test('nine-player selection preserves old profiles and adds independent new API slots', () => {
  const six = applyConnection(emptyPlan(), 'shared');
  six.ai_profiles[0].nickname = '保留二号';
  const nine = changeRoster(six, nineRoster(), 'shared');
  assert.equal(nine.ai_profiles.length, 8);
  assert.equal(nine.ai_profiles[0].nickname, '保留二号');
  assert.equal(nine.ai_profiles[7].seat, 9);
  nine.ai_profiles[7].persona.description = '九号自己的个性';
  assert.notEqual(nine.ai_profiles[6].persona.description, nine.ai_profiles[7].persona.description);
  const human9 = changeHuman(nine, 9, 'shared');
  assert.deepEqual(human9.ai_profiles.map(p => p.seat), [1,2,3,4,5,6,7,8]);
  const shrink = changeRoster(human9, sixRoster(), 'shared');
  assert.equal(shrink.human_seat, 6);
  assert.equal(shrink.ai_profiles.length, 5);
  assert.equal(six.ai_profiles.length, 5);
  assert.deepEqual(planBody(nine, '').roster, nineRoster());
  assert.throws(() => planBody({...nine, human_seat:10}, ''));
});
test('custom roster constraints match backend and reject unknown fields on serialization', () => {
  for (const roster of [{...sixRoster(),wolves:3}, {...sixRoster(),wolves:0}, {...nineRoster(),players:10},
    {...nineRoster(),seers:2}, {...nineRoster(),witches:2}, {...nineRoster(),players:8.5}]) {
    assert.ok(rosterError(roster));
    assert.throws(() => cleanRoster(roster));
  }
  assert.ok(rosterError({players:9,wolves:4,seers:0,witches:0}));
  assert.equal(rosterError({players:9,wolves:4,seers:1,witches:0,guards:1,knights:1}), '');
  const untrusted = {...nineRoster(), api_key:'canary'};
  const clean = cleanRoster(untrusted);
  assert.deepEqual(clean, nineRoster());
});
test('nine-player admission requires eight seats and identical roster, never stale six-player result', () => {
  const nine = changeRoster(applyConnection(emptyPlan(), 'shared'), nineRoster(), 'shared');
  const admission = {roster:nineRoster(), all_qualified:true, game_start_enabled:true,
    seats:nine.ai_profiles.map(p => ({seat:p.seat,connection_id:'shared',qualified:true,configuration_version:'nine-v'}))} as Admission;
  assert.equal(Object.keys(realStartBody(nine, '', admission, 'op', true).expected_versions).length, 8);
  assert.throws(() => realStartBody(nine, '', {...admission,roster:sixRoster()}, 'op', true));
  assert.throws(() => realStartBody(nine, '', {...admission,seats:admission.seats.slice(0,5)}, 'op', true));
  assert.throws(() => realStartBody(nine, '', admission, 'op', false));
});
test('messages and setup guidance use actual rosters, including no witch', () => {
  const rules = {version:'custom-player-v1',players:9,wolves:2,seers:1,witches:0,villagers:6};
  assert.match(rosterText(rules), /2 狼人.*6 村民/);
  assert.doesNotMatch(rosterText(rules), /女巫/);
  const event = describeEvent({kind:'game_started',day:1,data:{players:9,roster:{wolf:3,seer:1,witch:1,villager:4}}});
  assert.equal(event.title, '9 席就位');
  assert.match(event.text, /3 名狼人.*4 名村民/);
  const plan = changeRoster(emptyPlan(), nineRoster());
  const guide = plannerGuide(plan, [{id:'a',provider:'deepseek'} as never], null, true, false, false);
  assert.match(guide.title, /8个 AI/);
});
