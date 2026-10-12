import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { bubbleReady, initialTyping, syncTyping, tickTyping, displayedText, typingSpeeds } from '../src/typing.ts';
import type { ChatMessage } from '../src/chat.ts';
const speech = (key: string, seat = 2, text = '你好🐺'): ChatMessage => ({key, seat, text, day: 1, scope: 'public', title: '发言'});

test('initial history is instant, new other-player messages queue, own messages do not', () => {
  const old = speech('old');
  let state = initialTyping([old]);
  assert.equal(displayedText(state, old.key, old.text), old.text);
  const messages = [old, speech('mine', 1), speech('new')];
  state = syncTyping(state, messages, messages, 1, false);
  assert.deepEqual(state.queue.map(m => m.key), ['new']);
  assert.equal(displayedText(state, 'new', '你好🐺'), '');
  state = tickTyping(state); assert.equal(displayedText(state, 'new', ''), '你');
  state = tickTyping(state); assert.equal(displayedText(state, 'new', ''), '你好');
  state = tickTyping(state); assert.equal(state.queue.length, 0);
});
test('snapshot reconnect does not duplicate queued speech; batches advance in stable order', () => {
  const messages = [speech('one'), speech('two')];
  let state = syncTyping(initialTyping([]), messages, messages, 1, false);
  state = syncTyping(state, structuredClone(messages), messages, 1, false);
  assert.equal(state.queue.length, 2);
  assert.equal(bubbleReady(state, 'one'), true);
  assert.equal(bubbleReady(state, 'two'), false);
  assert.equal(bubbleReady(state, 'old'), true);
  state = tickTyping(state); state = tickTyping(state); state = tickTyping(state);
  assert.equal(state.queue[0].key, 'two');
  assert.equal(state.queue[0].count, 0);
  assert.equal(bubbleReady(state, 'two'), true);
});
test('hidden or removed secrets never continue typing or replay when shown again', () => {
  const secret = {...speech('secret'), scope: 'wolves' as const};
  let state = syncTyping(initialTyping([]), [secret], [secret], 1, false);
  state = syncTyping(state, [secret], [], 1, false);
  assert.equal(state.queue.length, 0);
  state = syncTyping(state, [secret], [secret], 1, false);
  assert.equal(state.queue.length, 0);
  assert.equal(syncTyping(initialTyping([]), [secret], [], 1, false).queue.length, 0);
});
test('reduced motion and final state show full text; speed stays bounded', () => {
  const messages = [speech('new')];
  assert.equal(syncTyping(initialTyping([]), messages, messages, 1, true).queue.length, 0);
  assert.ok(typingSpeeds.slow > typingSpeeds.normal && typingSpeeds.normal > typingSpeeds.fast);
});
