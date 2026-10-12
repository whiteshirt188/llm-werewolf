import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { describeEvent, isPrivate, makeAction, rosterText } from '../src/game.ts';
import type { ActionSpec } from '../src/types.ts';

const spec: ActionSpec = {kind:'witch',potions:{skip:[],save:[6],poison:[1,2,3,4,6]}};
test('witch submits exactly one potion decision with relevant fields only', () => {
  assert.deepEqual(makeAction(spec,'6','','save'),{kind:'witch',potion:'save',target:6});
  assert.deepEqual(makeAction(spec,'1','','poison'),{kind:'witch',potion:'poison',target:1});
  assert.deepEqual(makeAction(spec,'6','','skip'),{kind:'witch',potion:'skip',target:null});
  assert.throws(() => makeAction(spec,'6',''));
  assert.throws(() => makeAction(spec,'1','','save'));
  assert.throws(() => makeAction(spec,'5','','poison'));
  assert.throws(() => makeAction({kind:'witch',potions:{skip:[],poison:[1]}},'6','','save'));
});
test('peaceful nights, private potion records and versioned rosters render accurately', () => {
  assert.equal(describeEvent({kind:'dawn',day:1,data:{dead:[]}}).text,'昨夜无人出局。');
  assert.equal(isPrivate({kind:'witch_action',day:1,data:{potion:'save',target:6}}),true);
  assert.match(describeEvent({kind:'witch_action',day:1,data:{potion:'poison',target:1}}).text,/毒药/);
  assert.match(describeEvent({kind:'game_started',day:1,data:{roster:{witch:1}}}).text,/女巫/);
  assert.doesNotMatch(describeEvent({kind:'game_started',day:1,data:{players:6}}).text,/女巫/);
  assert.match(rosterText(),/1 女巫.*2 村民/);
  assert.doesNotMatch(rosterText({version:'six-player-v2',wolves:2,seers:1,villagers:3}),/女巫/);
});
