import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {recoveryExplanation, recoveryConfirmation} from '../src/recovery.ts';
import {pauseReason} from '../src/game.ts';

const recovery = {output_limit:4096, output_tokens:4096, reasoning_tokens:4096, thinking_mode:'requested_low', increased_limit:8192};
test('explains reasoning exhaustion with the actual allowance, not repeated automatic retries',()=>{
  assert.match(recoveryExplanation(recovery), /4096 token 全用于思考/);
  assert.doesNotMatch(pauseReason('truncated_response'), /持续/);
  assert.match(recoveryExplanation({...recovery,reasoning_tokens:null}), /未完整生成/);
  assert.doesNotMatch(recoveryExplanation({...recovery,reasoning_tokens:null}), /全用于思考/);
});
test('explicit recovery describes a single changed request and its cost',()=>{
  assert.match(recoveryConfirmation(recovery,'more_tokens'), /8192 token/);
  assert.match(recoveryConfirmation(recovery,'more_tokens'), /费用更高/);
  assert.match(recoveryConfirmation(recovery,'without_thinking'), /本次关闭思考/);
  assert.match(recoveryConfirmation(recovery,'without_thinking'), /后续行动仍按原配置/);
});
