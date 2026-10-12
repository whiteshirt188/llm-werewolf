import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { chatMessages, nearLatest, waitingText } from '../src/chat.ts';
import type { GameView } from '../src/types.ts';

test('sequential wolf replies render once, stay private and keep their seat order', () => {
  const state = game(); state.view.role = 'wolf'; state.human_seat = 3;
  state.view.events = [
    {day:2,kind:'wolf_message',data:{seat:3,text:'刀7，留6给我对跳'}},
    {day:2,kind:'wolf_message',data:{seat:8,text:'同意改刀7，留6'}},
    {day:2,kind:'wolf_message',data:{seat:9,text:'看到了8号改刀，配合留6'}},
  ];
  const lines = chatMessages(state);
  assert.deepEqual(lines.map(m => m.seat), [3,8,9]);
  assert.equal(new Set(lines.map(m => m.key)).size, 3);
  assert.ok(lines.every(m => m.scope === 'wolves'));
  assert.deepEqual(chatMessages(state), lines);
  state.view.role = 'villager';
  assert.equal(chatMessages(state).length, 0);
});

test('manual waiting is distinct from full pause and exposes no prefetch details', () => {
  const state = game(); state.playback_mode = 'manual'; state.step_waiting = true;
  assert.match(waitingText(state), /继续下一步/);
  assert.doesNotMatch(waitingText(state), /2 号|3 号|查验|刀人/);
  state.status = 'paused';
  assert.match(waitingText(state), /完全暂停/);
  state.status = 'running'; state.step_waiting = false; state.reading = true;
  assert.doesNotMatch(waitingText(state), /尚未开始决策/);
});

function game(): GameView {
  return { game_id: 'example', mode: 'mock', status: 'running', human_seat: 1,
    pause_reason: null, turn_version: null, seat_profiles: [],
    view: { seat: 1, role: 'wolf', alive: true, day: 1, phase: 'night', players: [],
      legal_actions: [], checks: [], wolf_teammates: [2], winner: null,
      events: [
        { id: 1, day: 1, kind: 'night_started', data: {} },
        { id: 2, day: 1, kind: 'wolf_proposals', data: { proposals: {
          '2': { target: 5, text: '先刀 5' }, '1': { target: 3, text: '我要刀 3' },
        } } },
        { id: 3, day: 1, kind: 'wolf_replies', data: { replies: { '1': '同意', '2': '再想想' } } },
        { id: 4, day: 1, kind: 'inspection', data: { target: 2, is_wolf: true } },
        { id: 5, day: 1, kind: 'speak', data: { seat: 3, text: '我是预言家' } },
        { id: 6, day: 1, kind: 'vote_result', data: { round: 1, votes: { '1': null } } },
      ] } };
}

test('wolf batches become per-seat bubbles without changing engine data or inventing timestamps', () => {
  const input = game(), original = structuredClone(input);
  const messages = chatMessages(input);
  assert.deepEqual(messages.filter(m => m.batch === '提案').map(m => m.seat), [1, 2]);
  assert.deepEqual(messages.filter(m => m.batch === '回应').map(m => m.text), ['同意', '再想想']);
  assert.match(messages.find(m => m.seat === 1)?.text ?? '', /刀 3 号/);
  assert.equal(messages.filter(m => m.scope === 'wolves').length, 4);
  assert.equal(new Set(messages.map(m => m.key)).size, messages.length);
  assert.equal(messages.some(m => m.title === '私密查验'), false);
  assert.deepEqual(input, original);
});

test('villagers, seers, dead spectators and hiding secrets produce no private placeholders', () => {
  for (const role of ['seer', 'villager'] as const) {
    const input = game(); input.view.role = role;
    assert.deepEqual(chatMessages(input).map(m => m.scope), ['public', 'public', 'public']);
  }
  const dead = game(); dead.view.alive = false;
  assert.deepEqual(chatMessages(dead).map(m => m.scope), ['public', 'public', 'public']);
  assert.deepEqual(chatMessages(game(), true), chatMessages(dead));
  dead.status = 'finished';
  assert.equal(chatMessages(dead).some(m => m.scope === 'wolves'), false);
});

test('only existing permitted events render; no optimistic wolf messages or unknown private event', () => {
  const input = game(); input.view.events = input.view.events.slice(0, 1);
  assert.equal(chatMessages(input).length, 1);
  input.view.events.push({day: 1, kind: 'future_secret', audience: [1], data: {text: 'secret'}});
  input.view.events.push({day: 1, kind: 'wolf_message', audience: [2], data: {seat: 2, text: 'secret'}});
  assert.equal(chatMessages(input).length, 1);
});

test('keys are stable across refresh, private filtering and public spectator ID reindexing', () => {
  const input = game(), first = chatMessages(input);
  assert.deepEqual(chatMessages(structuredClone(input)), first);
  const publicKeys = first.filter(m => m.scope === 'public').map(m => m.key);
  assert.deepEqual(chatMessages(input, true).map(m => m.key), publicKeys);
  input.view.alive = false;
  input.view.events = input.view.events.filter(e => ['night_started', 'speak', 'vote_result'].includes(e.kind))
    .map((e, i) => ({...e, id: i + 1}));
  assert.deepEqual(chatMessages(input).map(m => m.key), publicKeys);
});

test('player claims remain speech and ballots are expandable system messages', () => {
  const messages = chatMessages(game());
  const claim = messages.find(m => m.seat === 3)!;
  assert.equal(claim.text, '我是预言家');
  assert.equal(claim.scope, 'public');
  assert.equal(claim.title, '3 号发言');
  assert.equal(messages.at(-1)?.expandable, true);
  assert.equal(messages.at(-1)?.seat, undefined);
});

test('history reading threshold and safe wait text never expose night actors', () => {
  assert.equal(nearLatest(0, 100, 500), false);
  assert.equal(nearLatest(370, 100, 500), true);
  const input = game(); input.view.current_actor = 2;
  assert.equal(waitingText(input), '夜间行动进行中，请等待。');
  input.view.phase = 'day_speech';
  assert.match(waitingText(input), /2 号/);
  input.turn_version = 'current';
  assert.match(waitingText(input), /轮到你/);
  input.view.alive = false;
  assert.match(waitingText(input), /公开观战/);
  input.status = 'paused';
  assert.match(waitingText(input), /暂停/);
});
