// Response shapes mirroring the FastAPI routers in api/.
// Kept in one file so the contract between backend and frontend is easy to eyeball.
// These are mirrors for the frontend to type-check against; if the server adds a field,
// add it here. The canonical source of truth is the FastAPI routers in api/routers/.

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

export interface ChartResponse {
  symbol: string
  candles: CandlePoint[]
  signals: ChartPoint[]
  patterns: ChartPoint[]
}

export interface ChartPoint {
  ts: number
  x: number
  y: number
}

export interface DigestResponse {
  date: string
  summary: string
  generated_at: string
}

export interface SignalRow {
  id: number
  symbol: string
  timestamp: string
  action: string
  rsi: number | null
  adx: number | null
  atr: number | null
  sentiment_score: number | null
  rationale: string | null
}

export interface LlmSignalRow {
  id: number
  symbol: string
  timestamp: string
  action: string
  confidence: number | null
  rationale: string | null
  rule_based_action: string | null
}

export interface MlSignalRow {
  id: number
  symbol: string
  timestamp: string
  action: string
  confidence: number | null
  rule_based_action: string | null
}

export interface NewsRow {
  id: number
  symbol: string | null
  headline: string
  source: string
  published_at: string
  scored: number | null | undefined
  score: number | null
  rationale: string | null
}

export interface SizingResponse {
  symbol: string
  action: Action
  entry_price: number
  position_size: number
  position_value: number
  risk_amount: number
  stop_loss: number | null
  take_profit: number | null
  total_risk_used_pct: number
  correlated_with: string[]
  blocked: boolean
  block_reason: string | null
}

export interface WatchlistResponse {
  symbols: string[]
}

// Wallet types
export interface WalletFixed {
  capital: number
  total_deposits: number
  total_withdrawals: number
  realized_pnl_total: number
  book_balance: number
}

export interface WalletToday {
  open_pnl: number
  open_positions_count: number
  closed_pnl: number
  total_today_pnl: number
}

export interface WalletPosition {
  position_id: number
  symbol: string
  action: Action
  position_size: number
  entry_price: number
  current_price: number
  current_value: number
  current_pnl: number
  current_pnl_pct: number
  stop_loss: number | null
  take_profit: number | null
  stop_scenario_pnl: number | null
  target_scenario_pnl: number | null
  at_risk_rupees: number | null
}

export interface WalletResponse {
  fixed: WalletFixed
  today: WalletToday
  open_positions: WalletPosition[]
  prev_days_closed_pnl: Record<string, number>
}
