// Material UI theme built from the dashboard's existing palette (styles.css
// :root tokens), so MUI components and the remaining hand-styled markup read
// as one system. Dark only: the trading desk runs on a dark terminal.
import { createTheme } from '@mui/material/styles'

export const tokens = {
  bg: '#0b0f14',
  panel: '#0f141a',
  elev: '#151c25',
  border: '#1e2733',
  text: '#c9d4e0',
  textDim: '#8296ab',
  accent: '#3b82f6',
  pos: '#22c55e',
  neg: '#ef4444',
  warn: '#f59e0b',
  mono: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
}

export const theme = createTheme({
  palette: {
    mode: 'dark',
    primary: { main: tokens.accent },
    success: { main: tokens.pos },
    error: { main: tokens.neg },
    warning: { main: tokens.warn },
    background: { default: tokens.bg, paper: tokens.panel },
    text: { primary: tokens.text, secondary: tokens.textDim },
    divider: tokens.border,
  },
  shape: { borderRadius: 10 },
  typography: {
    fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    fontSize: 13,
    button: { textTransform: 'none', fontWeight: 600 },
    h2: { fontSize: '1rem', fontWeight: 600 },
    overline: { fontSize: '0.68rem', letterSpacing: '0.06em', lineHeight: 1.6 },
  },
  components: {
    MuiPaper: { styleOverrides: { root: { backgroundImage: 'none' } } },
    MuiCard: {
      defaultProps: { variant: 'outlined' },
      styleOverrides: { root: { borderColor: tokens.border, backgroundColor: tokens.panel } },
    },
    MuiAppBar: {
      defaultProps: { elevation: 0, color: 'inherit' },
      styleOverrides: { root: { backgroundColor: tokens.panel, borderBottom: `1px solid ${tokens.border}` } },
    },
    MuiTab: { styleOverrides: { root: { minHeight: 40, paddingInline: 14 } } },
    MuiTabs: { styleOverrides: { root: { minHeight: 40 } } },
    MuiTableCell: {
      styleOverrides: {
        root: { borderColor: tokens.border, paddingBlock: 6 },
        head: { color: tokens.textDim, fontWeight: 600, fontSize: '0.72rem', textTransform: 'uppercase' },
      },
    },
    MuiButton: { defaultProps: { disableElevation: true, size: 'small' } },
    MuiTextField: { defaultProps: { size: 'small' } },
  },
})
