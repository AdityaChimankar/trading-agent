import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert, Box, Button, Chip, Dialog, DialogActions, DialogContent, DialogTitle, Grid, MenuItem, Stack, TextField, Typography,
} from '@mui/material'

import { apiGet, apiPost } from '../api/client'
import { DataTable, Metric, Panel, StateMessage } from './ui'
import { inr } from '../lib/format'

interface ControlsStatus {
  now_ist: string
  kill_switch: { halted: boolean; reason: string | null; since: string | null; by: string | null }
  entry_block_reason: string | null
  entries_today: number
  max_trades_per_day: number
  entry_cutoff: string
  square_off_time: string
  square_off_due: boolean
  execution_mode: 'paper' | 'live'
  ledger: { entries_allowed: boolean; block_reason: string | null; realized_equity?: number; daily_realized_pnl?: number; realized_drawdown_pct?: number }
  limits: Record<string, number | string | boolean>
}
interface OrderRow {
  id: number; client_order_id: string; position_id: number | null; symbol: string; side: string; quantity: number
  purpose: string; mode: string; reference_price: number; fill_price: number | null; status: string
  charges: number; slippage_bps: number; reason: string | null; error: string | null; created_at: string
}
interface Reconciliation { checked_at: string; ok: boolean; broker_checked: boolean; issues: Record<string, string | number | null>[] }
interface AuditEvent { id?: string; at?: string; event: string; [key: string]: unknown }
interface CostPreview {
  entry: Record<string, number>; round_trip_at_same_price: number; breakeven_move_pct: number
}

const STATUS_COLOR: Record<string, 'success' | 'error' | 'warning' | 'default'> = {
  FILLED: 'success', REJECTED: 'error', SUBMITTED: 'warning', PENDING: 'warning',
}
const time = (value?: string | null) => (value ? new Date(value).toLocaleTimeString('en-IN', { hour12: false }) : '—')

export default function RiskExecutionPage() {
  const cache = useQueryClient()
  const status = useQuery({ queryKey: ['controls'], queryFn: () => apiGet<ControlsStatus>('/api/v1/controls/status'), refetchInterval: 15_000 })
  const orders = useQuery({ queryKey: ['orders'], queryFn: () => apiGet<OrderRow[]>('/api/v1/execution/orders?limit=200'), refetchInterval: 15_000 })
  const auditLog = useQuery({ queryKey: ['audit'], queryFn: () => apiGet<AuditEvent[]>('/api/v1/controls/audit?limit=100'), refetchInterval: 30_000 })
  const [haltOpen, setHaltOpen] = useState(false)
  const [squareOpen, setSquareOpen] = useState(false)
  const [reason, setReason] = useState('')
  const refresh = () => ['controls', 'orders', 'audit', 'positions', 'wallet', 'paper-positions'].forEach(key => void cache.invalidateQueries({ queryKey: [key] }))
  const halt = useMutation({ mutationFn: () => apiPost('/api/v1/controls/halt', { reason }), onSuccess: () => { setHaltOpen(false); setReason(''); refresh() } })
  const resume = useMutation({ mutationFn: () => apiPost('/api/v1/controls/resume'), onSuccess: refresh })
  const squareOff = useMutation({ mutationFn: () => apiPost<{ results: { symbol: string; status: string; error?: string }[] }>('/api/v1/controls/square-off'), onSuccess: () => { setSquareOpen(false); refresh() } })
  const reconcile = useMutation({ mutationFn: () => apiGet<Reconciliation>('/api/v1/execution/reconcile') })

  const s = status.data
  const halted = s?.kill_switch.halted

  return (
    <Stack spacing={2}>
      <Panel
        title="Trading controls"
        subtitle="Checked inside the ledger write lock before every entry: manual, router and dashboard."
        actions={s && <Chip color={s.execution_mode === 'live' ? 'error' : 'default'} label={s.execution_mode === 'live' ? 'LIVE ORDERS' : 'PAPER'} />}
      >
        <StateMessage loading={status.isLoading} error={status.error} empty="" isEmpty={false} />
        {s && <>
          <Alert severity={halted ? 'error' : s.entry_block_reason ? 'warning' : 'success'} sx={{ mb: 2 }}
            action={halted
              ? <Button color="inherit" onClick={() => resume.mutate()} disabled={resume.isPending}>Resume trading</Button>
              : <Button color="inherit" onClick={() => setHaltOpen(true)}>Engage kill switch</Button>}>
            {halted ? `Kill switch engaged: ${s.kill_switch.reason ?? ''} (since ${time(s.kill_switch.since)})`
              : s.entry_block_reason ? `New entries blocked: ${s.entry_block_reason}` : 'New entries allowed'}
          </Alert>
          <Grid container spacing={2}>
            <Grid size={{ xs: 6, md: 2 }}><Metric label="Entries today" value={`${s.entries_today} / ${s.max_trades_per_day}`} /></Grid>
            <Grid size={{ xs: 6, md: 2 }}><Metric label="Entry cutoff" value={`${s.entry_cutoff} IST`} /></Grid>
            <Grid size={{ xs: 6, md: 2 }}><Metric label="Square-off" value={`${s.square_off_time} IST`} hint={s.square_off_due ? 'Due now' : 'Scheduled'} /></Grid>
            <Grid size={{ xs: 6, md: 2 }}><Metric label="Realized today" value={inr(s.ledger.daily_realized_pnl)} tone={(s.ledger.daily_realized_pnl ?? 0) >= 0 ? 'pos' : 'neg'} /></Grid>
            <Grid size={{ xs: 6, md: 2 }}><Metric label="Drawdown" value={`${(s.ledger.realized_drawdown_pct ?? 0).toFixed(2)}%`} hint={`limit ${(Number(s.limits.max_drawdown_pct) * 100).toFixed(0)}%`} /></Grid>
            <Grid size={{ xs: 6, md: 2 }}><Metric label="Daily loss limit" value={`${(Number(s.limits.max_daily_loss_pct) * 100).toFixed(1)}%`} /></Grid>
          </Grid>
          {!s.ledger.entries_allowed && <Alert severity="warning" sx={{ mt: 2 }}>Ledger limit: {s.ledger.block_reason}</Alert>}
          <Box sx={{ mt: 2 }}>
            <Button variant="outlined" color="warning" onClick={() => setSquareOpen(true)}>Square off all positions now</Button>
          </Box>
          {squareOff.data && <Alert severity={squareOff.data.results.some(r => r.status !== 'closed') ? 'warning' : 'success'} sx={{ mt: 2 }}>
            {squareOff.data.results.length === 0 ? 'No open positions.' : squareOff.data.results.map(r => `${r.symbol}: ${r.status}${r.error ? ` (${r.error})` : ''}`).join(' · ')}
          </Alert>}
        </>}
      </Panel>

      <Grid container spacing={2}>
        <Grid size={{ xs: 12, lg: 7 }}>
          <Panel title="Orders" subtitle="Every routed order with its lifecycle, fill and modelled charges.">
            <StateMessage loading={orders.isLoading} error={orders.error} empty="No orders yet." isEmpty={(orders.data ?? []).length === 0} />
            {orders.data && orders.data.length > 0 && <DataTable rows={orders.data} maxRows={12} columns={[
              { header: 'Time', cell: o => time(o.created_at) },
              { header: 'Symbol', cell: o => o.symbol },
              { header: 'Side', cell: o => <Chip size="small" label={`${o.side} · ${o.purpose}`} color={o.side === 'BUY' ? 'success' : 'error'} variant="outlined" /> },
              { header: 'Qty', cell: o => o.quantity, align: 'right' },
              { header: 'Ref', cell: o => o.reference_price.toFixed(2), align: 'right' },
              { header: 'Fill', cell: o => o.fill_price?.toFixed(2) ?? '—', align: 'right' },
              { header: 'Charges', cell: o => inr(o.charges, 2), align: 'right' },
              { header: 'Status', cell: o => <Chip size="small" label={o.status} color={STATUS_COLOR[o.status] ?? 'default'} title={o.error ?? undefined} /> },
            ]} />}
          </Panel>
        </Grid>
        <Grid size={{ xs: 12, lg: 5 }}>
          <Panel title="Reconciliation" subtitle="Unconfirmed orders, fill mismatches and (live) broker net quantities."
            actions={<Button variant="contained" onClick={() => reconcile.mutate()} disabled={reconcile.isPending}>Run check</Button>}>
            {reconcile.error && <Alert severity="error">{(reconcile.error as Error).message}</Alert>}
            {!reconcile.data && !reconcile.error && <Typography variant="body2" color="text.secondary">Not run in this session.</Typography>}
            {reconcile.data && (reconcile.data.ok
              ? <Alert severity="success">In sync at {time(reconcile.data.checked_at)}{reconcile.data.broker_checked ? ' (broker checked)' : ' (paper: local book and orders)'}</Alert>
              : <Stack spacing={1}>{reconcile.data.issues.map((issue, i) => <Alert key={i} severity="warning">
                  {String(issue.kind).replaceAll('_', ' ')}: {Object.entries(issue).filter(([k]) => k !== 'kind').map(([k, v]) => `${k} ${v}`).join(', ')}
                </Alert>)}</Stack>)}
          </Panel>
          <CostCalculator />
        </Grid>
      </Grid>

      <Panel title="Audit trail" subtitle="Append-only record of today's entries, exits, fills, rejections and halts.">
        <StateMessage loading={auditLog.isLoading} error={auditLog.error} empty="No events today." isEmpty={(auditLog.data ?? []).length === 0} />
        {auditLog.data && auditLog.data.length > 0 && <DataTable rows={auditLog.data} maxRows={15} columns={[
          { header: 'Time', cell: e => time(e.at) },
          { header: 'Event', cell: e => e.event.replaceAll('_', ' ') },
          { header: 'Details', cell: e => Object.entries(e).filter(([k]) => !['id', 'at', 'event', 'results'].includes(k)).map(([k, v]) => `${k}=${typeof v === 'number' ? Number(v.toFixed(2)) : v}`).join(' · ') },
        ]} />}
      </Panel>

      <Dialog open={haltOpen} onClose={() => setHaltOpen(false)}>
        <DialogTitle>Engage kill switch</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>Blocks every new entry until resumed. Open positions are not closed.</Typography>
          <TextField autoFocus fullWidth label="Reason" value={reason} onChange={e => setReason(e.target.value)} />
          {halt.error && <Alert severity="error" sx={{ mt: 2 }}>{(halt.error as Error).message}</Alert>}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setHaltOpen(false)}>Cancel</Button>
          <Button color="error" variant="contained" disabled={reason.trim().length < 3 || halt.isPending} onClick={() => halt.mutate()}>Halt entries</Button>
        </DialogActions>
      </Dialog>
      <Dialog open={squareOpen} onClose={() => setSquareOpen(false)}>
        <DialogTitle>Square off every open position?</DialogTitle>
        <DialogContent>
          <Typography variant="body2">Each position is closed at its latest stored price through the order router{s?.execution_mode === 'live' ? ', which sends real MARKET orders' : ''}. Positions without a price stay open and are reported.</Typography>
          {squareOff.error && <Alert severity="error" sx={{ mt: 2 }}>{(squareOff.error as Error).message}</Alert>}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setSquareOpen(false)}>Cancel</Button>
          <Button color="warning" variant="contained" disabled={squareOff.isPending} onClick={() => squareOff.mutate()}>Square off</Button>
        </DialogActions>
      </Dialog>
    </Stack>
  )
}

function CostCalculator() {
  const [price, setPrice] = useState('1000')
  const [quantity, setQuantity] = useState('100')
  const [side, setSide] = useState('BUY')
  const valid = Number(price) > 0 && Number.isInteger(Number(quantity)) && Number(quantity) > 0
  const preview = useQuery({
    queryKey: ['costs', price, quantity, side],
    queryFn: () => apiGet<CostPreview>(`/api/v1/execution/costs?price=${price}&quantity=${quantity}&side=${side}`),
    enabled: valid,
  })
  const entry = preview.data?.entry
  return (
    <Panel title="Charges calculator" subtitle="Zerodha intraday equity schedule: brokerage, STT, exchange, SEBI, stamp and GST.">
      <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
        <TextField label="Price" type="number" value={price} onChange={e => setPrice(e.target.value)} />
        <TextField label="Qty" type="number" value={quantity} onChange={e => setQuantity(e.target.value)} />
        <TextField select label="Side" value={side} onChange={e => setSide(e.target.value)} sx={{ minWidth: 90 }}>
          <MenuItem value="BUY">BUY</MenuItem><MenuItem value="SELL">SELL</MenuItem>
        </TextField>
      </Stack>
      {entry && preview.data && <Grid container spacing={1.5}>
        <Grid size={4}><Metric label="This order" value={inr(entry.total, 2)} hint={`turnover ${inr(entry.turnover)}`} /></Grid>
        <Grid size={4}><Metric label="Round trip" value={inr(preview.data.round_trip_at_same_price, 2)} /></Grid>
        <Grid size={4}><Metric label="Breakeven move" value={`${preview.data.breakeven_move_pct.toFixed(3)}%`} /></Grid>
        <Grid size={12}><Typography variant="caption" color="text.secondary">
          brokerage {entry.brokerage.toFixed(2)} · STT {entry.stt.toFixed(2)} · exchange {entry.exchange.toFixed(2)} · SEBI {entry.sebi.toFixed(2)} · stamp {entry.stamp.toFixed(2)} · GST {entry.gst.toFixed(2)}
        </Typography></Grid>
      </Grid>}
    </Panel>
  )
}
