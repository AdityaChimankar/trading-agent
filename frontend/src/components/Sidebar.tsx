import { useMemo, useState } from 'react'

import { useRankings } from '../api/queries'
import type { RankedEntry } from '../api/types'
import { cx, inr, num } from '../lib/format'
import { StateMessage } from './ui'

export default function Sidebar({
  capital,
  onCapitalChange,
  selected,
  onSelect,
  watchlistSymbols,
}: {
  capital: number
  onCapitalChange: (value: number) => void
  selected: string | null
  onSelect: (symbol: string) => void
  watchlistSymbols: string[]
}) {
  const [search, setSearch] = useState('')
  const [view, setView] = useState<'Bullish' | 'Bearish'>('Bullish')
  const [capitalText, setCapitalText] = useState(String(capital))
  const { data, isLoading, error } = useRankings(capital)

  // Local text state so the field can be cleared/retyped. Binding the input
  // straight to the numeric `capital` makes clearing it snap back to the
  // last valid number, because an empty string parses to 0.
  const scoredBySymbol = useMemo(() => {
    const map = new Map<string, RankedEntry>()
    for (const entry of data?.scored ?? []) map.set(entry.symbol, entry)
    return map
  }, [data])

  // Symbol search within the watchlist
  const filteredSymbols = useMemo(() => {
    if (!search) return watchlistSymbols
    const q = search.toLowerCase()
    return watchlistSymbols.filter((s) => s.toLowerCase().includes(q))
  }, [watchlistSymbols, search])

  const ranked: RankedEntry[] = view === 'Bullish' ? (data?.bullish ?? []) : (data?.bearish ?? [])
  const scoreKey = view === 'Bullish' ? 'bullish_score' : 'bearish_score'

  return (
    <aside className="sidebar">
      <div className="side-block">
        <div className="panel-head-symmetric">
          <h2 className="panel-title">Watchlist Bias</h2>
        </div>
        <label className="field">
          <span>Available capital (₹)</span>
          <input
            type="number"
            min={1000}
            step={1000}
            value={capitalText}
            onChange={(e) => {
              setCapitalText(e.target.value)
              const parsed = Number(e.target.value)
              if (Number.isFinite(parsed) && parsed > 0) onCapitalChange(parsed)
            }}
          />
        </label>
        <p className="hint small">
          Sizes every suggested BUY/SELL on this page. Risk 1% of capital per trade, stop at 1.5×ATR.
        </p>
      </div>

      <div className="side-block">
        <div className="panel-head-symmetric">
          <h3 className="panel-title-small">Symbol Search</h3>
        </div>
        <input
          type="text"
          className="symbol-search sidebar-search"
          placeholder="Search symbols..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <ul className="symbol-quick-list">
          {filteredSymbols.slice(0, 6).map((s) => (
            <li key={s}>
              <button
                type="button"
                className={cx('symbol-quick-item', selected === s && 'is-active')}
                onClick={() => onSelect(s)}
              >
                {s}
              </button>
            </li>
          ))}
          {filteredSymbols.length === 0 && search && (
            <li className="muted small">No matching symbol</li>
          )}
        </ul>
      </div>

      {data && data.contradictions.length > 0 && (
        <div className="side-block">
          <h3 className="warn-title">⚠ {data.contradictions.length} bias contradiction(s)</h3>
          {data.contradictions.map((c) => (
            <p key={c.symbol} className="warn-text small">
              <strong>{c.symbol}</strong>: {c.contradiction}
            </p>
          ))}
        </div>
      )}

      <div className="side-block">
        <div className="segmented">
          {(['Bullish', 'Bearish'] as const).map((option) => (
            <button
              key={option}
              type="button"
              className={cx('segmented-btn', view === option && 'is-active')}
              onClick={() => setView(option)}
            >
              {option}
            </button>
          ))}
        </div>

        <StateMessage
          loading={isLoading}
          error={error}
          empty={`No ${view.toLowerCase()} candidates right now.`}
          isEmpty={ranked.length === 0}
        />

        <ul className="rank-list">
          {ranked.map((entry) => {
            const contradiction = scoredBySymbol.get(entry.symbol)?.contradiction
            return (
              <li key={entry.symbol} className={cx('rank-item', selected === entry.symbol && 'is-selected')}>
                <button type="button" className="rank-btn" onClick={() => onSelect(entry.symbol)}>
                  <span className="rank-line">
                    <span className="rank-symbol">
                      {entry.has_open_position && <span title="Open position">📌 </span>}
                      {contradiction && <span title="Bias contradiction">⚠️ </span>}
                      {entry.symbol}
                    </span>
                    <span className="rank-score">{num(entry[scoreKey], 1)}</span>
                  </span>
                  <span className="rank-meta small">
                    RSI {num(entry.rsi, 1)} · trend {entry.trend} · {entry.pattern_bias}
                  </span>
                </button>

                {contradiction && <p className="warn-text small">⚠️ {contradiction}</p>}

                {entry.blocked ? (
                  <p className="muted small">🚫 {entry.action} blocked — {entry.block_reason}</p>
                ) : entry.suggested_size && entry.suggested_size > 0 ? (
                  <p className="muted small">
                    Suggested {entry.action}: {entry.suggested_size} sh @ {num(entry.entry_price, 2)} (
                    {inr(entry.suggested_value)}) · stop {num(entry.stop_loss, 2)} · target{' '}
                    {num(entry.take_profit, 2)}
                  </p>
                ) : (
                  <p className="muted small">Not enough data to size this trade yet.</p>
                )}
              </li>
            )
          })}
        </ul>
      </div>
    </aside>
  )
}
