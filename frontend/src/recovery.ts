import type {DecisionRecovery, RecoveryStrategy} from './types.ts';

export function recoveryExplanation(recovery:DecisionRecovery):string {
  if (recovery.thinking_mode === 'requested_low' && recovery.output_tokens !== null && recovery.output_tokens > 0
    && recovery.reasoning_tokens === recovery.output_tokens) {
    return `本次 ${recovery.output_tokens} token 全用于思考，没有生成最终行动。再次使用相同额度仍可能卡住。`;
  }
  return `上次请求的输出额度为 ${recovery.output_limit} token，最终行动未完整生成。请选择另一种请求方式。`;
}

export function recoveryConfirmation(recovery:DecisionRecovery, strategy:RecoveryStrategy):string {
  const change = strategy === 'more_tokens' ? `本次将思考与回答的合计额度提高到 ${recovery.increased_limit} token，可能更慢、费用更高。`
    : '本次关闭思考，使用原来的短回答额度。';
  return `${change}仅重做当前行动，不改变整局设置；后续行动仍按原配置。继续运行会调用供应商（可能包括下一步预取），可能产生费用。`;
}
