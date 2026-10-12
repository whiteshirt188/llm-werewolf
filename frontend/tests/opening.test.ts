import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { presentationPhase, waitingText } from '../src/chat.ts';
import { ceremonies, initialCeremonies, syncCeremonies } from '../src/ceremony.ts';
import type { GameView } from '../src/types.ts';

function preparing(): GameView {
  return { game_id: 'opening', mode: 'mock', status: 'running', human_seat: 1,
    pause_reason: null, turn_version: null, seat_profiles: [], playback_mode: 'manual',
    step_waiting: true, preparation: {active: true, completed: 2, total: 8},
    view: {seat: 1, role: 'seer', alive: true, day: 1, phase: 'night', players: [],
      legal_actions: [], checks: [], wolf_teammates: [], winner: null,
      events: [{id: 1, day: 1, kind: 'night_started', data: {}}]} };
}

test('opening progress overrides action/step notices and allows reading own role', () => {
  const game = preparing(), original = structuredClone(game);
  assert.equal(presentationPhase(game), 'preparation');
  assert.match(waitingText(game), /拿牌准备（2\/8）.*自己的身份/);
  assert.doesNotMatch(waitingText(game), /继续下一步|号行动/);
  assert.equal(game.view.role, 'seer');
  assert.deepEqual(game, original);
  game.status = 'paused';
  assert.match(waitingText(game), /完全暂停/);
  game.status = 'running'; game.preparation!.active = false;
  assert.equal(presentationPhase(game), 'night');
  assert.match(waitingText(game), /继续下一步/);
  game.step_waiting = false;
  assert.equal(waitingText(game), '夜间行动进行中，请等待。');
});

test('opening night ceremony is deferred until preparation completes, then runs once', () => {
  const game = preparing();
  let state = initialCeremonies(ceremonies(game));
  assert.deepEqual(ceremonies(game), []);
  game.preparation!.completed = 8; game.preparation!.active = false;
  const cues = ceremonies(game);
  state = syncCeremonies(state, cues, false);
  assert.deepEqual(state.queue.map(cue => cue.kind), ['night']);
  state = {...state, queue: []};
  assert.deepEqual(syncCeremonies(state, cues, false).queue, []);
  assert.deepEqual(initialCeremonies(cues).queue, []);
});

test('terminal messages win over unfinished preparation and historical views remain normal', () => {
  const game = preparing();
  game.status = 'abandoned';
  assert.notEqual(presentationPhase(game), 'preparation');
  assert.match(waitingText(game), /本局已结束/);
  game.status = 'finished'; game.view.winner = 'good'; game.view.phase = 'finished';
  assert.equal(presentationPhase(game), 'finished');
  assert.match(waitingText(game), /赛后复盘/);
  game.status = 'running'; game.view.winner = null; game.view.phase = 'night'; delete game.preparation;
  assert.equal(presentationPhase(game), 'night');
  assert.match(waitingText(game), /继续下一步/);
});
