import { useClosePosition, usePositions } from '../api/queries'
import type { PositionStatus } from '../api/types'
import { cx, inr, num, pnlClass, signedInr, timeAgo } from '../lib/format'
import { Metric, Panel, StateMessage } from './ui'

// The exit verdict is not two-valued: a stale feed cannot confirm HOLD, and a
// symbol with no candle cannot be judged at all. Rendering either of those as
// SELL (or as a green P&L) is worse than rendering nothing - one is a false
// exit signal, the other a profit that was never measured.
const VERDICT_LABEL: Record<PositionStatus['recommendation'], string> = {
  HOLD: '🟡 HOLD',
  SELL: '🔴 SELL',
  STALE: '⚪ STALE',
  NO_DATA: '⚪ NO DATA',
}

export default function PositionMonitor() {
  const { data, isLoading, error } = usePositions()
  const closePosition = useClosePosition()

  const positions = data ?? []
  // Totals only over rows that actually have a mark. The monitor returns null
  // (not 0) for the rest, so summing them would silently report a flat position.
  const marked = positions.filter((s) => s.pnl !== null)
  const unmarked = positions.length - marked.length
  const totalPlaced = positions.reduce((sum, s) => sum + s.position.position_value, 0)
  const totalCurrent = marked.reduce((sum, s) => sum + (s.current_value ?? 0), 0)
  const totalPnl = marked.reduce((sum, s) => sum + (s.pnl ?? 0), 0)

  return (
    <Panel title="📊 Position Monitor" subtitle="Paper ledger — nothing here places a real order.">
      {closePosition.isError && (
        <p className="error small">
          Close failed: {closePosition.error instanceof Error ? closePosition.error.message : 'unknown error'}
        </p>
      )}

      <StateMessage
        loading={isLoading}
        error={error}
        empty="No open positions. Size a trade below and record it to track it here."
        isEmpty={positions.length === 0}
      />

      {positions.length > 0 && (
        <>
          <div className="metric-row">
            <Metric label="Placed value" value={inr(totalPlaced)} />
            <Metric label="Current value" value={inr(totalCurrent)} />
            <Metric
              label="Total P&L"
              value={signedInr(totalPnl)}
              tone={totalPnl >= 0 ? 'pos' : 'neg'}
              hint={`${totalPlaced ? ((totalPnl / totalPlaced) * 100).toFixed(2) : '0.00'}%`}
            />
          </div>

          {unmarked > 0 && (
            <p className="muted small">
              {unmarked} position{unmarked > 1 ? 's have' : ' has'} no usable mark right now (stale
              feed, or no candle) — left out of the totals above and flagged below.
            </p>
          )}

          <ul className="position-list">
            {positions.map((status) => {
              const p = status.position
              return (
                <li key={p.id} className="position">
                  <div className="position-head">
                    <span className="position-symbol">
                      {p.symbol} <span className="muted small">({p.action})</span>
                    </span>
                    <button
                      type="button"
                      className="btn btn-danger"
                      disabled={closePosition.isPending}
                      onClick={() => closePosition.mutate(p.id)}
                    >
                      Close
                    </button>
                  </div>

                  <div className="position-grid small">
                    <div>
                      <span className="muted">Placed</span> {inr(p.position_value)} @ {num(p.entry_price, 2)}
                    </div>
                    <div>
                      <span className="muted">Now</span> {inr(status.current_value)} @{' '}
                      {num(status.current_price, 2)}
                    </div>
                    <div className={pnlClass(status.pnl)}>
                      {status.pnl === null
                        ? '⚪ no mark'
                        : `${status.pnl >= 0 ? '🟢' : '🔴'} ${signedInr(status.pnl)} (${num(status.pnl_pct, 2)}%)`}
                    </div>
                    <div className="muted">
                      {p.position_size} sh · stop {num(p.stop_loss, 2)} · target {num(p.take_profit, 2)}
                    </div>
                  </div>

                  <p className={cx('small', status.recommendation === 'SELL' ? 'warn-text' : 'muted')}>
                    {VERDICT_LABEL[status.recommendation]} — {status.reason}
                  </p>
                  <p className="muted small">Opened {timeAgo(p.opened_at)}</p>
                </li>
              )
            })}
          </ul>
        </>
      )}
    </Panel>
  )
}
