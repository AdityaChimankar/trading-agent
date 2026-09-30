import { useState, type ReactNode } from 'react'

import { cx } from '../lib/format'

export function Panel({
  title,
  subtitle,
  actions,
  children,
  className,
  collapsible = false,
  defaultCollapsed = false,
}: {
  title?: string
  subtitle?: string
  actions?: ReactNode
  children: ReactNode
  className?: string
  collapsible?: boolean
  defaultCollapsed?: boolean
}) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed)

  return (
    <section className={cx('panel', 'panel-symmetric', className)}>
      {(title || actions || collapsible) && (
        <div className="panel-head">
          <div>
            {title && <h2 className="panel-title">{title}</h2>}
            {subtitle && <p className="panel-subtitle">{subtitle}</p>}
          </div>
          <div className="panel-head-actions">
            {actions}
            {collapsible && (
              <button
                type="button"
                className="collapse-btn"
                aria-label={collapsed ? 'Expand' : 'Collapse'}
                title={collapsed ? 'Expand' : 'Collapse'}
                onClick={(e) => {
                  e.stopPropagation()
                  setCollapsed(!collapsed)
                }}
              >
                {collapsed ? '▸' : '▾'}
              </button>
            )}
          </div>
        </div>
      )}
      <PanelContent collapsed={collapsed} collapsible={collapsible} onToggle={setCollapsed}>
        {children}
      </PanelContent>
    </section>
  )
}

function PanelContent({
  children,
  collapsed,
  collapsible,
  onToggle,
}: {
  children: ReactNode
  collapsed: boolean
  collapsible: boolean
  onToggle: (value: boolean) => void
}) {
  if (!collapsible) return <>{children}</>

  return (
    <>
      <div className={cx('panel-collapse-handle', collapsed && 'is-collapsed')}>
        <button
          type="button"
          className="collapse-btn collapse-btn-handle"
          aria-label={collapsed ? 'Expand' : 'Collapse'}
          onClick={() => onToggle(!collapsed)}
        />
      </div>
      <div className={cx('panel-content', collapsed && 'is-collapsed')}>
        {children}
      </div>
    </>
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
  maxRows = 10,
  maxHeight,
}: {
  columns: Column<T>[]
  rows: T[]
  empty?: string
  maxRows?: number
  maxHeight?: number
}) {
  if (rows.length === 0) return <p className="muted small">{empty}</p>

  const [expanded, setExpanded] = useState(false)
  const showAll = expanded || rows.length <= maxRows
  const displayRows = showAll ? rows : rows.slice(0, maxRows)
  const hasMore = rows.length > maxRows

  const wrapperStyle: React.CSSProperties = maxHeight ? { maxHeight, overflowY: 'auto' } : {}

  return (
    <div className="table-wrap" style={wrapperStyle}>
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
          {displayRows.map((row, i) => (
            <tr key={i}>
              {columns.map((c, j) => (
                <td key={j} style={{ textAlign: c.align ?? 'left' }}>
                  {c.cell(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {hasMore && (
        <div className="table-expand">
          <button
            type="button"
            className="btn btn-link"
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? `Show less (${rows.length})` : `Show ${maxRows} of ${rows.length}`}
          </button>
        </div>
      )}
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