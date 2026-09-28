const EMPTY = '—'

export function inr(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY
  return value.toLocaleString('en-IN', {
    style: 'currency',
    currency: 'INR',
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

export function num(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY
  return value.toFixed(digits)
}

export function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY
  return `${value.toFixed(digits)}%`
}

export function signedInr(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY
  const sign = value > 0 ? '+' : ''
  return `${sign}${inr(value, digits)}`
}

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}

export function actionClass(action: string | null | undefined): string {
  if (action === 'BUY') return 'action action-buy'
  if (action === 'SELL') return 'action action-sell'
  return 'action action-hold'
}

export function pnlClass(value: number | null | undefined): string {
  if (value === null || value === undefined) return 'mono'
  return value >= 0 ? 'mono pnl-pos' : 'mono pnl-neg'
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return EMPTY
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return iso
  const seconds = Math.round((Date.now() - then) / 1000)
  if (seconds < 60) return `${Math.max(seconds, 0)}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

export function shortTime(iso: string | null | undefined): string {
  if (!iso) return EMPTY
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString('en-IN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
