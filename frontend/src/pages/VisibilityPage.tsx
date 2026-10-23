/**
 * AI Visibility — how answer engines cite the brand and competitors.
 *
 * Target questions and competitor hosts are configured here; the AI Visibility
 * Tracker agent probes connected engines on its schedule. This screen does not
 * call models itself.
 */
import { useState } from 'react'

import { api } from '@/api/client'
import { useAuth } from '@/auth/AuthContext'
import { useToasts } from '@/components/Toasts'
import {
  Blueprint,
  EmptyState,
  ErrorState,
  Field,
  Loading,
  PageHeader,
  SectionHeading,
  StatTile,
  Tag,
} from '@/components/ui'
import { useResource } from '@/hooks/useResource'

function pct(share: number): string {
  return `${Math.round(share * 100)}%`
}

export function VisibilityPage() {
  const { canWrite } = useAuth()
  const { push, fromResult, fromError } = useToasts()
  const writable = canWrite('seo')
  const { data, loading, error, reload } = useResource(
    (signal) => api.visibility(signal),
    [],
    'visibility',
  )

  const [question, setQuestion] = useState('')
  const [competitorsText, setCompetitorsText] = useState('')
  const [busy, setBusy] = useState(false)

  if (loading && !data) return <Loading label="Loading AI visibility…" />
  if (error && !data) return <ErrorState message={error} onRetry={reload} />
  if (!data) return null

  const competitorsValue =
    competitorsText || data.competitors.join('\n')

  const addQuestion = async () => {
    if (!writable) {
      push('View-only access for your role', 'warning')
      return
    }
    const text = question.trim()
    if (text.length < 3) {
      push('Enter a longer question', 'warning')
      return
    }
    setBusy(true)
    try {
      await api.addVisibilityQuery({ question: text })
      setQuestion('')
      push('Question added', 'success')
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const removeQuestion = async (id: string) => {
    if (!writable) return
    setBusy(true)
    try {
      fromResult(await api.deleteVisibilityQuery(id))
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  const saveCompetitors = async () => {
    if (!writable) {
      push('View-only access for your role', 'warning')
      return
    }
    const domains = competitorsValue
      .split(/[\n,]+/)
      .map((part) => part.trim())
      .filter(Boolean)
    setBusy(true)
    try {
      await api.setVisibilityCompetitors(domains)
      setCompetitorsText('')
      push('Competitors saved', 'success')
      await reload()
    } catch (caught) {
      fromError(caught)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <PageHeader
        title="AI Visibility"
        description="How ChatGPT, Perplexity, Gemini and Claude answer your questions — and whether they cite you or a competitor."
      />

      <div className="stat-row">
        <StatTile kicker="Recent probes" value={String(data.summary.probes)} />
        <StatTile
          kicker="Brand citation share"
          value={pct(data.summary.brand_share)}
          meta={`${data.summary.brand_citations} of ${data.summary.probes}`}
        />
        <StatTile
          kicker="Tracked competitors"
          value={String(data.competitors.length)}
        />
      </div>

      <div className="grid-2">
        <Blueprint title="Target questions">
          <SectionHeading title="Ask these questions" />
          {data.queries.length === 0 ? (
            <EmptyState
              title="No questions yet"
              description="Add the prompts you care about. The AI Visibility Tracker agent will ask connected engines on its schedule."
            />
          ) : (
            <ul className="plain-list">
              {data.queries.map((item) => (
                <li key={item.id} className="list-row">
                  <div>
                    <strong>{item.label || item.question}</strong>
                    <div className="muted">{item.question}</div>
                    {!item.active ? <Tag tone="neutral">Paused</Tag> : null}
                  </div>
                  {writable ? (
                    <button
                      type="button"
                      className="btn ghost"
                      disabled={busy}
                      onClick={() => void removeQuestion(item.id)}
                    >
                      Remove
                    </button>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
          {writable ? (
            <div className="stack-form">
              <Field label="New question">
                <textarea
                  value={question}
                  onChange={(event) => setQuestion(event.target.value)}
                  rows={2}
                  maxLength={500}
                  placeholder="What is the best CRM for mid-market SaaS?"
                />
              </Field>
              <button
                type="button"
                className="btn primary"
                disabled={busy}
                onClick={() => void addQuestion()}
              >
                Add question
              </button>
            </div>
          ) : null}
        </Blueprint>

        <Blueprint title="Competitors">
          <SectionHeading title="Domains to flag when cited" />
          <Field label="One hostname per line">
            <textarea
              value={competitorsValue}
              onChange={(event) => setCompetitorsText(event.target.value)}
              rows={6}
              disabled={!writable}
              placeholder={'rival.example\nother-brand.com'}
            />
          </Field>
          {writable ? (
            <button
              type="button"
              className="btn primary"
              disabled={busy}
              onClick={() => void saveCompetitors()}
            >
              Save competitors
            </button>
          ) : null}
        </Blueprint>
      </div>

      <Blueprint title="By engine">
        {data.summary.by_engine.length === 0 ? (
          <EmptyState
            title="No probes yet"
            description="Configure the AI Visibility Tracker agent and connect an answer engine on Integrations."
          />
        ) : (
          <ul className="plain-list">
            {data.summary.by_engine.map((row) => (
              <li key={row.engine} className="list-row">
                <strong>{row.engine}</strong>
                <span>
                  {row.brand_citations}/{row.probes} cite you ({pct(row.share)})
                </span>
              </li>
            ))}
          </ul>
        )}
      </Blueprint>

      <Blueprint title="Recent observations">
        {data.recent.length === 0 ? (
          <EmptyState
            title="Nothing recorded yet"
            description="Runs appear here after the agent probes."
          />
        ) : (
          <ul className="plain-list">
            {data.recent.map((row) => (
              <li key={row.id} className="list-row">
                <div>
                  <strong>{row.engine}</strong>
                  <div className="muted">{row.question}</div>
                  <div className="muted">
                    {row.brand_cited
                      ? `Brand cited at #${row.brand_position ?? '?'}`
                      : 'Brand not cited'}
                    {row.competitors_cited.length
                      ? ` · Competitors: ${row.competitors_cited.join(', ')}`
                      : ''}
                  </div>
                </div>
                <Tag tone={row.brand_cited ? 'accent' : 'neutral'}>
                  {row.brand_cited ? 'Cited' : 'Miss'}
                </Tag>
              </li>
            ))}
          </ul>
        )}
      </Blueprint>

      {data.summary.top_competitors.length > 0 ? (
        <Blueprint title="Competitor sightings">
          <ul className="plain-list">
            {data.summary.top_competitors.map((row) => (
              <li key={row.domain} className="list-row">
                <strong>{row.domain}</strong>
                <span>{row.sightings} sightings</span>
              </li>
            ))}
          </ul>
        </Blueprint>
      ) : null}
    </>
  )
}
