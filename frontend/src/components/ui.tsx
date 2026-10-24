// Shared building blocks, on Material UI. Every page composes these, so the
// Material look reaches the whole app through this one file.
import { useState, type ReactNode } from 'react'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Collapse,
  IconButton,
  LinearProgress,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from '@mui/material'

import { tokens } from '../theme'

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
    <Card component="section" className={className}>
      <CardContent sx={{ '&:last-child': { pb: 2 } }}>
        {(title || actions || collapsible) && (
          <Box sx={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 2, mb: 1.5 }}>
            <Box sx={{ minWidth: 0 }}>
              {title && <Typography variant="h2" component="h2">{title}</Typography>}
              {subtitle && <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>{subtitle}</Typography>}
            </Box>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexShrink: 0 }}>
              {actions}
              {collapsible && (
                <IconButton
                  size="small"
                  aria-label={collapsed ? 'Expand' : 'Collapse'}
                  title={collapsed ? 'Expand' : 'Collapse'}
                  onClick={() => setCollapsed(!collapsed)}
                >
                  {collapsed ? '▸' : '▾'}
                </IconButton>
              )}
            </Box>
          </Box>
        )}
        {collapsible ? <Collapse in={!collapsed}>{children}</Collapse> : children}
      </CardContent>
    </Card>
  )
}

const TONE = { pos: tokens.pos, neg: tokens.neg, muted: tokens.textDim } as const

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
    <Box className="metric" sx={{ minWidth: 0 }}>
      <Typography variant="overline" color="text.secondary" component="div">{label}</Typography>
      <Typography
        component="div"
        sx={{ fontFamily: tokens.mono, fontWeight: 700, fontSize: '1.05rem', color: tone ? TONE[tone] : 'text.primary' }}
      >
        {value}
      </Typography>
      {hint && <Typography variant="caption" color="text.secondary" component="div">{hint}</Typography>}
    </Box>
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
  // Hooks before any early return: the row count changes between renders.
  const [expanded, setExpanded] = useState(false)
  if (rows.length === 0) return <Typography variant="body2" color="text.secondary">{empty}</Typography>

  const hasMore = rows.length > maxRows
  const displayRows = expanded || !hasMore ? rows : rows.slice(0, maxRows)

  return (
    <Box>
      <TableContainer sx={{ maxHeight, border: 1, borderColor: 'divider', borderRadius: 1 }}>
        <Table size="small" stickyHeader={Boolean(maxHeight)}>
          <TableHead>
            <TableRow>
              {columns.map((c) => (
                <TableCell key={c.header} align={c.align ?? 'left'} sx={{ bgcolor: tokens.elev }}>
                  {c.header}
                </TableCell>
              ))}
            </TableRow>
          </TableHead>
          <TableBody>
            {displayRows.map((row, i) => (
              <TableRow key={i} hover>
                {columns.map((c, j) => (
                  <TableCell key={j} align={c.align ?? 'left'}>{c.cell(row)}</TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
      {hasMore && (
        <Box sx={{ textAlign: 'center', mt: 1 }}>
          <Button variant="text" onClick={() => setExpanded(!expanded)}>
            {expanded ? `Show less (${rows.length})` : `Show ${maxRows} of ${rows.length}`}
          </Button>
        </Box>
      )}
    </Box>
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
  if (loading) return <LinearProgress sx={{ my: 1 }} aria-label="Loading" />
  if (error) return <Alert severity="error" variant="outlined">{error instanceof Error ? error.message : 'Request failed.'}</Alert>
  if (isEmpty) return <Typography variant="body2" color="text.secondary">{empty}</Typography>
  return null
}
