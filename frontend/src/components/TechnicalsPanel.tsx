import { Box, Chip, Grid, Typography } from '@mui/material'

import { useTechnicals } from '../api/queries'
import type { PivotLevels } from '../api/types'
import { num } from '../lib/format'
import { DataTable, Metric, Panel, StateMessage } from './ui'

const VERDICT_COLOR = { Bullish: 'success', Bearish: 'error', Neutral: 'default' } as const
const verdictTone = (v?: string) => (v?.includes('bullish') ? 'success' : v?.includes('bearish') ? 'error' : 'default')

const PIVOT_ROWS: (keyof PivotLevels)[] = ['r3', 'r2', 'r1', 'pivot', 's1', 's2', 's3']

export default function TechnicalsPanel({ symbol }: { symbol: string }) {
  const technicals = useTechnicals(symbol)
  const t = technicals.data
  const p = t?.pivots

  return (
    <Panel
      title="Technical summary"
      subtitle="Equal-weighted indicator vote and floor pivots from the previous session. Context only: it never overrides the rule signal."
      actions={t?.verdict ? <Chip label={t.verdict} color={verdictTone(t.verdict.toLowerCase())} /> : undefined}
    >
      <StateMessage loading={technicals.isLoading} error={technicals.error} empty="Not enough completed candles for a technical summary."
        isEmpty={Boolean(t?.insufficient_data)} />
      {t?.readings && (
        <Grid container spacing={2}>
          <Grid size={{ xs: 12, md: 7 }}>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              {t.bullish_count} bullish · {t.neutral_count} neutral · {t.bearish_count} bearish of {t.total}
            </Typography>
            <DataTable rows={t.readings} maxRows={12} columns={[
              { header: 'Indicator', cell: r => r.name },
              { header: 'Value', cell: r => num(r.value, 2), align: 'right' },
              { header: 'Verdict', cell: r => <Chip size="small" variant="outlined" label={r.verdict} color={VERDICT_COLOR[r.verdict]} /> },
            ]} />
          </Grid>
          <Grid size={{ xs: 12, md: 5 }}>
            {p ? (
              <Box>
                <Box sx={{ display: 'flex', gap: 2, mb: 1, flexWrap: 'wrap' }}>
                  <Metric label="Nearest resistance" value={num(p.nearest_resistance, 2)} />
                  <Metric label="Nearest support" value={num(p.nearest_support, 2)} />
                </Box>
                <DataTable rows={PIVOT_ROWS} columns={[
                  { header: 'Level', cell: k => k.toUpperCase() },
                  { header: 'Price', cell: k => num(p[k] as number, 2), align: 'right' },
                ]} />
                <Typography variant="caption" color="text.secondary">
                  From {p.based_on}: high {num(p.prev_high, 2)}, low {num(p.prev_low, 2)}, close {num(p.prev_close, 2)}
                </Typography>
              </Box>
            ) : (
              <Typography variant="body2" color="text.secondary">Pivots need a completed previous session in stored candles.</Typography>
            )}
          </Grid>
        </Grid>
      )}
    </Panel>
  )
}
