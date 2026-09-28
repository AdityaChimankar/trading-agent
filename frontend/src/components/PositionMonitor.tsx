import { useClosePosition, usePositions } from '../api/queries'
import { cx, inr, num, pnlClass, signedInr, timeAgo } from '../lib/format'
import { Metric, Panel, StateMessage } from './ui'

export default function PositionMonitor() {
  const { data, isLoading, error } = usePositions()
  const closePosition = useClosePosition()

  const positions = data ?? []
  const totalPlaced = positions.reduce((sum, s) => sum + s.position.position_value, 0)
  const totalCurrent = positions.reduce((sum, s) => sum + s.current_value, 0)
  const totalPnl = positions.reduce((sum, s) => sum + s.pnl, 0)

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
                      {status.pnl >= 0 ? '🟢' : '🔴'} {signedInr(status.pnl)} ({num(status.pnl_pct, 2)}%)
                    </div>
                    <div className="muted">
                      {p.position_size} sh · stop {num(p.stop_loss, 2)} · target {num(p.take_profit, 2)}
                    </div>
                  </div>

                  <p className={cx('small', status.recommendation === 'HOLD' ? 'muted' : 'warn-text')}>
                    {status.recommendation === 'HOLD' ? '🟡 HOLD' : '🔴 SELL'} — {status.reason}
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
