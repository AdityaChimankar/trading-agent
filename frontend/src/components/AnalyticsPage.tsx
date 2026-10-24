import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Box, Button, Chip, Grid, Stack, Typography } from '@mui/material'

import { apiGet, apiPost } from '../api/client'
import { inr, num } from '../lib/format'
import { DataTable, Metric, Panel, StateMessage } from './ui'

interface Summary {
  trades: number; win_rate_pct?: number; gross_pnl?: number; charges?: number; net_pnl?: number; net_return_pct?: number
  avg_win?: number | null; avg_loss?: number | null; profit_factor?: number | null; expectancy?: number
  avg_r_multiple?: number | null; max_drawdown_pct?: number; sharpe?: number | null; sortino?: number | null
  trading_days?: number; avg_holding_minutes?: number; cost_drag_pct_of_gross?: number | null
}
interface TradeRow {
  position_id: number; symbol: string; action: string; strategy: string; quantity: number; closed_at: string
  gross_pnl: number; charges: number; charges_modelled: boolean; net_pnl: number; r_multiple: number | null
}
interface Performance {
  capital: number; overall: Summary; by_strategy: (Summary & { strategy: string })[]
  by_symbol: (Summary & { symbol: string })[]; recent_trades: TradeRow[]; sample_warning: string | null
}
interface VolumeRow {
  symbol: string; price: number; rvol: number | null; bar_surge: number | null; vwap: number | null
  price_vs_vwap: string | null; liquidity: string; avg_daily_value_cr: number; max_quantity: number
  recommendation: string; reason: string; scan_action: string; scan_score: number; agreement: string
  forecast_remaining_volume: number | null; max_quantity_remaining: number | null
}
interface OutcomeStats {
  signals: number; scored: number; hold: number; pending: number; unpriced: number; after_cutoff: number
  mean_net_bps?: number; win_rate_pct?: number; sessions?: number; session_t?: number | null; mean_alpha_bps?: number | null
}
interface DecisionOutcomes {
  report: null | {
    generated_at: string; config: { days: number; horizon_minutes: number; cost_bps: number }; notes: string
    by_strategy: Record<string, OutcomeStats>; by_action: Record<string, OutcomeStats>; by_confidence: Record<string, OutcomeStats>
  }
}
interface TrialSession {
  session: string; net_bps: number; net_at_open_bps: number | null; random_bps: number | null
  long: string[]; short: string[]; unpriced: string[]
}
interface Trial {
  status: 'not_started' | 'collecting' | 'passed' | 'failed'; start_session?: string; spec_sha256?: string
  config?: { top_k: number; cost_bps: number; train_days: number; max_lag: number; min_avg_value_cr: number }
  universe_size?: number; picks?: number; settled?: number; missed?: string[]; pending?: string[]
  done_when?: { sessions: number; t_stat_at_least: number }
  model?: RankStats; at_open?: RankStats; random_baseline?: RankStats; sessions?: TrialSession[]
  verdict?: { sessions: [string, string]; checks: Record<string, boolean> }
}
interface RankStats {
  days: number; mean_daily_net_bps?: number; hit_rate_pct?: number; t_stat?: number | null
  annualised_sharpe?: number | null; total_net_pct_points?: number; max_drawdown_pct_points?: number
}
interface RankPick { symbol: string; probability: number }
interface RankingReport {
  status: string; reason?: string; generated_at: string; interval: string; lags: number[]
  symbols_with_data: number; labelled_sessions?: number; required_sessions: number
  model?: RankStats; random_baseline?: RankStats; classifier_accuracy_pct?: number | null
  research_gate?: { passed: boolean; checks: Record<string, boolean> }
  latest_ranking?: { session: string; long: RankPick[]; short: RankPick[] } | null
  ranking_withheld?: string; limitations: string; config: { top_k: number; cost_bps: number; train_days: number }
}
interface Ranking { job: { running: boolean; started_at: string | null; error: string | null }; report: RankingReport | null }
interface DriftFeature { feature: string; psi: number; ks: number; state: 'ok' | 'warn' | 'drift' }
interface Drift {
  report: { status: string; live_samples: number; message?: string; baseline_source?: string; features: DriftFeature[]; drifted_features?: string[] }
  fallback: { active: boolean; reason?: string; since?: string }
}

const CALL_COLOR: Record<string, 'success' | 'error' | 'warning' | 'default' | 'info'> = {
  BUY_CONFIRMED: 'success', SELL_CONFIRMED: 'error', AVOID: 'warning', WATCH: 'info', NEUTRAL: 'default',
}
const STATE_COLOR = { ok: 'success', warn: 'warning', drift: 'error' } as const
const tone = (v?: number | null) => (v == null ? 'muted' : v >= 0 ? 'pos' : 'neg') as 'pos' | 'neg' | 'muted'

export default function AnalyticsPage({ onSelectSymbol }: { onSelectSymbol: (symbol: string) => void }) {
  const cache = useQueryClient()
  const perf = useQuery({ queryKey: ['performance'], queryFn: () => apiGet<Performance>('/api/v1/analytics/performance'), refetchInterval: 60_000 })
  const volume = useQuery({ queryKey: ['volume-board'], queryFn: () => apiGet<VolumeRow[]>('/api/v1/analytics/volume?top=40'), refetchInterval: 120_000 })
  const drift = useQuery({ queryKey: ['drift'], queryFn: () => apiGet<Drift>('/api/v1/analytics/drift'), refetchInterval: 300_000 })
  const evaluate = useMutation({ mutationFn: () => apiPost('/api/v1/analytics/drift/evaluate'), onSuccess: () => cache.invalidateQueries({ queryKey: ['drift'] }) })
  const ranking = useQuery({ queryKey: ['ranking'], queryFn: () => apiGet<Ranking>('/api/v1/analytics/cross-sectional'),
    refetchInterval: (q) => (q.state.data?.job.running ? 5_000 : 300_000) })
  const runRanking = useMutation({ mutationFn: () => apiPost('/api/v1/analytics/cross-sectional/run'),
    onSuccess: () => cache.invalidateQueries({ queryKey: ['ranking'] }) })
  const outcomes = useQuery({ queryKey: ['decision-outcomes'], queryFn: () => apiGet<DecisionOutcomes>('/api/v1/analytics/decision-outcomes'), refetchInterval: 300_000 })
  const scoreOutcomes = useMutation({ mutationFn: () => apiPost('/api/v1/analytics/decision-outcomes/run'),
    onSuccess: () => cache.invalidateQueries({ queryKey: ['decision-outcomes'] }) })
  const trialRf = useQuery({ queryKey: ['ranker-trial', 'rf'], queryFn: () => apiGet<Trial>('/api/v1/analytics/cross-sectional/trial?arm=rf'), refetchInterval: 300_000 })
  const trialHgb = useQuery({ queryKey: ['ranker-trial', 'hgb'], queryFn: () => apiGet<Trial>('/api/v1/analytics/cross-sectional/trial?arm=hgb'), refetchInterval: 300_000 })
  const clear = useMutation({ mutationFn: () => apiPost('/api/v1/analytics/drift/clear'), onSuccess: () => cache.invalidateQueries({ queryKey: ['drift'] }) })
  const o = perf.data?.overall

  return (
    <Stack spacing={2}>
      <Panel title="Performance (net of costs)" subtitle="Closed trades from the ledger. Net subtracts filled-order charges, or modelled Zerodha charges for manual entries.">
        <StateMessage loading={perf.isLoading} error={perf.error} empty="No closed trades yet." isEmpty={o?.trades === 0} />
        {perf.data?.sample_warning && <Alert severity="info" sx={{ mb: 2 }}>{perf.data.sample_warning}</Alert>}
        {o && o.trades > 0 && <Grid container spacing={2}>
          <Grid size={{ xs: 6, md: 2 }}><Metric label="Net P&L" value={inr(o.net_pnl)} tone={tone(o.net_pnl)} hint={`${num(o.net_return_pct, 2)}% of capital`} /></Grid>
          <Grid size={{ xs: 6, md: 2 }}><Metric label="Gross / charges" value={inr(o.gross_pnl)} hint={`charges ${inr(o.charges)}${o.cost_drag_pct_of_gross != null ? ` (${num(o.cost_drag_pct_of_gross, 0)}% of gross)` : ''}`} /></Grid>
          <Grid size={{ xs: 6, md: 2 }}><Metric label="Trades / win rate" value={`${o.trades} · ${num(o.win_rate_pct, 1)}%`} hint={`${o.trading_days} trading day(s)`} /></Grid>
          <Grid size={{ xs: 6, md: 2 }}><Metric label="Expectancy" value={inr(o.expectancy, 2)} tone={tone(o.expectancy)} hint={`avg R ${num(o.avg_r_multiple, 2)}`} /></Grid>
          <Grid size={{ xs: 6, md: 2 }}><Metric label="Profit factor" value={num(o.profit_factor, 2)} hint={`win ${inr(o.avg_win)} / loss ${inr(o.avg_loss)}`} /></Grid>
          <Grid size={{ xs: 6, md: 2 }}><Metric label="Sharpe / Sortino" value={`${num(o.sharpe, 2)} / ${num(o.sortino, 2)}`} hint={`max DD ${num(o.max_drawdown_pct, 2)}%`} /></Grid>
        </Grid>}
        {perf.data && perf.data.by_strategy.length > 0 && <Box sx={{ mt: 2 }}>
          <Typography variant="overline" color="text.secondary">Attribution by strategy</Typography>
          <DataTable rows={perf.data.by_strategy} columns={[
            { header: 'Strategy', cell: r => r.strategy },
            { header: 'Trades', cell: r => r.trades, align: 'right' },
            { header: 'Win %', cell: r => num(r.win_rate_pct, 1), align: 'right' },
            { header: 'Net P&L', cell: r => inr(r.net_pnl), align: 'right' },
            { header: 'Expectancy', cell: r => inr(r.expectancy, 2), align: 'right' },
            { header: 'Profit factor', cell: r => num(r.profit_factor, 2), align: 'right' },
          ]} />
        </Box>}
        {perf.data && perf.data.recent_trades.length > 0 && <Box sx={{ mt: 2 }}>
          <Typography variant="overline" color="text.secondary">Recent closed trades</Typography>
          <DataTable rows={perf.data.recent_trades} maxRows={10} columns={[
            { header: 'Closed', cell: t => new Date(t.closed_at).toLocaleString('en-IN', { hour12: false }) },
            { header: 'Symbol', cell: t => t.symbol },
            { header: 'Side', cell: t => t.action },
            { header: 'Gross', cell: t => inr(t.gross_pnl, 2), align: 'right' },
            { header: 'Charges', cell: t => `${inr(t.charges, 2)}${t.charges_modelled ? '*' : ''}`, align: 'right' },
            { header: 'Net', cell: t => inr(t.net_pnl, 2), align: 'right' },
            { header: 'R', cell: t => num(t.r_multiple, 2), align: 'right' },
          ]} />
          <Typography variant="caption" color="text.secondary">* modelled charges (manual entry without router orders)</Typography>
        </Box>}
      </Panel>

      <Panel title="Volume board" subtitle="Strongest scan names, by time-of-day relative volume. Confirms or contradicts each scan signal; max size is capped at a share of average daily traded value.">
        <StateMessage loading={volume.isLoading} error={volume.error} empty="No scan rows with minute history. Run a scan first." isEmpty={(volume.data ?? []).length === 0} />
        {volume.data && volume.data.length > 0 && <DataTable rows={volume.data} maxRows={15} columns={[
          { header: 'Symbol', cell: r => <Button size="small" onClick={() => onSelectSymbol(r.symbol)}>{r.symbol}</Button> },
          { header: 'Scan', cell: r => `${r.scan_action} ${num(r.scan_score, 0)}` },
          { header: 'RVOL', cell: r => num(r.rvol, 2), align: 'right' },
          { header: 'vs VWAP', cell: r => r.price_vs_vwap ?? '—' },
          { header: 'Liquidity', cell: r => `${r.liquidity} · ₹${num(r.avg_daily_value_cr, 1)} Cr/day` },
          { header: 'Max qty', cell: r => r.max_quantity.toLocaleString('en-IN'), align: 'right' },
          { header: 'Fcst left today', cell: r => r.forecast_remaining_volume == null ? '—' : r.forecast_remaining_volume.toLocaleString('en-IN'), align: 'right' },
          { header: 'Max qty (left)', cell: r => r.max_quantity_remaining == null ? '—' : r.max_quantity_remaining.toLocaleString('en-IN'), align: 'right' },
          { header: 'Volume call', cell: r => <Chip size="small" label={r.recommendation.replace('_', ' ')} color={CALL_COLOR[r.recommendation] ?? 'default'} title={r.reason} /> },
          { header: 'vs scan', cell: r => <Chip size="small" variant="outlined" label={r.agreement} color={r.agreement === 'confirms' ? 'success' : r.agreement === 'contradicts' ? 'warning' : 'default'} /> },
        ]} />}
      </Panel>

      <RankingPanel data={ranking.data} loading={ranking.isLoading} error={ranking.error}
        onRun={() => runRanking.mutate()} starting={runRanking.isPending} startError={runRanking.error} />

      <OutcomesPanel data={outcomes.data} loading={outcomes.isLoading} error={outcomes.error ?? scoreOutcomes.error}
        onRun={() => scoreOutcomes.mutate()} running={scoreOutcomes.isPending} />

      <TrialPanel arm="rf" label="random forest" data={trialRf.data} loading={trialRf.isLoading} error={trialRf.error} />
      <TrialPanel arm="hgb" label="gradient boosting" data={trialHgb.data} loading={trialHgb.isLoading} error={trialHgb.error} />

      <Panel title="ML drift monitor" subtitle="PSI and KS of live model inputs against the training-period baseline. Significant drift withdraws ML so hybrid decisions hold."
        actions={<Stack direction="row" spacing={1}>
          <Button variant="outlined" onClick={() => evaluate.mutate()} disabled={evaluate.isPending}>Evaluate now</Button>
          {drift.data?.fallback.active && <Button color="warning" variant="contained" onClick={() => clear.mutate()} disabled={clear.isPending}>Restore ML</Button>}
        </Stack>}>
        <StateMessage loading={drift.isLoading} error={drift.error} empty="" isEmpty={false} />
        {drift.data && <>
          {drift.data.fallback.active
            ? <Alert severity="error" sx={{ mb: 2 }}>ML withdrawn since {drift.data.fallback.since}: {drift.data.fallback.reason}</Alert>
            : <Alert severity={drift.data.report.status === 'ok' ? 'success' : drift.data.report.status === 'drift' ? 'error' : 'info'} sx={{ mb: 2 }}>
                {drift.data.report.message ?? `Status ${drift.data.report.status} over ${drift.data.report.live_samples} logged predictions`}
              </Alert>}
          {drift.data.report.features.length > 0 && <DataTable rows={drift.data.report.features} columns={[
            { header: 'Feature', cell: f => f.feature },
            { header: 'PSI', cell: f => f.psi.toFixed(3), align: 'right' },
            { header: 'KS', cell: f => f.ks.toFixed(3), align: 'right' },
            { header: 'State', cell: f => <Chip size="small" label={f.state} color={STATE_COLOR[f.state]} /> },
          ]} />}
          {drift.data.report.baseline_source && <Typography variant="caption" color="text.secondary">Baseline: {drift.data.report.baseline_source}</Typography>}
        </>}
      </Panel>
    </Stack>
  )
}

function OutcomesPanel({ data, loading, error, onRun, running }: {
  data?: DecisionOutcomes; loading: boolean; error: unknown; onRun: () => void; running: boolean
}) {
  const r = data?.report
  const rows = (group?: Record<string, OutcomeStats>) => Object.entries(group ?? {}).map(([name, s]) => ({ name, ...s }))
  const columns = [
    { header: 'Group', cell: (x: OutcomeStats & { name: string }) => x.name },
    { header: 'Scored', cell: (x: OutcomeStats) => `${x.scored} / ${x.signals}`, align: 'right' as const },
    { header: 'Net bps', cell: (x: OutcomeStats) => num(x.mean_net_bps, 1), align: 'right' as const },
    { header: 'Win %', cell: (x: OutcomeStats) => num(x.win_rate_pct, 1), align: 'right' as const },
    { header: 'Session t', cell: (x: OutcomeStats) => num(x.session_t, 2), align: 'right' as const },
    { header: 'Alpha bps', cell: (x: OutcomeStats) => num(x.mean_alpha_bps, 1), align: 'right' as const },
    { header: 'Hold / pending', cell: (x: OutcomeStats) => `${x.hold} / ${x.pending + x.unpriced}`, align: 'right' as const },
  ]
  return (
    <Panel title="Decision outcomes (rule vs ML vs LLM)"
      subtitle="Every stored BUY/SELL signal priced on what happened next: entry at the next bar, exit after the horizon or at 15:15, net of costs; alpha against the Nifty 50 when its candles are stored. Research only."
      actions={<Button variant="outlined" onClick={onRun} disabled={running}>{running ? 'Scoring…' : 'Score now'}</Button>}>
      <StateMessage loading={loading} error={error} empty="Not scored yet. The scheduler scores after the close at 15:55, or press Score now."
        isEmpty={!r} />
      {r && <>
        {rows(r.by_strategy).length === 0
          ? <Alert severity="info" sx={{ mb: 2 }}>No signals stored in the last {r.config.days} days.</Alert>
          : <DataTable rows={rows(r.by_strategy)} columns={columns} />}
        {rows(r.by_confidence).length > 0 && <>
          <Typography variant="overline" color="text.secondary">By confidence</Typography>
          <DataTable rows={rows(r.by_confidence)} columns={columns} />
        </>}
        <Typography variant="caption" color="text.secondary" component="p" sx={{ mt: 1 }}>
          Last {r.config.days} days · held {r.config.horizon_minutes} min · {r.config.cost_bps} bps · scored {new Date(r.generated_at).toLocaleString('en-IN', { hour12: false })}. {r.notes}
        </Typography>
      </>}
    </Panel>
  )
}

function TrialPanel({ arm, label, data, loading, error }: { arm: string; label: string; data?: Trial; loading: boolean; error: unknown }) {
  const stat = (s?: RankStats) => s && s.days > 0
    ? `${num(s.mean_daily_net_bps, 1)} bps/day · hit ${num(s.hit_rate_pct, 1)}% · t ${num(s.t_stat, 2)}`
    : '—'
  const target = data?.done_when?.sessions ?? 60
  return (
    <Panel title={`Ranker paper trial: ${label} arm (prospective)`}
      subtitle="The frozen ranker's book recorded each session after the 09:15 bar and priced at 15:15: entry at the first bar after the pick, net of costs. Only these sessions count as clean evidence. Never places orders.">
      <StateMessage loading={loading} error={error} empty={`Arm not started. Start it with: trading-agent optimize ranker-trial start --arm ${arm}`}
        isEmpty={data?.status === 'not_started'} />
      {data && data.status !== 'not_started' && <>
        <Alert severity={data.status === 'passed' ? 'success' : data.status === 'failed' ? 'error' : 'info'} sx={{ mb: 2 }}>
          {data.status === 'collecting'
            ? `Collecting: ${data.settled} of ${target} sessions settled since ${data.start_session}${data.missed?.length ? ` · ${data.missed.length} missed` : ''}${data.pending?.length ? ` · ${data.pending.length} awaiting settlement` : ''}`
            : `Trial ${data.status} on its first ${target} sessions (${data.verdict?.sessions.join(' to ')}): ${Object.entries(data.verdict?.checks ?? {}).filter(([, ok]) => !ok).map(([k]) => k.replaceAll('_', ' ')).join(', ') || 'all checks met'}`}
        </Alert>
        <Grid container spacing={2} sx={{ mb: 2 }}>
          <Grid size={{ xs: 12, md: 4 }}><Metric label="Book (entry after pick)" value={stat(data.model)} hint={`${data.model?.days ?? 0} sessions · total ${num(data.model?.total_net_pct_points, 2)} pts`} /></Grid>
          <Grid size={{ xs: 12, md: 4 }}><Metric label="Same book at the open" value={stat(data.at_open)} hint="Backtest assumption, for comparison only" /></Grid>
          <Grid size={{ xs: 12, md: 4 }}><Metric label="Random picks" value={stat(data.random_baseline)} hint="Same eligible set, entry and exit" /></Grid>
        </Grid>
        {(data.sessions ?? []).length > 0 && <DataTable rows={[...data.sessions!].reverse()} maxRows={10} columns={[
          { header: 'Session', cell: r => r.session },
          { header: 'Net bps', cell: r => num(r.net_bps, 1), align: 'right' },
          { header: 'At open', cell: r => num(r.net_at_open_bps, 1), align: 'right' },
          { header: 'Random', cell: r => num(r.random_bps, 1), align: 'right' },
          { header: 'Long', cell: r => r.long.join(', ') },
          { header: 'Short', cell: r => r.short.join(', ') },
        ]} />}
        {data.config && <Typography variant="caption" color="text.secondary" component="p" sx={{ mt: 1 }}>
          Frozen: lags 1–{data.config.max_lag} · train {data.config.train_days} · top/bottom {data.config.top_k} · ≥ ₹{data.config.min_avg_value_cr} Cr/day · {data.config.cost_bps} bps · {data.universe_size} symbols · spec {data.spec_sha256?.slice(0, 12)}. Verdict on the first {target} settled sessions only: mean &gt; 0, t ≥ {data.done_when?.t_stat_at_least ?? 2}, above random.
        </Typography>}
      </>}
    </Panel>
  )
}

function RankingPanel({ data, loading, error, onRun, starting, startError }: {
  data?: Ranking; loading: boolean; error: unknown; onRun: () => void; starting: boolean; startError: unknown
}) {
  const r = data?.report
  const running = data?.job.running ?? false
  const stat = (s?: RankStats) => s && s.days > 0
    ? `${num(s.mean_daily_net_bps, 1)} bps/day · hit ${num(s.hit_rate_pct, 1)}% · t ${num(s.t_stat, 2)} · Sharpe ${num(s.annualised_sharpe, 2)}`
    : '—'
  return (
    <Panel title="Cross-sectional ranking (research)"
      subtitle="Random forest ranks the universe each morning by the chance of beating the day's median open-to-close return; long the top k, short the bottom k (Ghosh et al. 2021). Walk-forward after costs, against random picks. Never places orders."
      actions={<Button variant="outlined" onClick={onRun} disabled={running || starting}>{running ? 'Running…' : 'Run evaluation'}</Button>}>
      <StateMessage loading={loading} error={error ?? startError} empty="No evaluation yet. Run one; it reads stored candles for the Nifty 500 list and can take several minutes."
        isEmpty={!r && !running} />
      {data?.job.error && <Alert severity="error" sx={{ mb: 2 }}>Last run failed: {data.job.error}</Alert>}
      {r && <>
        {r.status !== 'completed'
          ? <Alert severity="info" sx={{ mb: 2 }}>Insufficient history: {r.reason ?? `${r.labelled_sessions ?? 0} usable sessions`}</Alert>
          : <Alert severity={r.research_gate?.passed ? 'success' : 'warning'} sx={{ mb: 2 }}>
              Research gate {r.research_gate?.passed ? 'passed' : 'not passed'}
              {r.research_gate && ` (${Object.entries(r.research_gate.checks).filter(([, ok]) => !ok).map(([k]) => k.replaceAll('_', ' ')).join(', ') || 'all checks met'})`}
            </Alert>}
        {r.model && <Grid container spacing={2} sx={{ mb: 2 }}>
          <Grid size={{ xs: 12, md: 6 }}><Metric label="Model book (net)" value={stat(r.model)} hint={`${r.model.days} test days · total ${num(r.model.total_net_pct_points, 2)} pts · max DD ${num(r.model.max_drawdown_pct_points, 2)} pts`} /></Grid>
          <Grid size={{ xs: 12, md: 6 }}><Metric label="Random picks (net)" value={stat(r.random_baseline)} hint={`classifier accuracy ${num(r.classifier_accuracy_pct, 1)}%`} /></Grid>
        </Grid>}
        {r.latest_ranking && <Grid container spacing={2}>
          {(['long', 'short'] as const).map(side => <Grid key={side} size={{ xs: 12, md: 6 }}>
            <Typography variant="overline" color="text.secondary">{side === 'long' ? 'Buy at open' : 'Short at open'} · {r.latest_ranking!.session}</Typography>
            <DataTable rows={r.latest_ranking![side]} columns={[
              { header: 'Symbol', cell: p => p.symbol },
              { header: 'P(beat median)', cell: p => num(p.probability, 3), align: 'right' },
            ]} />
          </Grid>)}
        </Grid>}
        {r.ranking_withheld && <Typography variant="body2" color="text.secondary">{r.ranking_withheld}.</Typography>}
        <Typography variant="caption" color="text.secondary" component="p" sx={{ mt: 1 }}>
          {r.symbols_with_data} symbols · {r.interval} candles · lags {r.lags[0]}–{r.lags[r.lags.length - 1]} · train {r.config.train_days} days · top {r.config.top_k} · cost {r.config.cost_bps} bps · generated {new Date(r.generated_at).toLocaleString('en-IN', { hour12: false })}. {r.limitations}
        </Typography>
      </>}
    </Panel>
  )
}
