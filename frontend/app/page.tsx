const entries = [
  { href: "/staff/email", title: "Email Assistant", description: "Staff email review queue and assistant.", access: "Staff access" },
  { href: "/staff/support", title: "Support Console", description: "Staff support and human-handoff console.", access: "Staff access" },
  { href: "/staff/admin", title: "Admin Dashboard", description: "Administrator operations dashboard.", access: "Administrator" },
  { href: "/staff/admin/authorization", title: "Authorization Management", description: "Connector and application-access management.", access: "Administrator" },
] as const;

export default function HomePage() {
  const livenessUrl = process.env.NEXT_PUBLIC_LIVENESS_URL ?? "http://localhost/health/live";
  return (
    <main className="home-shell">
      <header className="site-header"><span className="brand-mark" aria-hidden="true">E</span><span className="brand-name">Enterprise AI</span><span className="header-status">Grounded assistance · Human controlled</span><form action="/api/v1/auth/login" method="get"><button className="login-button" type="submit">Sign in</button></form></header>
      <section className="hero" aria-labelledby="home-title"><p className="eyebrow">Enterprise AI agent platform</p><h1 id="home-title">Enterprise AI Support</h1><p className="hero-copy">Bring trusted knowledge, assisted email workflows, and human handoff into one secure operations workspace.</p></section>
      <section className="feature-grid" aria-label="Platform capabilities">
        <article><span>01</span><h2>Knowledge assistance</h2><p>Ground answers in authorized internal sources with reviewable evidence.</p></article>
        <article><span>02</span><h2>Email workflows</h2><p>Classify and draft replies while preserving staff approval before delivery.</p></article>
        <article><span>03</span><h2>Human handoff</h2><p>Move customer conversations to accountable staff without losing context.</p></article>
      </section>
      <section className="workflow" aria-labelledby="workflow-title"><div><p className="eyebrow">How it works</p><h2 id="workflow-title">Evidence first. People in control.</h2></div><ol><li>Connect approved knowledge</li><li>Assist operational work</li><li>Review and act safely</li></ol></section>
      <section className="entry-section" aria-labelledby="entry-title">
        <div className="section-heading"><p className="eyebrow">Workspace</p><h2 id="entry-title">Application access</h2></div>
        <div className="entry-grid">{entries.map((entry) => <a className="entry-card" href={entry.href} key={entry.href}><span className="access-badge">{entry.access}</span><strong>{entry.title}</strong><span>{entry.description}</span><span className="entry-action">Open workspace <span aria-hidden="true">→</span></span></a>)}</div>
      </section>
      <section className="health-section" aria-labelledby="health-title"><div><p className="eyebrow">System status</p><h2 id="health-title">Health checks</h2></div><div className="health-links"><a href={livenessUrl}><strong>Liveness</strong><span>Public process-liveness endpoint.</span></a><a href="/health/ready"><strong>Readiness</strong><span>Public dependency-readiness endpoint.</span></a></div></section>
    </main>
  );
}
