import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Box, Button, Chip, Grid, MenuItem, Stack, TextField, Typography } from '@mui/material'
import { useState } from 'react'

import { apiGet, apiPost } from '../api/client'
import { inr, num, timeAgo } from '../lib/format'
import { tokens } from '../theme'
import { DataTable, Metric, Panel, StateMessage } from './ui'

interface Check {
  rows: number; current: number; stale: number; lagging: number; no_rsi: number; no_macd: number
  partial_52w: number; unclassified: number; quoted: number; median_bar_age_minutes: number | null; notes: string[]
}
interface QuoteStatus { status: 'ok' | 'disabled' | 'unavailable'; returned?: number; requested?: number; reason?: string }
interface Status {
  status: 'idle' | 'running' | 'completed' | 'cancelled' | 'error' | 'interrupted'
  processed: number; total: number; started_at?: string; completed_at?: string; latest_session?: string
  universe?: string; sector_map_loaded?: boolean; skipped: number; errors: number; check: Check | null
  quotes?: QuoteStatus | null
}
interface Sector {
  sector: string; stocks: number; advances: number; declines: number; avg_change_pct: number
  median_change_pct: number; turnover: number
  top_gainer: { symbol: string; change_pct: number }; top_loser: { symbol: string; change_pct: number }
}
interface Sectors { sectors: Sector[]; unclassified_stocks: number; method: string }
interface Screen { id: string; title: string; bias: 'bullish' | 'bearish' | 'neutral'; description: string; count: number }
interface Row {
  symbol: string; company: string | null; industry: string | null; ltp: number; change_pct: number | null
  change_5m_pct: number | null; change_15m_pct: number | null; change_1h_pct: number | null
  volume: number; volume_vs_week_pct: number | null; turnover: number; year_high: number; year_low: number
  full_year: boolean; rsi: number | null; rsi_live: number | null; macd_hist: number | null
  r1: number; s1: number; r1_breakout: boolean; near_r1: boolean; s1_breakdown: boolean; as_of: string
  price_source: 'quote' | 'candles'; new_52w_high: boolean; new_52w_low: boolean
}
type Mover = 'gainers' | 'losers' | 'volume'
const MOVER_QUERY: Record<Mover, string> = {
  gainers: 'sort=change_pct&order=desc', losers: 'sort=change_pct&order=asc', volume: 'sort=volume_vs_week_pct&order=desc',
}
interface Stocks { count: number; rows: Row[] }
interface Stat { sessions: number; mean_bps?: number; hit_rate_pct?: number; t_stat?: number | null }
interface OutcomePart { net: Stat; edge_vs_universe: Stat; avg_names: number }
interface ScreenOutcome {
  id: string; title: string; side: 'long' | 'short'
  verdict: 'edge_candidate' | 'no_edge' | 'inconclusive' | 'insufficient_sessions'
  development: OutcomePart; holdout: OutcomePart
}
interface Outcomes {
  status: string; generated_at?: string; sessions?: number; holdout_from?: string; last_session?: string
  symbols?: number; config?: { decision_minute: number; cost_bps: number }; screens?: ScreenOutcome[]
  universe_baseline?: { long_net: Stat; short_net: Stat }
}
const VERDICT_COLOR = { edge_candidate: 'success', no_edge: 'error', inconclusive: 'warning', insufficient_sessions: 'default' } as const
const bps = (v?: number) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(1)}`)
interface SymbolCheck {
  current: Row; saved_as_of: string | null; changed: Record<string, { saved: unknown; now: unknown }>
}

const BIAS_COLOR = { bullish: 'success', bearish: 'error', neutral: 'default' } as const
const INDICES = ['NIFTY50', 'NIFTY100', 'NIFTY500', 'NIFTYMIDCAP150', 'NIFTYSMALLCAP250', 'NIFTYMICROCAP250']
const SORTS: Record<string, string> = {
  turnover: 'Turnover', change_pct: '1D change', change_5m_pct: '5 min change', volume_vs_week_pct: 'Volume vs 1W',
  rsi: 'RSI', year_high_proximity_pct: 'Distance to 52W high', ltp: 'Price',
}
const signed = (v: number | null | undefined, digits = 2) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`)
const color = (v: number | null | undefined) => (v == null || v === 0 ? 'text.secondary' : v > 0 ? tokens.pos : tokens.neg)
const Change = ({ value }: { value: number | null | undefined }) => <Box component="span" sx={{ color: color(value), fontFamily: tokens.mono }}>{signed(value)}</Box>

interface Filters {
  screen: string; window: string; minChange: string; rsi: string; macd: string; near52w: string
  breakout: string; index: string; sector: string; sort: string; order: 'asc' | 'desc'
}
const NO_FILTERS: Filters = {
  screen: '', window: '1d', minChange: '', rsi: '', macd: '', near52w: '', breakout: '', index: '', sector: '',
  sort: 'turnover', order: 'desc',
}

function query(f: Filters): string {
  const p = new URLSearchParams({ sort: f.sort, order: f.order, limit: '200' })
  if (f.screen) p.set('screen', f.screen)
  if (f.minChange) { p.set('min_change_pct', f.minChange); p.set('change_window', f.window) }
  if (f.rsi === 'oversold') p.set('rsi_max', '30')
  if (f.rsi === 'overbought') p.set('rsi_min', '70')
  if (f.rsi === 'neutral') { p.set('rsi_min', '30'); p.set('rsi_max', '70') }
  if (f.macd) p.set('macd', f.macd)
  if (f.near52w) p.set('near_52w', f.near52w)
  if (f.breakout.startsWith('near_')) p.set('near_level', f.breakout.slice(5))
  else if (f.breakout) p.set('breakout', f.breakout)
  if (f.index) p.set('index', f.index)
  if (f.sector) p.set('sector', f.sector)
  return p.toString()
}

function Select({ label, value, onChange, options }: {
  label: string; value: string; onChange: (v: string) => void; options: [string, string][]
}) {
  return (
    <TextField select size="small" label={label} value={value} onChange={(e) => onChange(e.target.value)} sx={{ minWidth: 140 }}>
      {options.map(([v, text]) => <MenuItem key={v} value={v}>{text}</MenuItem>)}
    </TextField>
  )
}

function ScreenTrackRecord({ outcomes }: { outcomes?: Outcomes }) {
  if (!outcomes) return null
  if (outcomes.status !== 'completed' || !outcomes.screens) {
    return (
      <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5 }}>
        No track record yet. Run <code>trading-agent optimize screen-outcomes</code> to test each screen on stored history.
      </Typography>
    )
  }
  const cfg = outcomes.config
  const at = cfg ? `${String(Math.floor(cfg.decision_minute / 60)).padStart(2, '0')}:${String(cfg.decision_minute % 60).padStart(2, '0')}` : ''
  const edges = outcomes.screens.filter((s) => s.verdict === 'edge_candidate')
  return (
    <Box sx={{ mt: 2 }}>
      <Alert severity={edges.length ? 'info' : 'warning'} sx={{ mb: 1.5 }}>
        {edges.length
          ? `${edges.map((s) => s.title).join(', ')} passed the historical check; paper-test before relying on it.`
          : `Historical check: no screen made money after ${cfg?.cost_bps ?? 14} bps costs, entering at ${at} and exiting by 15:15. They are watchlists, not signals.`}
        {' '}{outcomes.sessions} sessions to {outcomes.last_session}, holdout from {outcomes.holdout_from}, {outcomes.symbols} liquid names.
      </Alert>
      <DataTable rows={outcomes.screens} maxRows={10} columns={[
        { header: 'Screen', cell: r => r.title },
        { header: 'Side', cell: r => r.side },
        { header: 'Names/day', cell: r => num(r.holdout.avg_names, 0), align: 'right' },
        { header: 'Holdout net bps', cell: r => <Box component="span" sx={{ color: color(r.holdout.net.mean_bps), fontFamily: tokens.mono }}>{bps(r.holdout.net.mean_bps)}</Box>, align: 'right' },
        { header: 'vs universe', cell: r => <Box component="span" sx={{ color: color(r.holdout.edge_vs_universe.mean_bps), fontFamily: tokens.mono }}>{bps(r.holdout.edge_vs_universe.mean_bps)}</Box>, align: 'right' },
        { header: 'Dev net / vs universe', cell: r => `${bps(r.development.net.mean_bps)} / ${bps(r.development.edge_vs_universe.mean_bps)}`, align: 'right' },
        { header: 't (holdout)', cell: r => num(r.holdout.net.t_stat ?? null, 2), align: 'right' },
        { header: 'Verdict', cell: r => <Chip size="small" variant="outlined" color={VERDICT_COLOR[r.verdict]} label={r.verdict.replace('_', ' ')} /> },
      ]} />
    </Box>
  )
}

export default function ScreenerPage({ onSelectSymbol }: { onSelectSymbol: (symbol: string) => void }) {
  const cache = useQueryClient()
  const [filters, setFilters] = useState<Filters>(NO_FILTERS)
  const set = (patch: Partial<Filters>) => setFilters((f) => ({ ...f, ...patch }))

  const status = useQuery({ queryKey: ['screener-status'], queryFn: () => apiGet<Status>('/api/v1/screener/status'),
    refetchInterval: (q) => (q.state.data?.status === 'running' ? 3_000 : 60_000) })
  const version = status.data?.completed_at ?? ''
  const sectors = useQuery({ queryKey: ['screener-sectors', version], queryFn: () => apiGet<Sectors>('/api/v1/screener/sectors') })
  const screens = useQuery({ queryKey: ['screener-screens', version], queryFn: () => apiGet<Screen[]>('/api/v1/screener/screens') })
  const stocks = useQuery({ queryKey: ['screener-stocks', version, filters],
    queryFn: () => apiGet<Stocks>(`/api/v1/screener/stocks?${query(filters)}`) })
  const outcomes = useQuery({ queryKey: ['screen-outcomes'], queryFn: () => apiGet<Outcomes>('/api/v1/screener/outcomes'),
    refetchInterval: 600_000 })
  const [mover, setMover] = useState<Mover>('gainers')
  const [moverIndex, setMoverIndex] = useState('NIFTY100')
  const movers = useQuery({ queryKey: ['screener-movers', version, mover, moverIndex],
    queryFn: () => apiGet<Stocks>(`/api/v1/screener/stocks?${MOVER_QUERY[mover]}&limit=8${moverIndex ? `&index=${moverIndex}` : ''}`) })
  const update = useMutation({ mutationFn: (universe: string) => apiPost('/api/v1/screener/run', { universe }),
    onSuccess: () => cache.invalidateQueries({ queryKey: ['screener-status'] }) })
  const check = useMutation({ mutationFn: (symbol: string) => apiGet<SymbolCheck>(`/api/v1/screener/symbols/${encodeURIComponent(symbol)}`) })

  const s = status.data
  const running = s?.status === 'running'
  const c = s?.check

  return (
    <Stack spacing={2}>
      <Panel
        title="Market screener"
        subtitle="Sector breadth, trading screens and intraday filters from stored minute candles. RSI(14) and MACD(12,26,9) use completed daily sessions; pivots use the previous session."
        actions={<>
          <Button variant="contained" disabled={running || update.isPending} onClick={() => update.mutate('all')}>
            {running ? `Updating ${s?.processed ?? 0}/${s?.total ?? 0}` : 'Update technicals'}
          </Button>
          <Button variant="outlined" disabled={running || update.isPending} onClick={() => update.mutate('indexed')}
            title="Only the ~750 Nifty Total Market names: faster">Indexed only</Button>
        </>}
      >
        <StateMessage loading={status.isLoading} error={status.error ?? update.error} empty="" isEmpty={false} />
        {s && s.status !== 'completed' && !running && (
          <Alert severity={s.status === 'idle' ? 'info' : 'warning'} sx={{ mb: 2 }}>
            {s.status === 'idle' ? 'No screener scan yet. Click "Update technicals" to compute one.'
              : `Last scan ${s.status}. Showing the previous completed rows, if any.`}
          </Alert>
        )}
        {s && !s.sector_map_loaded && s.status !== 'idle' && (
          <Alert severity="info" sx={{ mb: 2 }}>No sector map; run <code>trading-agent sector-map</code> for sector breadth and index filters.</Alert>
        )}
        {s?.quotes?.status === 'unavailable' && (
          <Alert severity="warning" sx={{ mb: 2 }}>
            Live Kite quotes were unavailable ({s.quotes.reason}); prices come from stored candles, so names the ticker
            does not stream show as lagging. Run <code>trading-agent login</code> and update again.
          </Alert>
        )}
        {c && <Grid container spacing={2}>
          <Grid size={{ xs: 6, md: 2 }}><Metric label="Session" value={s?.latest_session ?? '—'} hint={`updated ${timeAgo(s?.completed_at)}`} /></Grid>
          <Grid size={{ xs: 6, md: 2 }}><Metric label="Current rows" value={c.current}
            hint={`of ${c.rows} scanned · ${c.quoted ?? 0} live-quoted`} /></Grid>
          <Grid size={{ xs: 6, md: 2 }}><Metric label="Stale / lagging" value={`${c.stale} / ${c.lagging}`} tone={c.stale ? 'neg' : undefined}
            hint="excluded from screens and sectors" /></Grid>
          <Grid size={{ xs: 6, md: 2 }}><Metric label="Median bar age" value={c.median_bar_age_minutes == null ? '—' : `${num(c.median_bar_age_minutes, 0)} min`} /></Grid>
          <Grid size={{ xs: 6, md: 2 }}><Metric label="No RSI / MACD" value={`${c.no_rsi} / ${c.no_macd}`} hint="too few sessions" /></Grid>
          <Grid size={{ xs: 6, md: 2 }}><Metric label="Partial 52W / no sector" value={`${c.partial_52w} / ${c.unclassified}`} /></Grid>
        </Grid>}
      </Panel>

      <Panel title="Top movers today" subtitle="Biggest 1D moves, or today's volume against the 1-week average, within an index."
        actions={<Select label="Index" value={moverIndex} onChange={setMoverIndex}
          options={[['', 'All stocks'], ...INDICES.map((i): [string, string] => [i, i])]} />}>
        <Box sx={{ display: 'flex', gap: 1, mb: 1.5 }}>
          {(['gainers', 'losers', 'volume'] as Mover[]).map((m) => (
            <Chip key={m} label={m === 'volume' ? 'Volume shockers' : m === 'gainers' ? 'Gainers' : 'Losers'}
              color={mover === m ? 'primary' : 'default'} variant={mover === m ? 'filled' : 'outlined'} onClick={() => setMover(m)} />
          ))}
        </Box>
        <StateMessage loading={movers.isLoading} error={movers.error} empty="No current rows for this index." isEmpty={movers.data?.count === 0} />
        {movers.data && movers.data.rows.length > 0 && <DataTable rows={movers.data.rows} maxRows={8} columns={[
          { header: 'Company', cell: r => <Button size="small" onClick={() => onSelectSymbol(r.symbol)} sx={{ p: 0, minWidth: 0 }}>{r.symbol}</Button> },
          { header: 'Price', cell: r => inr(r.ltp, 2), align: 'right' },
          { header: '1D', cell: r => <Change value={r.change_pct} />, align: 'right' },
          { header: 'Volume', cell: r => r.volume.toLocaleString('en-IN'), align: 'right' },
          { header: 'vs 1W avg', cell: r => <Change value={r.volume_vs_week_pct} />, align: 'right' },
        ]} />}
      </Panel>

      <Panel title="Sectors trending today" subtitle={sectors.data?.method}>
        <StateMessage loading={sectors.isLoading} error={sectors.error} empty="No classified rows yet." isEmpty={(sectors.data?.sectors ?? []).length === 0} />
        {sectors.data && sectors.data.sectors.length > 0 && <DataTable rows={sectors.data.sectors} maxRows={12} columns={[
          { header: 'Sector', cell: r => <Button size="small" onClick={() => set({ sector: r.sector })}>{r.sector}</Button> },
          { header: 'Gainers / losers', cell: r => <Box component="span" sx={{ fontFamily: tokens.mono }}>
            <Box component="span" sx={{ color: tokens.pos }}>{r.advances}</Box> / <Box component="span" sx={{ color: tokens.neg }}>{r.declines}</Box>
            <Box component="span" sx={{ color: 'text.secondary' }}> of {r.stocks}</Box></Box> },
          { header: '1D change', cell: r => <Change value={r.avg_change_pct} />, align: 'right' },
          { header: 'Median', cell: r => <Change value={r.median_change_pct} />, align: 'right' },
          { header: 'Top gainer', cell: r => `${r.top_gainer.symbol} ${signed(r.top_gainer.change_pct, 1)}` },
          { header: 'Top loser', cell: r => `${r.top_loser.symbol} ${signed(r.top_loser.change_pct, 1)}` },
        ]} />}
      </Panel>

      <Panel title="Trading screens" subtitle="Preset filters over the same rows; click one to load it into the screener below.">
        <StateMessage loading={screens.isLoading} error={screens.error} empty="" isEmpty={false} />
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
          {(screens.data ?? []).map((p) => (
            <Chip key={p.id} title={p.description} color={filters.screen === p.id ? 'primary' : BIAS_COLOR[p.bias]}
              variant={filters.screen === p.id ? 'filled' : 'outlined'}
              label={`${p.title} · ${p.count}`} onClick={() => set({ screen: filters.screen === p.id ? '' : p.id })} />
          ))}
        </Box>
        <ScreenTrackRecord outcomes={outcomes.data} />
      </Panel>

      <Panel title="Intraday screener" subtitle={stocks.data ? `${stocks.data.count} matching stocks` : undefined}
        actions={<Button size="small" onClick={() => setFilters(NO_FILTERS)}>Clear all</Button>}>
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1.5, mb: 2 }}>
          <Select label="Price change ≥" value={filters.minChange} onChange={(v) => set({ minChange: v })}
            options={[['', 'Any'], ['1', '+1%'], ['2', '+2%'], ['5', '+5%'], ['-1', '−1% or more']]} />
          <Select label="over" value={filters.window} onChange={(v) => set({ window: v })}
            options={[['5m', 'last 5 min'], ['15m', 'last 15 min'], ['1h', 'last 1 hour'], ['1d', 'today']]} />
          <Select label="RSI" value={filters.rsi} onChange={(v) => set({ rsi: v })}
            options={[['', 'Any'], ['oversold', 'Oversold (≤ 30)'], ['neutral', '30 – 70'], ['overbought', 'Overbought (≥ 70)']]} />
          <Select label="MACD" value={filters.macd} onChange={(v) => set({ macd: v })}
            options={[['', 'Any'], ['bullish', 'Above signal'], ['bearish', 'Below signal'],
              ['positive', 'Above zero (Groww)'], ['negative', 'Below zero']]} />
          <Select label="52W" value={filters.near52w} onChange={(v) => set({ near52w: v })}
            options={[['', 'Any'], ['high', 'Near 52W high'], ['low', 'Near 52W low']]} />
          <Select label="Breakout" value={filters.breakout} onChange={(v) => set({ breakout: v })}
            options={[['', 'Any'], ['r1', 'R1 breakout'], ['near_r1', 'Near R1'], ['r2', 'R2 breakout'], ['near_r2', 'Near R2'], ['r3', 'R3 breakout']]} />
          <Select label="Index" value={filters.index} onChange={(v) => set({ index: v })}
            options={[['', 'All'], ...INDICES.map((i): [string, string] => [i, i])]} />
          <Select label="Sector" value={filters.sector} onChange={(v) => set({ sector: v })}
            options={[['', 'All'], ...(sectors.data?.sectors ?? []).map((x): [string, string] => [x.sector, x.sector])]} />
          <Select label="Sort by" value={filters.sort} onChange={(v) => set({ sort: v })}
            options={Object.entries(SORTS)} />
          <Select label="Order" value={filters.order} onChange={(v) => set({ order: v as 'asc' | 'desc' })}
            options={[['desc', 'High to low'], ['asc', 'Low to high']]} />
        </Box>

        {check.error && <Alert severity="error" sx={{ mb: 2 }}>{check.error.message}</Alert>}
        {check.data && (
          <Alert severity={Object.keys(check.data.changed).length ? 'warning' : 'success'} sx={{ mb: 2 }} onClose={() => check.reset()}>
            <Typography variant="body2" sx={{ fontWeight: 700 }}>
              {check.data.current.symbol} rechecked at bar {new Date(check.data.current.as_of).toLocaleTimeString('en-IN', { hour12: false })}:
              RSI {num(check.data.current.rsi, 1)} (live {num(check.data.current.rsi_live, 1)}), MACD hist {num(check.data.current.macd_hist, 3)},
              R1 {num(check.data.current.r1)} / S1 {num(check.data.current.s1)}
            </Typography>
            {check.data.saved_as_of == null ? 'Not in the saved scan.'
              : Object.keys(check.data.changed).length === 0 ? 'Saved technicals are still current.'
              : `Changed since the saved scan: ${Object.entries(check.data.changed)
                  .map(([k, v]) => `${k} ${String(v.saved)} → ${String(v.now)}`).join(', ')}. Update technicals to refresh every row.`}
          </Alert>
        )}

        <StateMessage loading={stocks.isLoading} error={stocks.error} empty="No stocks match these filters." isEmpty={stocks.data?.count === 0} />
        {stocks.data && stocks.data.rows.length > 0 && <DataTable rows={stocks.data.rows} maxRows={25} maxHeight={640} columns={[
          { header: 'Company', cell: r => <Box>
            <Button size="small" onClick={() => onSelectSymbol(r.symbol)} sx={{ p: 0, minWidth: 0 }}>{r.symbol}</Button>
            <Typography variant="caption" color="text.secondary" component="div">{r.industry ?? 'Unclassified'}</Typography></Box> },
          { header: 'Price', cell: r => <Box title={r.price_source === 'quote' ? 'Live Kite quote' : 'Last stored minute bar'}>
            {inr(r.ltp, 2)}{r.price_source === 'candles' ? '°' : ''}</Box>, align: 'right' },
          { header: '1D', cell: r => <Change value={r.change_pct} />, align: 'right' },
          { header: '5m / 15m / 1h', cell: r => <><Change value={r.change_5m_pct} /> <Change value={r.change_15m_pct} /> <Change value={r.change_1h_pct} /></>, align: 'right' },
          { header: 'Volume', cell: r => r.volume.toLocaleString('en-IN'), align: 'right' },
          { header: 'vs 1W avg', cell: r => <Change value={r.volume_vs_week_pct} />, align: 'right' },
          { header: '52W L–H', cell: r => <Box title={r.full_year ? '' : 'Less than a year of stored sessions'}>
            {num(r.year_low, 1)} – {num(r.year_high, 1)}{r.full_year ? '' : '*'}
            {r.new_52w_high && <Chip size="small" color="success" label="New high" sx={{ ml: 0.5 }} />}
            {r.new_52w_low && <Chip size="small" color="error" label="New low" sx={{ ml: 0.5 }} />}</Box>, align: 'right' },
          { header: 'RSI', cell: r => <Box component="span" sx={{ color: r.rsi == null ? 'text.secondary' : r.rsi >= 70 ? tokens.neg : r.rsi <= 30 ? tokens.pos : 'text.primary' }}>{num(r.rsi, 1)}</Box>, align: 'right' },
          { header: 'MACD', cell: r => r.macd_hist == null ? '—' : <Chip size="small" variant="outlined" color={r.macd_hist > 0 ? 'success' : 'error'} label={r.macd_hist > 0 ? 'Bullish' : 'Bearish'} /> },
          { header: 'Pivots', cell: r => r.r1_breakout ? <Chip size="small" color="success" label="R1 breakout" />
            : r.s1_breakdown ? <Chip size="small" color="error" label="S1 breakdown" />
            : r.near_r1 ? <Chip size="small" variant="outlined" color="success" label="Near R1" /> : '—' },
          { header: 'Turnover', cell: r => `₹${num(r.turnover / 1e7, 1)} Cr`, align: 'right' },
          { header: '', cell: r => <Button size="small" disabled={check.isPending} onClick={() => check.mutate(r.symbol)} title="Recompute this stock's technicals now">Check</Button> },
        ]} />}
      </Panel>
    </Stack>
  )
}
