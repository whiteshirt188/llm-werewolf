import { useEffect, useState } from 'react';
import { request } from './api.ts';
import type { GameView } from './types.ts';
type Usage = {calls:number; known_input_tokens:number; known_output_tokens:number; unknown_usage_calls:number;
  known_cached_input_tokens:number; known_reasoning_tokens:number; unknown_cached_usage_calls:number; unknown_reasoning_usage_calls:number};
export function GameUsage({game}: {game:GameView}) {
  const [usage,setUsage] = useState<Usage | null>(null), [failed,setFailed] = useState(false);
  useEffect(() => {
    let stopped = false, pending = false;
    const load = async () => {
      if (pending) return;
      pending = true;
      try {const value = await request<Usage>(`/api/games/${game.game_id}/usage`); if (!stopped) {setUsage(value); setFailed(false);}}
      catch {if (!stopped) setFailed(true);}
      finally {pending = false;}
    };
    void load();
    const timer = ['finished','abandoned'].includes(game.status) ? undefined : setInterval(() => {void load();},3000);
    return () => {stopped = true; clearInterval(timer);};
  },[game.game_id,game.status]);
  return <details className="panel table-tools"><summary>本局调用用量 {usage ? `· ${usage.calls} 次（含重试与预取）` : ''}</summary>
    {failed && <p className="error-text">暂时无法读取用量，不代表用量为零。</p>}
    {usage && <><p>已知输入 {usage.known_input_tokens} / 输出 {usage.known_output_tokens} tokens；{usage.unknown_usage_calls} 次主用量未知。</p><p className="helper">缓存输入子项 {usage.known_cached_input_tokens}、推理子项 {usage.known_reasoning_tokens} 已包含在总量中，不重复累计；分别有 {usage.unknown_cached_usage_calls} / {usage.unknown_reasoning_usage_calls} 次未报告。未配置价格，不估算金额，最终以供应商账单为准。资格考试不计入本局；对局中只展示合计，不暴露夜间行动者。</p></>}
    {game.simulated_transport && <p className="helper">隔离模拟接口，没有供应商实际账单。</p>}
  </details>;
}
