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
}
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
          { header: 'Volume call', cell: r => <Chip size="small" label={r.recommendation.replace('_', ' ')} color={CALL_COLOR[r.recommendation] ?? 'default'} title={r.reason} /> },
          { header: 'vs scan', cell: r => <Chip size="small" variant="outlined" label={r.agreement} color={r.agreement === 'confirms' ? 'success' : r.agreement === 'contradicts' ? 'warning' : 'default'} /> },
        ]} />}
      </Panel>

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
