import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { ceremonies, ceremonyTiming, finishCeremony, initialCeremonies, speechIntroPending, syncCeremonies } from '../src/ceremony.ts';
import { chatMessages } from '../src/chat.ts';
import type { GameEvent, GameView } from '../src/types.ts';

function game(events: GameEvent[]): GameView {
  return { game_id: 'ceremonies', mode: 'mock', status: 'running', human_seat: 1, seat_profiles: [],
    pause_reason: null, turn_version: null, playback_mode: 'manual', step_waiting: false, speech_to_vote: false,
    view: { seat: 1, role: 'wolf', alive: true, day: 1, phase: 'day_speech', players: [], legal_actions: [], checks: [],
      wolf_teammates: [2], winner: null, events } };
}
const event = (kind: string, data: Record<string, unknown> = {}, day = 1): GameEvent => ({ kind, data, day });

test('public ceremonies cover each node without repeating each player speech', () => {
  const view = game([event('night_started'), event('dawn', { dead: [3] }), event('speak', { seat: 1 }),
    event('speak', { seat: 2 }), event('voting_started', { round: 1 }), event('vote_result', { round: 1, votes: {} }),
    event('runoff_started', { candidates: [2, 4] }), event('voting_started', { round: 2 }),
    event('no_elimination', { reason: 'tie' }), event('night_started', {}, 2), event('eliminated', { seat: 2 }, 2),
    event('game_over', { winner: 'good' }, 2)]);
  const cues = ceremonies(view);
  assert.deepEqual(cues.map(cue => cue.kind), ['night', 'dawn', 'speech', 'vote', 'result', 'tie', 'vote', 'result', 'night', 'result', 'victory']);
  assert.match(cues[1].detail, /3 号/);
  assert.equal(cues.at(-1)?.title, '好人阵营获胜');
});

test('private events never produce ceremonies or affect their keys', () => {
  const publicGame = game([event('night_started'), event('dawn', { dead: [] })]);
  const mixed = game([event('night_started'), event('wolf_proposals', { proposals: { 1: { target: 3, text: 'SECRET' } } }),
    { ...event('unknown_private', { text: 'SECRET' }), audience: [1] }, event('inspection'), event('dawn', { dead: [] })]);
  assert.deepEqual(ceremonies(mixed), ceremonies(publicGame));
  assert.doesNotMatch(JSON.stringify(ceremonies(mixed)), /SECRET/);
});

test('voting ceremony waits for final typing and the manual release, including runoff', () => {
  for (const round of [1, 2]) {
    const view = game([event('speak', { seat: 6, text: '还在说' }), event('voting_started', { round })]);
    view.view.phase = round === 1 ? 'vote' : 'runoff_vote';
    view.step_waiting = true; view.speech_to_vote = true;
    assert.equal(ceremonies(view).some(cue => cue.kind === 'vote'), false);
    view.step_waiting = false; view.speech_to_vote = false;
    const key = chatMessages(view)[0].key;
    assert.equal(ceremonies(view, [key]).some(cue => cue.kind === 'vote'), false);
    assert.equal(ceremonies(view).some(cue => cue.kind === 'vote'), true);
  }
});

test('history and polling do not replay; same-batch results retain order', () => {
  const view = game([event('night_started')]);
  let state = initialCeremonies(ceremonies(view));
  assert.equal(state.queue.length, 0);
  view.view.events.push(event('vote_result', { round: 1, votes: {} }), event('eliminated', { seat: 2 }), event('night_started', {}, 2));
  const cues = ceremonies(view);
  state = syncCeremonies(state, cues, false);
  assert.deepEqual(state.queue.map(cue => cue.kind), ['result', 'result', 'night']);
  assert.deepEqual(syncCeremonies(state, cues, false).queue, state.queue);
  state = { ...state, queue: [] };
  assert.equal(syncCeremonies(state, cues, false).queue.length, 0);
  assert.equal(initialCeremonies(cues).queue.length, 0);
});

test('pause/reduced motion cancel without replay; newer visible steps supersede stale batches', () => {
  const view = game([]);
  let state = initialCeremonies([]);
  view.view.events.push(event('dawn', { dead: [] }));
  state = syncCeremonies(state, ceremonies(view), false);
  assert.equal(state.queue[0].kind, 'dawn');
  view.view.events.push(event('speak', { seat: 1 }));
  state = syncCeremonies(state, ceremonies(view), false);
  assert.deepEqual(state.queue.map(cue => cue.kind), ['dawn', 'speech']);
  view.view.events.push(event('voting_started', {round:1}));
  state = syncCeremonies(state, ceremonies(view), false);
  assert.deepEqual(state.queue.map(cue => cue.kind), ['vote']);
  state = syncCeremonies(state, ceremonies(view), true);
  assert.equal(state.queue.length, 0);
  assert.equal(syncCeremonies(state, ceremonies(view), false).queue.length, 0);
});

test('float timeline holds ordinary text for two seconds and victory for longer', () => {
  const cues = ceremonies(game([event('night_started'), event('game_over', { winner: 'wolves' })]));
  assert.deepEqual(ceremonyTiming(cues[0]), { enterMs: 650, holdMs: 2000, exitMs: 850, exitDelayMs: 2650, totalMs: 3500 });
  assert.deepEqual(ceremonyTiming(cues[1]), { enterMs: 650, holdMs: 2800, exitMs: 1050, exitDelayMs: 3450, totalMs: 4500 });
  assert.equal(cues[1].winner, 'wolves');
});

test('skip/timeout completes only the active cue, never its successor or a replacement', () => {
  const cues = ceremonies(game([event('dawn', { dead: [] }), event('night_started', {}, 2)]));
  const state = { seen: new Set(cues.map(cue => cue.key)), queue: cues };
  const skipped = finishCeremony(state, cues[0].key);
  assert.deepEqual(skipped.queue, cues.slice(1));
  assert.equal(finishCeremony(skipped, cues[0].key), skipped);
  assert.equal(finishCeremony(state, 'expired-key'), state);
  assert.deepEqual(finishCeremony(skipped, cues[1].key).queue, cues.slice(2));
});

test('human first speaker gets the speech cue before submitting and submission cannot replay it', () => {
  const view = game([event('night_started')]);
  let state = initialCeremonies(ceremonies(view));
  view.turn_version = 'human-first'; view.view.current_actor = 1;
  view.view.legal_actions = [{kind:'speak',max_length:300}];
  view.view.events.push(event('dawn',{dead:[4]}));
  state = syncCeremonies(state, ceremonies(view), false);
  assert.deepEqual(state.queue.map(cue => cue.kind), ['dawn','speech']);
  assert.equal(speechIntroPending(state, view), true);
  const keys = state.queue.map(cue => cue.key);
  state = finishCeremony(state, state.queue[0].key);
  assert.equal(speechIntroPending(state, view), true);
  state = finishCeremony(state, state.queue[0].key);
  assert.equal(speechIntroPending(state, view), false);
  state = {...state,queue:[]};
  view.view.events.push(event('speak',{seat:1,text:'我的第一条发言'}));
  const after = ceremonies(view);
  assert.deepEqual(after.filter(cue => ['dawn','speech'].includes(cue.kind)).map(cue => cue.key),keys);
  assert.equal(syncCeremonies(state,after,false).queue.length,0);
  assert.equal(initialCeremonies(after).queue.length,0);
});

test('speech intro gate releases on pause/reduced motion, reload and later phases', () => {
  const view = game([event('dawn', {dead:[]})]);
  const cues = ceremonies(view);
  const state = syncCeremonies(initialCeremonies([]), cues, false);
  assert.equal(speechIntroPending(state, view), true);
  assert.equal(speechIntroPending(initialCeremonies(cues), view), false);
  assert.equal(speechIntroPending(syncCeremonies(state, cues, true), view), false);
  view.status = 'paused';
  assert.equal(speechIntroPending(state, view), false);
  view.status = 'running'; view.view.phase = 'vote';
  assert.equal(speechIntroPending(state, view), false);
});

test('a game that ends at dawn never announces a nonexistent speech phase', () => {
  const view = game([event('dawn',{dead:[4]}),
    {...event('inspection',{text:'PRIVATE'}),audience:[1]},event('game_over',{winner:'wolves'})]);
  assert.deepEqual(ceremonies(view).map(cue => cue.kind),['dawn','victory']);
});
