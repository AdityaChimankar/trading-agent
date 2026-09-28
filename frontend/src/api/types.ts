// Response shapes mirroring the FastAPI routers in api/. Kept in one file
// so the contract between backend and frontend is easy to eyeball.

export type Action = 'BUY' | 'SELL' | 'HOLD'
export type Bias = 'bullish' | 'bearish' | 'neutral'

export interface RankedEntry {
  symbol: string
  rsi: number | null
  adx: number | null
  trend: 'up' | 'down' | 'flat'
  pattern_bias: Bias
  patterns: string[]
  bullish_score: number
  bearish_score: number
  has_open_position: boolean
  dominant_bias: Bias | null
  contradiction: string | null
  // Present only on the sized lists returned alongside `scored`.
  action?: Action
  entry_price?: number | null
  stop_loss?: number | null
  take_profit?: number | null
  suggested_size?: number | null
  suggested_value?: number | null
  blocked?: boolean
  block_reason?: string | null
  total_risk_used_pct?: number
  correlated_with?: string[]
}

export interface RankingsResponse {
  scored: RankedEntry[]
  bullish: RankedEntry[]
  bearish: RankedEntry[]
  contradictions: RankedEntry[]
}

export interface Position {
  id: number
  symbol: string
  action: Action
  entry_price: number
  position_size: number
  position_value: number
  risk_amount: number
  stop_loss: number | null
  take_profit: number | null
  opened_at: string
  status: string
}

export interface PositionStatus {
  position: Position
  current_price: number
  current_value: number
  pnl: number
  pnl_pct: number
  recommendation: 'HOLD' | 'SELL'
  reason: string
}

export interface Opportunity {
  symbol: string
  action: Action
  conviction_score: number
  technical_score: number
  agreements: string[]
  disagreements: string[]
  entry_price: number | null
  stop_loss: number | null
  take_profit: number | null
  suggested_size: number | null
  suggested_value: number | null
  blocked: boolean
  block_reason: string | null
  room_to_target: number | null
  realistic_reward_risk: number | null
}

export interface OpportunitiesResponse {
  bullish: Opportunity[]
  bearish: Opportunity[]
}

export interface BasePlan {
  symbol: string
  action: Action
  entry_price: number
  atr: number
  stop_loss: number
  take_profit: number
  stop_distance: number
  risk_amount: number
  position_size: number
  position_value: number
  capped_by_max_position: boolean
}

export interface AdjustedPlan {
  base_plan: BasePlan | null
  approved_size: number
  approved_value: number
  blocked: boolean
  block_reason: string | null
  total_risk_used_pct: number
  cluster_exposure_pct: number
  correlated_with: string[]
}

export type SizingResponse =
  | { symbol: string; has_signal: false; action: Action | null; adjusted: null }
  | { symbol: string; has_signal: true; action: Action; adjusted: AdjustedPlan }

export interface CandleArrays {
  timestamp: string[]
  open: number[]
  high: number[]
  low: number[]
  close: number[]
  volume: number[]
}

export interface IndicatorArrays {
  rsi: (number | null)[]
  adx: (number | null)[]
  atr: (number | null)[]
  ma20: (number | null)[]
  ma50: (number | null)[]
}

export interface PatternFlags {
  doji: boolean[]
  hammer: boolean[]
  bullish_engulfing: boolean[]
  bearish_engulfing: boolean[]
}

export type ChartResponse =
  | { symbol: string; insufficient_data: true; candles: null }
  | {
      symbol: string
      insufficient_data: false
      candles: CandleArrays
      indicators: IndicatorArrays
      patterns: PatternFlags
      signal_markers: { timestamp: string; action: Action }[]
      latest: { close: number; rsi: number | null; adx: number | null; atr: number | null }
    }

export interface NewsRow {
  headline: string
  published_at: string
  source: string | null
  score: number | null
  rationale: string | null
}

export interface SignalRow {
  timestamp: string
  action: Action
  rsi: number | null
  adx: number | null
  sentiment_score: number | null
  rationale: string | null
}

export interface LlmSignalRow {
  timestamp: string
  llm_action: Action
  rule_based_action: Action | null
  confidence: number | null
  rationale: string | null
}

export interface MlSignalRow {
  timestamp: string
  ml_action: Action
  rule_based_action: Action | null
  confidence: number | null
}

export interface AgentSignalsResponse<T> {
  agreement_pct: number | null
  rows: T[]
}

export interface DigestResponse {
  date: string
  summary: string | null
  generated_at: string | null
}

export interface WatchlistResponse {
  symbols: string[]
}
