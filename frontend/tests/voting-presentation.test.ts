import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { chatMessages, presentationPhase, votingNoticeReady, waitingText } from '../src/chat.ts';
import type { GameView } from '../src/types.ts';

function voting(): GameView {
  return {game_id:'vote-example',mode:'mock',status:'running',human_seat:1,seat_profiles:[],pause_reason:null,
    turn_version:'turn',playback_mode:'manual',step_waiting:true,step_id:'gate',
    view:{seat:1,role:'villager',alive:true,day:1,phase:'vote',players:[],legal_actions:[],checks:[],wolf_teammates:[],winner:null,
      events:[{day:1,kind:'speak',data:{seat:6,text:'最后一位还在发言'}},
        {day:1,kind:'voting_started',data:{round:1}}]}};
}

test('manual last-speech gate keeps vote title and notice hidden until continue', () => {
  const game = voting(), messages = chatMessages(game);
  assert.equal(presentationPhase(game), 'day_speech');
  assert.equal(votingNoticeReady(game, messages, 1, []), false);
  assert.equal(votingNoticeReady(game, messages, 0, []), true);
  game.step_waiting = false;
  assert.equal(presentationPhase(game, true), 'day_speech');
  assert.equal(votingNoticeReady(game, messages, 1, [messages[0].key]), false);
  assert.equal(presentationPhase(game), 'vote');
  assert.equal(votingNoticeReady(game, messages, 1, []), true);
});
test('auto gate and local typing lag both prevent an early voting prompt', () => {
  const game = voting(); game.playback_mode = 'auto'; game.step_waiting = false; game.reading = true;
  assert.equal(presentationPhase(game), 'day_speech');
  game.reading = false;
  assert.match(waitingText(game, true), /最后的发言/);
  assert.match(waitingText(game), /轮到你/);
  game.view.phase = 'runoff_vote';
  assert.equal(presentationPhase(game, true), 'runoff_speech');
  assert.equal(presentationPhase(game), 'runoff_vote');
});
test('old voting messages remain visible during later days and rounds; engine view is untouched', () => {
  const game = voting(), original = structuredClone(game), messages = chatMessages(game);
  presentationPhase(game, true); votingNoticeReady(game, messages, 1, [messages[0].key]);
  assert.deepEqual(game, original);
  game.view.day = 2;
  assert.equal(votingNoticeReady(game, messages, 1, [messages[0].key]), true);
  game.view.day = 1;
  game.view.events.push({day:1,kind:'voting_started',data:{round:2}});
  const rounds = chatMessages(game);
  assert.equal(votingNoticeReady(game, rounds, 1, []), true);
  assert.equal(votingNoticeReady(game, rounds, 2, []), false);
});

test('changing playback mode during an already-started vote does not rewind the phase', () => {
  const game = voting(); game.speech_to_vote = false;
  assert.equal(presentationPhase(game), 'vote');
  assert.equal(votingNoticeReady(game, chatMessages(game), 1, []), true);
});
