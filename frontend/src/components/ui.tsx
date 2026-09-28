import type { ReactNode } from 'react'

import { cx } from '../lib/format'

export function Panel({
  title,
  subtitle,
  actions,
  children,
  className,
}: {
  title?: string
  subtitle?: string
  actions?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={cx('panel', className)}>
      {(title || actions) && (
        <div className="panel-head">
          <div>
            {title && <h2 className="panel-title">{title}</h2>}
            {subtitle && <p className="panel-subtitle">{subtitle}</p>}
          </div>
          {actions}
        </div>
      )}
      {children}
    </section>
  )
}

export function Metric({
  label,
  value,
  tone,
  hint,
}: {
  label: string
  value: ReactNode
  tone?: 'pos' | 'neg' | 'muted'
  hint?: string
}) {
  return (
    <div className="metric">
      <div className="metric-label">{label}</div>
      <div className={cx('metric-value', tone && `metric-${tone}`)}>{value}</div>
      {hint && <div className="metric-hint">{hint}</div>}
    </div>
  )
}

export interface Column<T> {
  header: string
  cell: (row: T) => ReactNode
  align?: 'left' | 'right'
}

export function DataTable<T>({
  columns,
  rows,
  empty = 'No rows.',
}: {
  columns: Column<T>[]
  rows: T[]
  empty?: string
}) {
  if (rows.length === 0) return <p className="muted small">{empty}</p>

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.header} style={{ textAlign: c.align ?? 'left' }}>
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {columns.map((c, j) => (
                // Rows have no stable id (they're log entries, newest first),
                // so index-based keys are safe here.
                <td key={j} style={{ textAlign: c.align ?? 'left' }}>
                  {c.cell(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function StateMessage({
  loading,
  error,
  empty,
  isEmpty,
}: {
  loading: boolean
  error: unknown
  empty: string
  isEmpty: boolean
}) {
  if (loading) return <p className="muted small">Loading…</p>
  if (error) return <p className="error small">{error instanceof Error ? error.message : 'Request failed.'}</p>
  if (isEmpty) return <p className="muted small">{empty}</p>
  return null
}
