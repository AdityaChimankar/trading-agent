// plotly.js-dist-min is the prebuilt Plotly bundle and ships no type
// definitions. Only the entry points this app actually calls are declared,
// so we avoid pulling in the full @types/plotly.js surface.
declare module 'plotly.js-dist-min' {
  export function react(
    el: HTMLElement,
    data: unknown[],
    layout?: Record<string, unknown>,
    config?: Record<string, unknown>,
  ): Promise<unknown>
  export function purge(el: HTMLElement): void

  const Plotly: {
    react: typeof react
    purge: typeof purge
  }
  export default Plotly
}
