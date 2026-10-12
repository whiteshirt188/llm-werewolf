import type { Persona, PlayStyle } from './types.ts';
import { playStyleNames } from './game.ts';

export function PlayStyleSelect({value, onChange, label = '玩法倾向'}: {value: Persona['play_style']; onChange: (value: PlayStyle) => void; label?: string}) {
  return <label>{label}<select value={value ?? 'serious'} onChange={e => onChange(e.target.value as PlayStyle)}>
    {Object.entries(playStyleNames).map(([style, name]) => <option key={style} value={style}>{name}</option>)}
  </select></label>;
}
