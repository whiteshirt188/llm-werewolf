export type Role = 'wolf' | 'seer' | 'villager' | 'witch' | 'hunter' | 'guard' | 'knight';
export type Potion = 'skip' | 'save' | 'poison';
export interface RuleConfig { version: string; players?: number; wolves: number; seers: number; witches?: number; hunters?: number; guards?: number; knights?: number; villagers: number; last_words?: boolean; win_condition?: 'eliminate_side' | 'parity' }
export type ActionKind = 'speak' | 'wolf_message' | 'propose_kill' | 'kill' | 'inspect' | 'vote' | 'witch' | 'protect' | 'shoot' | 'duel' | 'last_words';
export interface ActionSpec { kind: ActionKind; targets?: number[]; max_length?: number; allow_abstain?: boolean; allow_empty?: boolean; potions?: Partial<Record<Potion, number[]>> }
export interface Action { kind: ActionKind; target?: number | null; text?: string; potion?: Potion }
export interface GameEvent { id?: number; day: number; kind: string; data: Record<string, unknown>; audience?: number[] | null }
export type PlayStyle = 'serious' | 'intuitive' | 'observer' | 'provocateur' | 'playful' | 'follower';
export interface Persona { label: string; description: string; risk: 'cautious' | 'balanced' | 'aggressive'; bluff: 'low' | 'medium' | 'high'; strategy?: 'balanced' | 'evidence' | 'contradictions' | 'relationships' | 'pressure'; play_style?: PlayStyle }
export interface SeatProfile { seat: number; nickname: string; connection_id: string; persona: Persona; model?: string }
export type RecoveryStrategy = 'more_tokens' | 'without_thinking';
export interface DecisionRecovery { output_limit:number; output_tokens:number|null; reasoning_tokens:number|null;
  thinking_mode:string|null; increased_limit:number|null }
export interface GameView {
  game_id: string; mode: 'mock' | 'real'; status: 'running' | 'paused' | 'finished' | 'abandoned'; human_seat: number;
  simulated_transport?: boolean;
  pause_reason: string | null; turn_version: string | null; seat_profiles: SeatProfile[];
  reading?: boolean;
  playback_mode?: 'manual' | 'auto'; step_waiting?: boolean; step_id?: string | null;
  speech_to_vote?: boolean;
  preparation?: {active:boolean; completed:number; total:number} | null;
  decision_recovery?: DecisionRecovery | null;
  view: { seat: number; role: Role; alive: boolean; day: number; phase: string;
    rules?: RuleConfig; witch?: { potions: {save: boolean; poison: boolean}; night_victim?: number | null };
    guard?: {last_target:number | null}; knight?: {used:boolean};
    players: { seat: number; alive: boolean }[]; legal_actions: ActionSpec[]; events: GameEvent[];
    checks: { target: number; is_wolf: boolean }[]; wolf_teammates: number[];
    current_actor?: number | null; winner: 'good' | 'wolves' | null; revealed_roles?: Record<string, Role> };
}
export interface Replay {
  winner: 'good' | 'wolves'; players: { seat: number; alive: boolean; role: Role }[]; events: GameEvent[];
  usage: { seat: number; model?: string; status: string; elapsed_ms?: number;
    usage?: { input_tokens: number | null; output_tokens: number | null }; simulated: boolean }[];
}
export interface Mutation { path: string; body: Record<string, unknown> }
