// Hash routing, on purpose.
//
// The dashboard is served as a static bundle (Vite build) and the backend
// serves the API from a different origin in dev, so a history-API router
// would need a catch-all rewrite on every server that ever hosts it. Hash
// routes work from file://, from the Vite dev server and from any static
// host with no config, and they survive a page reload - which is the whole
// point of having separate pages instead of one long scroll.
//
// A full router library is not worth a dependency for three views, so this
// is deliberately tiny and total: any unknown or missing hash resolves to
// the dashboard rather than a blank screen.
import { useEffect, useState } from 'react'

export const ROUTES = ['dashboard', 'history', 'pipeline'] as const

export type Route = (typeof ROUTES)[number]

function readHash(): Route {
  const raw = window.location.hash.replace(/^#\/?/, '').split('?')[0]
  return (ROUTES as readonly string[]).includes(raw) ? (raw as Route) : 'dashboard'
}

export function useHashRoute(): [Route, (route: Route) => void] {
  const [route, setRoute] = useState<Route>(readHash)

  useEffect(() => {
    const onHashChange = () => setRoute(readHash())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  function navigate(next: Route) {
    // Assigning the same hash fires no event, so a click on the page you are
    // already on would otherwise do nothing at all.
    if (readHash() === next) {
      setRoute(next)
      return
    }
    window.location.hash = `/${next}`
  }

  return [route, navigate]
}
