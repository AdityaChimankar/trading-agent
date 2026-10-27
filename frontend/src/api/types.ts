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
  scanned?: string[]
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
  // Null - never 0 - when the monitor cannot vouch for a mark: the newest
  // candle is stale during the session, or the symbol has no usable candle at
  // all. Null means "unknown", which is a different claim from "flat".
  current_price: number | null
  current_value: number | null
  pnl: number | null
  pnl_pct: number | null
  // HOLD = no exit condition on fresh data. SELL = an exit condition fired.
  // STALE = no exit condition, but the candle is behind the feed, so HOLD
  // cannot be confirmed. NO_DATA = no usable candle to judge against.
  recommendation: 'HOLD' | 'SELL' | 'STALE' | 'NO_DATA'
  reason: string
  stale: boolean
  candle_age_minutes: number | null
  candle_timestamp: string | null
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
  // Symbols left out of the lists above because an open position already
  // exists in them - lets the panel explain a list shorter than `n`.
  excluded: string[]
  // Effective capital used for sizing - the wallet's available balance when
  // the caller didn't pass one. Lets the panel show what money it sized with.
  available_capital?: number
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
  ema9?: (number | null)[]
  ema21?: (number | null)[]
  vwap?: (number | null)[]
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
      // The bars shown: 1-minute while fresh, else the live feed's interval.
      interval?: string
      candles: CandleArrays
      indicators: IndicatorArrays
      patterns: PatternFlags
      signal_markers: { timestamp: string; action: Action }[]
      latest: { close: number; rsi: number | null; adx: number | null; atr: number | null }
    }

export interface NewsRow {
  symbol: string
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

// --- Transaction history (api/routers/history.py) ------------------------
export type TransactionType = 'deposit' | 'withdrawal' | 'realized_pnl'

export interface TransactionRow {
  id: number
  type: TransactionType
  amount: number
  note: string | null
  symbol: string | null
  trade_id: number | null
  transacted_at: string
}

export interface TradeRow {
  id: number
  symbol: string
  action: Action
  position_id: number | null
  entry_price: number
  entry_at: string
  exit_price: number
  exit_at: string
  position_size: number
  realized_pnl_rupees: number
  exit_reason: string
  slippage_deducted: number
}

export interface DailyPnlRow {
  date: string
  trades: number
  closed_pnl: number
}

export interface TransactionsResponse {
  summary: {
    deposits: number
    withdrawals: number
    realized_pnl: number
    net: number
    transaction_count: number
    trade_count: number
    wins: number
    losses: number
    win_rate_pct: number | null
    gross_profit: number
    gross_loss: number
    slippage_total: number
    best: { symbol: string; realized_pnl_rupees: number; exit_at: string } | null
    worst: { symbol: string; realized_pnl_rupees: number; exit_at: string } | null
    first_at: string | null
    last_at: string | null
  }
  ledger: TransactionRow[]
  trades: TradeRow[]
  daily: DailyPnlRow[]
}

// --- Signal pipeline (api/routers/pipeline.py) ---------------------------
export interface RuleSignalRow {
  timestamp: string
  action: Action
  rsi: number | null
  adx: number | null
  atr: number | null
  sentiment_score: number | null
  rationale: string | null
}

export interface AgentSignal {
  timestamp: string
  action: Action
  confidence: number | null
  // The rule path's call captured in the SAME cycle - null when the row
  // predates that column, which is why agreement is tri-state in the UI.
  rule_based_action: Action | null
  rationale?: string | null
  // False when the agent sat out (HOLD). An abstention is not a disagreement,
  // so the UI must not render HOLD as "✗ rules".
  is_call: boolean
  agrees_with_rule: boolean
  agrees_with_plan: boolean
}

export interface PipelineRow {
  symbol: string
  direction: 'bullish' | 'bearish'
  action: Action
  technical_score: number
  has_open_position: boolean
  rule: RuleSignalRow | null
  ml: AgentSignal | null
  llm: AgentSignal | null
  blocked: boolean
  block_reason: string | null
  sizing: {
    entry_price: number | null
    stop_loss: number | null
    take_profit: number | null
    suggested_size: number | null
    suggested_value: number | null
    total_risk_used_pct: number | null
    correlated_with: string[]
  }
}

export interface PipelineResponse {
  summary: {
    symbols: number
    sized: number
    blocked: number
    open_positions: number
    ml_calls: number
    ml_compared: number
    ml_agreement_pct: number | null
    llm_calls: number
    llm_compared: number
    llm_agreement_pct: number | null
  }
  rows: PipelineRow[]
}

export interface TechnicalReading { name: string; value: number; verdict: 'Bullish' | 'Bearish' | 'Neutral' }
export interface PivotLevels {
  session: string; based_on: string; prev_high: number; prev_low: number; prev_close: number; price: number
  pivot: number; r1: number; r2: number; r3: number; s1: number; s2: number; s3: number
  nearest_resistance: number | null; nearest_support: number | null
}
export interface TechnicalsResponse {
  symbol: string; interval?: string; as_of?: string; insufficient_data?: boolean
  readings?: TechnicalReading[]; bullish_count?: number; bearish_count?: number; neutral_count?: number
  total?: number; verdict?: string; pivots?: PivotLevels | null
}

export interface ReplayOutcomes {
  count: number
  p_target: number
  p_stop: number
  p_neither: number
  p_green: number
  avg_r: number
  avg_r_net: number
  targets: number
  stops: number
  neither: number
}

export interface TradeReplayResponse {
  symbol: string
  side: 'BUY' | 'SELL'
  entry: number
  stop: number
  target: number
  entry_time: string
  first_bar: string
  exit_time: string
  bar_minutes: number
  bars_left: number
  risk_per_share: number
  r_target: number
  break_even_share: number
  mean_block_minutes: number
  config: { replays: number; seed: number; sessions: number; window_minutes: number; cost_bps: number }
  pool: { sessions: number; first: string; last: string; interval: string }
  volatility: { today: number | null; today_bars: number; recent: number; used: number; ratio: number }
  simulation: ReplayOutcomes & {
    close_p5: number
    close_median: number
    close_p95: number
    avg_r_net_range: [number, number]
    share_range: [number, number]
    bands: { time: string[]; p5: number[]; p25: number[]; p50: number[]; p75: number[]; p95: number[] }
  }
  variants: { single_bars: ReplayOutcomes; unscaled: ReplayOutcomes }
  history: ReplayOutcomes
  verdict: string
  conclusion: {
    status: 'conclusive_negative' | 'conclusive_positive' | 'inconclusive'
    line: string
    checks: { name: string; passed: boolean; detail: string }[]
  }
  notes: string[]
  limitations: string
}
