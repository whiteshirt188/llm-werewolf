import { useState } from 'react';
import { nineRoster, sixRoster, villagers, rosterError, gods } from './roster.ts';
import type { Roster } from './roster.ts';

export function RosterSettings({value, onChange}: {value: Roster; onChange: (r: Roster) => void}) {
  const matches = (r: Roster) => Object.keys(r).every(k => (value[k as keyof Roster] ?? 0) === r[k as keyof Roster]) && gods(value) === gods(r);
  const [custom, setCustom] = useState(!matches(sixRoster()) && !matches(nineRoster()));
  const mode = custom ? 'custom' : value.players === 9 && value.wolves === 3 && gods(value) === 3 ? 'nine' : matches(sixRoster()) ? 'six' : 'custom';
  const error = rosterError(value);
  return <section className="roster-settings" aria-label="人数与阵容">
    <label>人数与阵容<select value={mode} onChange={e => {
      setCustom(e.target.value === 'custom');
      if (e.target.value !== 'custom') onChange(e.target.value === 'nine' ? nineRoster() : sixRoster());
      else if (gods(value) !== 3) onChange({...value,seers:1,hunters:1});
    }}><option value="six">6 人预设</option><option value="nine">9 人预设</option><option value="custom">自定义 · 6～9 人</option></select></label>
    {mode === 'custom' && <div className="roster-fields">
      <label>总人数<select value={value.players} onChange={e => onChange({...value,players:Number(e.target.value)})}>{[6,7,8,9].map(n => <option key={n} value={n}>{n} 人</option>)}</select></label>
      <label>狼人数量<select value={value.wolves} onChange={e => onChange({...value,wolves:Number(e.target.value)})}>{[1,2,3,4].map(n => <option key={n} value={n}>{n}</option>)}</select></label>
    </div>}
    {mode !== 'six' && <div className="god-selection"><p>3 个神职 · 预言家必选，另外选择两项</p><label className="check-label"><input type="checkbox" checked disabled />预言家 · 必选</label>{([['witches','女巫'],['hunters','猎人'],['guards','守卫'],['knights','骑士']] as const).map(([key,label]) => <label className="check-label" key={key}><input type="checkbox" checked={Boolean(value[key])} disabled={!value[key] && gods(value) >= 3} onChange={e => onChange({...value,seers:1,[key]:e.target.checked ? 1 : 0})} />{label}</label>)}<p className="helper">换神职时先取消一项，再选新的一项。已选 {gods(value)} / 3；身份仍随机分配。</p></div>}
    <p className="roster-summary">{value.players} 人 · {value.wolves} 狼人 / {gods(value)} 神职 / {villagers(value)} 村民</p>
    <label className="check-label"><input type="checkbox" checked={value.last_words ?? true} onChange={e => onChange({...value,last_words:e.target.checked})} />开启遗言 · 默认</label>
    <p className="helper">放逐与首夜死亡者可公开发言 300 字或跳过；猎人先遗言再开枪。后续夜晚死亡、枪杀、决斗出局无遗言。</p>
    {error ? <p role="alert" className="error-text">{error}</p> : <p className="helper">你 + {value.players - 1} 个独立玩家；身份随机分配。自定义只改阵容，不改技能与胜负规则，平衡性尚未验证。更多席位通常会增加模型调用和费用。</p>}
  </section>;
}
