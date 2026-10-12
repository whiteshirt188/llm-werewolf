import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { countChars, describeEvent, makeAction, phases, visibleEvents, wolfVictoryText, wolfObjective } from '../src/game.ts';
import type { GameEvent } from '../src/types.ts';

test('new side-elimination guidance and historical parity guidance use the saved rules', () => {
  const old = {version:'nine-player-v2-gods', wolves:3, seers:1, witches:1, hunters:1, villagers:3};
  const current = {...old, win_condition:'eliminate_side' as const};
  assert.match(wolfVictoryText(current), /村民全部出局或神职全部出局/);
  assert.match(wolfObjective(current), /淘汰全部村民或全部神职/);
  assert.match(wolfVictoryText(old), /旧局人数规则/);
  assert.match(wolfObjective(old), /狼人数不少于好人数/);
  assert.match(wolfVictoryText(), /屠边/);
});

test('Unicode code points match the Python length limit, not UTF-16 units', () => {
  assert.equal(countChars('你好🐺'), 3);
  const spec = {kind: 'wolf_message' as const, max_length: 150};
  assert.equal(makeAction(spec, '', '🐺'.repeat(150)).text, '🐺'.repeat(150));
  assert.throws(() => makeAction(spec, '', '🐺'.repeat(151)), /150/);
});
test('actions include only relevant fields and enforce legal targets', () => {
  assert.deepEqual(makeAction({kind: 'inspect', targets: [2, 3]}, '2', 'ignored'), {kind: 'inspect', target: 2});
  assert.throws(() => makeAction({kind: 'inspect', targets: [2]}, '', ''), /选择/);
  assert.throws(() => makeAction({kind: 'inspect', targets: [2]}, '3', ''), /选择/);
  assert.throws(() => makeAction({kind: 'kill', targets: [2]}, 'abstain', ''), /选择/);
  assert.deepEqual(makeAction({kind: 'vote', targets: [2], allow_abstain: true}, 'abstain', ''), {kind: 'vote', target: null});
  assert.deepEqual(makeAction({kind: 'speak', max_length: 300}, '', '我是村民。'), {kind: 'speak', text: '我是村民。'});
});
test('empty and overlong speech are rejected without truncation', () => {
  assert.throws(() => makeAction({kind: 'speak', max_length: 300}, '', '  '), /填写/);
  assert.throws(() => makeAction({kind: 'speak', max_length: 300}, '', '中'.repeat(301)), /300/);
  assert.deepEqual(makeAction({kind: 'propose_kill', targets: [3], max_length: 150}, '3', '可疑'), {kind: 'propose_kill', target: 3, text: '可疑'});
});
test('public logs never include private events, including audience-marked future events', () => {
  const events: GameEvent[] = ['speak', 'inspection', 'wolf_proposals', 'wolf_replies', 'wolf_decision', 'dawn']
    .map(kind => ({day: 1, kind, data: {}}));
  events.push({day: 1, kind: 'future_secret', audience: [1], data: {}});
  assert.deepEqual(visibleEvents(events).map(e => e.kind), ['speak', 'dawn']);
  assert.equal(visibleEvents(events, true).length, 5);
});
test('inspection rendering does not turn player claims into facts', () => {
  assert.equal(describeEvent({day: 1, kind: 'speak', data: {seat: 2, text: '我是预言家'}}).text, '我是预言家');
  assert.equal(describeEvent({day: 1, kind: 'inspection', data: {target: 2, is_wolf: false}}).text, '2 号是好人。');
  assert.equal(phases.vote, '放逐投票');
});
test('wolf proposal, reply, consensus and public ballots are readable', () => {
  assert.match(describeEvent({day: 1, kind: 'wolf_proposals', data: {proposals: {'1': {target: 3, text: '可疑'}}}}).text, /1 号提议刀 3 号：可疑/);
  assert.match(describeEvent({day: 1, kind: 'wolf_replies', data: {replies: {'1': '同意'}}}).text, /同意/);
  assert.match(describeEvent({day: 1, kind: 'wolf_decision', data: {target: 3, method: 'unanimous_proposals'}}).text, /一致/);
  assert.match(describeEvent({day: 1, kind: 'vote_result', data: {round: 1, votes: {'1': null, '2': 3}}}).text, /1 号 → 弃票；2 号 → 3 号/);
});
