import { Box, Chip } from '@mui/material'

import { useTradeReplay, type TradeReplayRequest } from '../api/queries'
import type { ReplayOutcomes, TradeReplayResponse } from '../api/types'
import { num } from '../lib/format'
import { DataTable, Metric } from './ui'

const share = (value: number) => `${(value * 100).toFixed(1)}%`
const signed = (value: number) => `${value >= 0 ? '+' : ''}${value.toFixed(3)}`

const STATUS = {
  conclusive_negative: { label: 'Conclusive: negative', color: 'error' },
  conclusive_positive: { label: 'Conclusive: positive', color: 'success' },
  inconclusive: { label: 'Inconclusive', color: 'default' },
} as const

function comparison(doc: TradeReplayResponse): { name: string; row: ReplayOutcomes }[] {
  return [
    { name: 'Replay', row: doc.simulation },
    { name: 'Single bars', row: doc.variants.single_bars },
    { name: 'No vol filter', row: doc.variants.unscaled },
    { name: 'Real sessions', row: doc.history },
  ]
}

// Simulated odds and a conclusion for the plan's bracket over the rest of the
// session. Context only: it never changes the plan, the size or the signal.
export default function TradeReplay({ plan }: { plan: TradeReplayRequest }) {
  const replay = useTradeReplay()
  const doc = replay.data
  const sim = doc?.simulation
  const status = doc ? STATUS[doc.conclusion.status] : null

  return (
    <Box sx={{ display: 'grid', gap: 1, mt: 2, justifyItems: 'start', minWidth: 0, width: '100%' }}>
      <button
        type="button"
        className="btn"
        disabled={replay.isPending}
        onClick={() => replay.mutate(plan)}
      >
        {replay.isPending ? 'Replaying…' : doc ? 'Replay again' : 'Replay this plan'}
      </button>
      {replay.isError && (
        <p className="error small">
          Replay unavailable: {replay.error instanceof Error ? replay.error.message : 'unknown error'}
        </p>
      )}
      {doc && sim && status && (
        <>
          <Chip label={status.label} color={status.color} />
          <p className="small">{doc.conclusion.line}</p>
          <p className="muted small">
            {doc.config.replays.toLocaleString()} replays of {doc.first_bar}–{doc.exit_time} IST from{' '}
            {doc.pool.sessions} sessions ({doc.pool.first} to {doc.pool.last}) on {doc.bar_minutes}-minute bars in
            ~{Math.round(doc.mean_block_minutes)}-minute blocks, seed {doc.config.seed}. Volatility used:{' '}
            {num(doc.volatility.ratio, 2)}× the pool median ({doc.volatility.today_bars} bars from today). Below: the
            same run with single bars, without the volatility filter, and the bracket on the real sessions.
          </p>
          <div className="metric-row">
            <Metric label="Target first" value={share(sim.p_target)} />
            <Metric label="Stop first" value={share(sim.p_stop)} />
            <Metric label="Neither by 15:15" value={share(sim.p_neither)} />
            <Metric
              label="Avg R, net"
              value={signed(sim.avg_r_net)}
              tone={sim.avg_r_net > 0 ? 'pos' : sim.avg_r_net < 0 ? 'neg' : undefined}
              hint={`90%: ${signed(sim.avg_r_net_range[0])} to ${signed(sim.avg_r_net_range[1])}`}
            />
            <Metric
              label="Price at 15:15"
              value={num(sim.close_median, 2)}
              hint={`90%: ${num(sim.close_p5, 2)}–${num(sim.close_p95, 2)}`}
            />
          </div>
          <p className="small">{doc.verdict}</p>
          {doc.notes.map(note => <p key={note} className="small muted">{note}</p>)}
          <Box sx={{ width: '100%', minWidth: 0, overflowX: 'auto' }}>
            <DataTable rows={comparison(doc)} columns={[
              { header: 'Run', cell: r => r.name },
              { header: 'Net R', cell: r => signed(r.row.avg_r_net), align: 'right' },
              { header: 'Target', cell: r => share(r.row.p_target), align: 'right' },
              { header: 'Stop', cell: r => share(r.row.p_stop), align: 'right' },
              { header: 'Neither', cell: r => share(r.row.p_neither), align: 'right' },
            ]} />
          </Box>
          <ul className="small muted" style={{ margin: 0, paddingLeft: '1.2em' }}>
            {doc.conclusion.checks.map(check => (
              <li key={check.name}>{check.passed ? '✓' : '✗'} {check.name}: {check.detail}</li>
            ))}
          </ul>
          <p className="small muted">{doc.limitations}</p>
        </>
      )}
    </Box>
  )
}
