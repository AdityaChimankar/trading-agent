import { ROUTES, type Route } from '../lib/router'
import { cx } from '../lib/format'

export const ROUTE_LABELS: Record<Route, string> = {
  dashboard: 'Dashboard',
  history: 'Transactions',
  pipeline: 'Signal Pipeline',
}

// The header IS the navigation bar - one sticky bar rather than a nav row
// stacked above it, so the sidebar's `top` offset and the page's scroll
// origin stay a single number instead of two that can drift apart.
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
          <label className="field inline">
            <span>Symbol</span>
            <select value={symbol ?? ''} onChange={(e) => onSymbolChange(e.target.value)}>
              {symbols.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
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
