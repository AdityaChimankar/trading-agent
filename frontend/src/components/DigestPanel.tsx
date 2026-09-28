import { useDigest } from '../api/queries'
import { Panel } from './ui'

export default function DigestPanel() {
  const { data, isLoading, error } = useDigest()

  // Nothing to show until scheduler.py has generated the day's digest.
  if (isLoading || error || !data?.summary) return null

  return (
    <Panel title="📋 Today's Digest" subtitle={`Generated ${data.generated_at ?? '—'} for ${data.date}`}>
      <div className="digest">{data.summary}</div>
    </Panel>
  )
}
