export type Provider = 'deepseek' | 'dashscope' | 'siliconflow';
export const providers: Record<Provider, string> = {deepseek: 'DeepSeek', dashscope: '通义千问', siliconflow: '硅基流动'};
export const hunyuanModel = 'tencent/Hy4-preview';
export const glmModel = 'zai-org/GLM-5.3';
export const qwenModel = 'qwen3.6-plus';
export const qwenModels = ['qwen3.5-plus', 'qwen3.5-plus-2026-02-15', 'qwen3.5-flash', 'qwen3.5-flash-2026-02-23',
  qwenModel, 'qwen3.6-plus-2026-04-02', 'qwen3.6-flash', 'qwen3.6-flash-2026-04-16',
  'qwen3.7-plus', 'qwen3.7-plus-2026-05-26', 'qwen3.7-max', 'qwen3.7-max-2026-05-20', 'qwen3.7-max-2026-06-08',
  'qwen3.7-flash', 'qwen3.7-flash-2026-07-15', 'qwen3.8-max', 'qwen3.8-max-0902', 'qwen3.8-flash',
  'qwen-plus', 'qwen-plus-latest', 'qwen-flash'];
export const supportsThinking = (provider: Provider) => provider === 'deepseek' || provider === 'dashscope';
export const playableConnection = (c: {provider: Provider; model: string}) => c.provider === 'deepseek' ||
  (c.provider === 'dashscope' && qwenModels.includes(c.model)) ||
  (c.provider === 'siliconflow' && [hunyuanModel, glmModel].includes(c.model));
export type Account = {user: {id: string; username: string} | null; csrf_token: string | null};
export type Generation = {temperature: number; max_output_tokens: number; target_output_tokens: number; reasoning_budget: number | null;
  thinking_mode?: 'disabled' | 'low'; thinking_output_tokens?: number};
export type SavedConnection = {id: string; label: string; provider: Provider; model: string; base_url: string;
  generation: Generation; has_key: boolean; key_mask: string; verification: 'not_verified'};
export const defaultGeneration = (): Generation => ({temperature: .7, max_output_tokens: 768, target_output_tokens: 256, reasoning_budget: null,
  thinking_mode: 'disabled', thinking_output_tokens: 4096});
export function connectionBody(label: string, provider: Provider, model: string, generation: Generation, key: string, editing: boolean) {
  generation = {...defaultGeneration(), ...generation};
  if (!label.trim() || !model.trim()) throw new Error('请填写连接名称和模型 ID。');
  if (!editing && !key) throw new Error('新连接需要填写 API Key。');
  if (/\s/u.test(key)) throw new Error('API Key 不能包含空白。');
  if (!Number.isFinite(generation.temperature) || generation.temperature < 0 || generation.temperature > 2 ||
      ![generation.max_output_tokens, generation.target_output_tokens].every(v => Number.isSafeInteger(v) && v > 0) ||
      (generation.reasoning_budget !== null && (!Number.isSafeInteger(generation.reasoning_budget) || generation.reasoning_budget < 1))) {
    throw new Error('请检查生成参数：温度为 0–2，输出和推理预算为正整数。');
  }
  if (!['disabled', 'low'].includes(generation.thinking_mode!) ||
      !Number.isSafeInteger(generation.thinking_output_tokens) || generation.thinking_output_tokens! < 1024 || generation.thinking_output_tokens! > 16384) {
    throw new Error('请选择关闭或低档思考，单次思考与回答额度为 1024–16384 tokens。');
  }
  if (!supportsThinking(provider) && generation.thinking_mode !== 'disabled') throw new Error('这个思考开关用于 DeepSeek 和通义千问。');
  return {label: label.trim(), provider, model: model.trim(), generation, ...(key ? {api_key: key} : {})};
}
