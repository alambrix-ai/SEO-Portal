/**
 * Dashboard - four KPI tiles, what is actually running, and the approval queue.
 *
 * "What is actually running" rather than "the fleet": the panels list the
 * agents that are working and the systems that are connected. A workspace
 * where nothing has been started shows an empty state that says so and points
 * at the screen that fixes it, instead of six paused cards that look like the
 * product is broken.
 */
import { Link } from 'react-router-dom'

import { api } from '@/api/client'
import type { AgentOut } from '@/api/types'
import { useAuth } from '@/auth/AuthContext'
import { AgentIcon } from '@/components/AgentIcon'
import { ConnectorIcon } from '@/components/ConnectorIcon'
import { useResource } from '@/hooks/useResource'
import {
  Blueprint,
  ErrorState,
  Loading,
  SectionHeading,
  StatTile,
  Tag,
  statusTone,
} from '@/components/ui'

/**
 * One agent on the dashboard.
 *
 * Extracted because two panels render it now - working, and configured but
 * stopped - and a second copy would drift.
 *
 * It shows the last run's outcome, which the dashboard never did: a card
 * saying only "Paused" hid the fact that this agent had synced pages or
 * opened issues, which is the part somebody opening a dashboard wants.
 */
function AgentCard({ agent }: { agent: AgentOut }) {
  return (
    <Blueprint className="card elev-sm">
      <div className="card-head">
        <AgentIcon slug={agent.slug} size={28} />
        <div className="card-head-text">
          <div className="card-title" style={{ fontSize: 15 }}>
            {agent.name}
          </div>
        </div>
      </div>
      <div className="row">
        <Tag tone={statusTone(agent.status)}>{agent.status_label}</Tag>
        <Tag tone="outline">{agent.autonomy_label}</Tag>
      </div>
      {agent.busy ? (
        <div className="card-meta">{agent.step || 'Working'}</div>
      ) : (
        <>
          {agent.metric_label ? (
            <div className="card-meta">{agent.metric_label}</div>
          ) : null}
          {agent.last_run_summary ? (
            <div className="agent-last-run">
              {agent.last_run_status === 'skipped'
                ? 'Last run did not proceed: '
                : 'Last run: '}
              {agent.last_run_summary}
            </div>
          ) : null}
        </>
      )}
    </Blueprint>
  )
}

export function DashboardPage() {
  const { canView, session } = useAuth()
  // Same reason as the agents page: the header's autonomy switch changes what
  // this screen is showing.
  const globalAutonomy = session?.organization.global_autonomy
  const { data, loading, error, reload } = useResource(
    (signal) => api.dashboard(signal),
    [globalAutonomy],
    'dashboard',
  )

  if (loading && !data) return <Loading label="Loading your workspace…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  const { kpis, agents, connectors, approvals, active_agents, ready, ready_agents } =
    data
  // How many of the running agents act without asking. The dashboard's job is
  // to answer that in one line rather than leave it to be counted off tags.
  const autonomousCount = agents.filter((agent) => agent.autonomy).length

  return (
    <>
      <div className="grid-tiles">
        <StatTile
          kicker="Organic Sessions"
          value={kpis.organic_sessions}
          meta="30-day rolling"
        />
        <StatTile
          kicker="Agents Running"
          value={`${kpis.agents_running} / ${kpis.agents_total}`}
          meta="Autonomous fleet"
        />
        <StatTile
          kicker="Connected Systems"
          value={`${kpis.connectors_connected} / ${kpis.connectors_total}`}
          meta="CMS, ads, analytics, CRM"
        />
        <StatTile
          kicker="Pending Approvals"
          value={kpis.pending_approvals}
          meta="Human-in-the-loop queue"
        />
      </div>

      <div className="split-2-1">
        <div>
          <SectionHeading
            title={agents.length > 0 ? 'Running agents' : 'Your agents'}
            /* The autonomy split, said once here rather than read off twelve
               tags. It is the answer to "how much of this is acting on its
               own?", which is the thing somebody wants from a dashboard  - 
               and it used to require counting cards. */
            description={
              agents.length > 0
                ? [
                    active_agents > agents.length
                      ? `Showing ${agents.length} of ${active_agents}`
                      : `${agents.length} running`,
                    `${autonomousCount} autonomous`,
                    `${agents.length - autonomousCount} awaiting review`,
                  ].join(' · ')
                : ready.length > 0
                  ? `${ready_agents} configured, none running`
                  : undefined
            }
            action={
              canView('agents') ? (
                <Link to="/agents" className="btn btn-ghost">
                  View all agents →
                </Link>
              ) : undefined
            }
          />
          {agents.length > 0 ? (
            <div className="grid-cards-sm">
              {agents.map((agent) => (
                <AgentCard key={agent.slug} agent={agent} />
              ))}
            </div>
          ) : ready.length > 0 ? (
            /* Set up, and stopped. The panel used to show nothing here and
               then advise configuring an agent - advice for a step already
               finished, with the results these agents had produced hidden
               behind it. */
            <>
              <div className="grid-cards-sm">
                {ready.map((agent) => (
                  <AgentCard key={agent.slug} agent={agent} />
                ))}
              </div>
              {canView('agents') ? (
                <p className="panel-note">
                  {ready.length === 1
                    ? 'This agent is configured and stopped.'
                    : `${ready_agents} agents are configured and stopped.`}{' '}
                  Start {ready.length === 1 ? 'it' : 'them'} on the{' '}
                  <Link to="/agents">AI&nbsp;Agents</Link> screen and they run on
                  their own schedule from then on.
                </p>
              ) : null}
            </>
          ) : (
            <div className="panel-empty">
              <p>Nothing is set up yet.</p>
              {canView('agents') ? (
                <p className="small muted">
                  Configure an agent on the{' '}
                  <Link to="/agents">AI&nbsp;Agents</Link> screen, then start it.
                  Nothing runs against your site until you do.
                </p>
              ) : null}
            </div>
          )}

          <SectionHeading
            title="Connected systems"
            spaced
            action={
              canView('connectors') ? (
                <Link to="/connectors" className="btn btn-ghost">
                  View catalogue →
                </Link>
              ) : undefined
            }
          />
          {connectors.length > 0 ? (
            <div className="grid-cards-sm">
              {connectors.map((connector) => (
                <Blueprint key={connector.slug} className="card elev-sm">
                  <div className="card-head">
                    <ConnectorIcon slug={connector.slug} name={connector.name} size={28} />
                    <div className="card-head-text">
                      <div className="card-kicker">{connector.category}</div>
                      <div className="card-title" style={{ fontSize: 15 }}>
                        {connector.name}
                      </div>
                    </div>
                  </div>
                  <div className="row">
                    <Tag
                      tone={
                        connector.health === 'failing' || connector.health === 'degraded'
                          ? 'outline'
                          : 'accent'
                      }
                    >
                      {connector.status_label}
                    </Tag>
                  </div>
                  {connector.last_error ? (
                    <div className="agent-error">{connector.last_error}</div>
                  ) : connector.activity_label ? (
                    <div className="card-meta">{connector.activity_label}</div>
                  ) : null}
                </Blueprint>
              ))}
            </div>
          ) : (
            <div className="panel-empty">
              <p>Nothing is connected yet.</p>
              {canView('connectors') ? (
                <p className="small muted">
                  The agents work on whatever you connect - a CMS, an ad account,
                  analytics. Until then they run and report that they are waiting.
                  Start on the <Link to="/connectors">Connectors</Link> screen.
                </p>
              ) : null}
            </div>
          )}
        </div>

        <div>
          <SectionHeading
            title="Approval queue"
            action={
              canView('approvals') ? (
                <Link to="/approvals" className="btn btn-ghost">
                  View all →
                </Link>
              ) : undefined
            }
          />
          <div className="stack-sm">
            {approvals.map((item) => (
              <div key={item.id} className="card">
                <div className="row-between">
                  <Tag tone="accent">{item.type}</Tag>
                </div>
                <div style={{ fontSize: 13, marginTop: 4 }}>{item.title}</div>
                <div className="card-meta">
                  from {item.agent} · {item.meta}
                </div>
              </div>
            ))}
            {approvals.length === 0 ? (
              <div className="small muted">
                {canView('approvals')
                  ? 'Queue is clear.'
                  : 'The approval queue is not visible to your role.'}
              </div>
            ) : null}
          </div>
        </div>
      </div>
    </>
  )
}
