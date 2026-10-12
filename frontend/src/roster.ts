import type { RuleConfig } from './types.ts';

export type Roster = {players: number; wolves: number; seers: number; witches: number; hunters?: number; guards?: number; knights?: number; last_words?: boolean};
export const sixRoster = (): Roster => ({players:6, wolves:2, seers:1, witches:1});
export const nineRoster = (): Roster => ({players:9, wolves:3, seers:1, witches:1, hunters:1, guards:0, knights:0});
export const gods = (r: Roster) => r.seers + r.witches + (r.hunters ?? 0) + (r.guards ?? 0) + (r.knights ?? 0);
export const villagers = (r: Roster) => r.players - r.wolves - gods(r);
export const seatNumbers = (players: number) => Array.from({length:players}, (_, i) => i + 1);
export function rosterError(r: Roster): string {
  if (r.last_words !== undefined && typeof r.last_words !== 'boolean') return '遗言开关须为布尔值。';
  if (![r.players,r.wolves,r.seers,r.witches].every(Number.isInteger) || r.players < 6 || r.players > 9) return '总人数须为 6～9 人。';
  if (r.wolves < 1 || r.wolves >= r.players - r.wolves) return '至少一名狼人，且狼人必须少于好人。';
  if (r.seers !== 1) return '预言家为必选项。';
  if (![r.witches,r.hunters ?? 0,r.guards ?? 0,r.knights ?? 0].every(n => [0,1].includes(n))) return '各神职最多一名。';
  if ((r.players > 6 || r.hunters || r.guards || r.knights) && gods(r) !== 3) return '需要 3 名神职：预言家 + 另外两名不同神职。';
  if (villagers(r) < 1) return '至少保留一名村民。';
  return '';
}
export function ruleConfig(r: Roster): RuleConfig {
  return {...r, last_words:r.last_words ?? true, villagers:villagers(r), version:'preview', win_condition:'eliminate_side'};
}
export function cleanRoster(r: Roster): Roster {
  const error = rosterError(r);
  if (error) throw new Error(error);
  return {players:r.players, wolves:r.wolves, seers:r.seers, witches:r.witches, hunters:r.hunters ?? 0, guards:r.guards ?? 0, knights:r.knights ?? 0, ...(r.last_words === undefined ? {} : {last_words:r.last_words})};
}
