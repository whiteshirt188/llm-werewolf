import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { qualificationError, qualificationStartUncertain, qualificationStatus } from '../src/qualification.ts';
import type { QualificationReport } from '../src/qualification.ts';

test('simulated and stale passes never display current real qualification', () => {
  const baseline = {status:'passed', simulated:false, qualified:true} as QualificationReport;
  assert.equal(qualificationStatus(baseline), '当前配置资格通过');
  assert.match(qualificationStatus({...baseline, simulated:true, qualified:false}), /不授予真实资格/);
  assert.match(qualificationStatus({...baseline, qualified:false}), /当前资格无效/);
  assert.equal(qualificationStatus({...baseline, status:'running', qualified:false}), '正在测试');
});

test('error labels explain unknown use and do not echo arbitrary provider messages', () => {
  assert.match(qualificationError('timeout'), /未知/);
  assert.match(qualificationError('authentication'), /未自动重试/);
  assert.match(qualificationError('unsupported_reasoning_budget'), /清空/);
  assert.equal(qualificationError('secret-key-canary'), '本次测试未完成，请查看调用记录。');
  assert.equal(qualificationError(null), '');
});

test('uncertain starts retain the original operation ID for network, timeout and server failures', () => {
  for (const status of [undefined, 408, 500, 502, 503]) assert.equal(qualificationStartUncertain(status), true);
  for (const status of [401, 403, 409, 422]) assert.equal(qualificationStartUncertain(status), false);
});
