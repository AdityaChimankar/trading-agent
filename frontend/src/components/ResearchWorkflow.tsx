export type ResearchStage = 'find' | 'inspect' | 'validate'

const stages: { id: ResearchStage; title: string; description: string }[] = [
  { id: 'find', title: '1. Find opportunities', description: 'Full-universe scan and ranked plans' },
  { id: 'inspect', title: '2. Inspect selected stock', description: 'Chart, decision, risk and news' },
  { id: 'validate', title: '3. Validate strategy', description: 'Walk-forward, outcomes and ML gates' },
]

export default function ResearchWorkflow({ stage, onStageChange, onPaper, symbol, interval, mode, scanStatus, planStatus }: {
  stage: ResearchStage
  onStageChange: (stage: ResearchStage) => void
  onPaper: () => void
  symbol: string; interval: string; mode: string; scanStatus: string; planStatus: string
}) {
  return <section className="workspace-overview" aria-label="Research workflow">
    <div className="workspace-heading">
      <div><p className="workspace-eyebrow">Connected research workspace</p><h1>Discover. Inspect. Validate.</h1></div>
      <span className="workspace-badge">Research &amp; paper only</span>
    </div>
    <nav className="workflow-nav" aria-label="Research steps">
      {stages.map(item => <button className={`workflow-step ${stage === item.id ? 'is-active' : ''}`}
        key={item.id} aria-pressed={stage === item.id} aria-controls={`workspace-${item.id}`}
        onClick={() => onStageChange(item.id)}>
        <strong>{item.title}</strong><span>{item.description}</span>
      </button>)}
      <button className="workflow-step" onClick={onPaper}><strong>4. Paper review</strong><span>Shared selection, manual paper book</span></button>
    </nav>
    <div className="workspace-context" aria-label="Shared research context">
      <span><small>Selected stock</small><strong>{symbol || 'Select a symbol'}</strong></span>
      <span><small>Shared interval</small><strong>{interval}</strong></span>
      <span><small>Decision mode</small><strong>{mode === 'rule_only' ? 'Rules only' : mode === 'ml_only' ? 'ML only' : 'Rules + ML hybrid'}</strong></span>
      <span><small>Universe scan</small><strong>{scanStatus}</strong></span>
      <span><small>Selected plan</small><strong>{planStatus}</strong></span>
    </div>
  </section>
}
