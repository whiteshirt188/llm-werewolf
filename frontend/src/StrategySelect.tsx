import type { Persona } from './types.ts';

export function StrategySelect({value, onChange, label = '决策风格'}: {value: Persona['strategy']; onChange: (value: NonNullable<Persona['strategy']>) => void; label?: string}) {
  return <label>{label}<select value={value ?? 'balanced'} onChange={e => onChange(e.target.value as NonNullable<Persona['strategy']>)}>
    <option value="balanced">综合判断</option><option value="evidence">证据优先</option><option value="contradictions">矛盾审查</option><option value="relationships">票型与关系</option><option value="pressure">主动质疑</option>
  </select><small className="helper">影响判断侧重点，不强制分票；语气个性单独设置。</small></label>;
}
