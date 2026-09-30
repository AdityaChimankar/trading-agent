import { useMemo, useState } from 'react'
import { ROUTES, type Route } from '../lib/router'
import { cx } from '../lib/format'

export const ROUTE_LABELS: Record<Route, string> = {
  dashboard: 'Dashboard',
  history: 'Transactions',
  pipeline: 'Signal Pipeline',
  news: 'News',
}

export default function NavBar({
  route,
  onNavigate,
  inFlight,
  onRefresh,
  symbol,
  symbols,
  onSymbolChange,
  showSymbolPicker,
}: {
  route: Route
  onNavigate: (route: Route) => void
  inFlight: number
  onRefresh: () => void
  symbol: string | null
  symbols: string[]
  onSymbolChange: (symbol: string) => void
  showSymbolPicker: boolean
}) {
  return (
    <header className="topbar">
      <div className="topbar-left">
        <div className="brand">
          <span className="brand-dot" />
          Intraday Trading Agent
        </div>

        <nav className="nav" aria-label="Sections">
          {ROUTES.map((r) => (
            <button
              key={r}
              type="button"
              className={cx('nav-link', route === r && 'is-active')}
              aria-current={route === r ? 'page' : undefined}
              onClick={() => onNavigate(r)}
            >
              {ROUTE_LABELS[r]}
            </button>
          ))}
        </nav>
      </div>

      <div className="topbar-right">
        {showSymbolPicker && (
          <SearchableSymbolSelect
            symbol={symbol}
            symbols={symbols}
            onChange={onSymbolChange}
          />
        )}

        <span className={inFlight > 0 ? 'pulse is-active' : 'pulse'} title="Auto-refreshing">
          {inFlight > 0 ? '⟳ live' : '● idle'}
        </span>

        <button type="button" className="btn" onClick={onRefresh}>
          Refresh
        </button>
      </div>
    </header>
  )
}

function SearchableSymbolSelect({
  symbol,
  symbols,
  onChange,
}: {
  symbol: string | null
  symbols: string[]
  onChange: (symbol: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [search, setSearch] = useState('')

  const filtered = useMemo(() => {
    if (!search) return symbols
    const q = search.toLowerCase()
    return symbols.filter((s) => s.toLowerCase().includes(q))
  }, [symbols, search])

  const selectedLabel = symbol ?? 'Select symbol'

  return (
    <div className="symbol-selector">
      <button
        type="button"
        className="symbol-selector-trigger"
        onClick={() => setOpen(!open)}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span>{selectedLabel}</span>
        <span className="symbol-selector-chevron">{open ? '▴' : '▾'}</span>
      </button>

      {open && (
        <div className="symbol-selector-dropdown">
          <input
            type="text"
            className="symbol-search"
            placeholder="Search symbols..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') setOpen(false)
            }}
            autoFocus
          />
          <ul className="symbol-list" role="listbox">
            {filtered.length === 0 ? (
              <li className="symbol-empty">No matching symbol</li>
            ) : (
              filtered.map((s) => (
                <li key={s}>
                  <button
                    type="button"
                    className={cx('symbol-item', s === symbol && 'is-active')}
                    onClick={() => {
                      onChange(s)
                      setOpen(false)
                      setSearch('')
                    }}
                  >
                    {s}
                  </button>
                </li>
              ))
            )}
          </ul>
        </div>
      )}

      {open && (
        <div
          className="symbol-selector-backdrop"
          onClick={() => setOpen(false)}
          aria-hidden="true"
        />
      )}
    </div>
  )
}
