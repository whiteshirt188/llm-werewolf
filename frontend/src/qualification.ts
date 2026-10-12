export type QualificationAttempt = {id: string; question_id: string; retry: number; provider: string; model: string;
  status: string; error: string | null; latency_ms: number | null; simulated: boolean;
  usage: {input_tokens: number | null; output_tokens: number | null; cached_input_tokens: number | null; reasoning_tokens: number | null}};
export type QualificationReport = {id: string; connection_id: string; status: 'running' | 'passed' | 'failed' | 'cancelled' | 'incomplete';
  model: string; qualified: boolean; simulated: boolean; error: string | null; rule_passed: number; logic_passed: number;
  rule_total?: number; logic_total?: number; question_total?: number;
  results: {id: string; title: string; category: 'rule' | 'logic'; passed: boolean; error: string | null}[];
  attempts: QualificationAttempt[];
  usage: {calls: number; known_input_tokens: number; known_output_tokens: number; unknown_usage_calls: number;
    known_cached_input_tokens: number; known_reasoning_tokens: number; unknown_cached_usage_calls: number;
    unknown_reasoning_usage_calls: number; estimated_cost: null}};
export type QualificationPreview = {fingerprint: string; configuration_version: string; unsupported: string | null;
  cached: boolean; latest_report: QualificationReport | null; normal_calls: number; maximum_calls: number;
  simulated_transport: boolean; thinking_mode: 'disabled' | 'requested_disabled'};
export const thinkingNotice = (mode: QualificationPreview['thinking_mode']) => mode === 'requested_disabled'
  ? '本批向硅基流动明确请求关闭思考；混元参数生效情况待真实验证，供应商若返回推理用量仍记录，费用以账单为准。'
  : '本批固定关闭思考模式。';
// A server error may arrive after the start receipt was committed. Keep its operation ID.
export const qualificationStartUncertain = (status?: number) => status === undefined || status === 408 || status >= 500;
export const qualificationError = (code: string | null) => {
  const messages: Record<string, string> = {
    authentication: '供应商鉴权失败，请检查 API Key；未自动重试。', balance: '供应商报告余额不足。',
    rate_limit: '供应商限流，已达到本次重试上限。', timeout: '请求超时，未报告的用量保持未知。',
    server_error: '供应商暂时不可用，已达到重试上限。', transport: '网络连接失败，未报告的用量保持未知。',
    request_parameters: '供应商拒绝参数或模型 ID，请检查连接配置。', invalid_action: '输出不符合行动协议，本题不通过。',
    truncated_response: '输出被截断，未取得完整行动。', invalid_content: '供应商未返回有效行动文本。',
    invalid_envelope: '供应商响应格式异常。', configuration_changed: '配置或规则在考试中变化，已停止后续调用。',
    connection_unavailable: '连接已删除或无法读取。', unsupported_connection: '当前支持 DeepSeek、通义千问官方连接与硅基流动混元 / GLM-5.3。',
    unsupported_model: '硅基流动支持 tencent/Hy4-preview 和 zai-org/GLM-5.3，请检查完整模型 ID。',
    unsupported_reasoning_budget: '当前适配器不支持单独推理预算；请清空该项后重新检查配置。DeepSeek 低档思考使用合计额度。',
    unsupported_thinking_model: '低档思考支持 deepseek-flash / deepseek-v4-pro，请检查准确模型 ID。',
    unsupported_qwen_model: '千问支持 Qwen3.5/3.6/3.7 Plus、Flash 和 Qwen3.7/3.8 Max、Qwen3.8 Flash 的已接入版本；建议填 qwen3.6-plus。',
    unsupported_qwen_thinking_model: '千问合计思考额度需要 Qwen3.5 及之后的已接入商业模型，建议填 qwen3.6-plus；qwen-plus / qwen-flash 请关闭思考。',
    unsupported_thinking_provider: '这个思考开关用于 DeepSeek 和通义千问，请检查供应商设置。',
    invalid_output_limit: '输出上限超出当前适配器范围，请检查生成参数。', server_restart: '服务重启中断考试，不会自动续测。',
    server_shutdown: '服务停止中断考试，不会自动续测。', cancelled: '测试已取消；已发出的调用可能已产生费用。',
    internal_error: '测试遇到内部错误，已停止。', wrong_answer: '答案不符合该题的唯一要求。',
    invalid_credential_format: 'API Key 格式不支持，请检查复制内容。',
  };
  return code ? messages[code] ?? '本次测试未完成，请查看调用记录。' : '';
};
export function qualificationStatus(report: QualificationReport) {
  if (report.status === 'running') return '正在测试';
  if (report.simulated && report.status === 'passed') return '模拟题通过 · 不授予真实资格';
  if (report.qualified) return '当前配置资格通过';
  if (report.status === 'passed') return '历史题目通过 · 当前资格无效';
  return {failed: '未通过', cancelled: '已取消', incomplete: '未完成'}[report.status] ?? '待确认';
}
