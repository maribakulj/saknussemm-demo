import { useEffect, useState } from 'react'

import { putReviews } from '../api/client'
import { lineRegionUrl } from '../lib/iiif'
import type { LayoutLine, LineReview, ReviewVerdict } from '../types'

/**
 * Where a reader's judgement on one line is made.
 *
 * The corpora this demo runs on carry no ground truth — Gallica's `text.txt`
 * is the same OCR layer as its ALTO — so nothing on disk can say whether a
 * correction was right or a refusal justified. Only a person reading the scan
 * can, and this panel is where that reading is captured.
 *
 * Three verdicts, and the third is the one that compounds. `accepted` and
 * `refused` grade what the engine did, which is useful for tuning it.
 * `transcribed` records what the reader actually read, which stands on its own
 * whatever the engine decided — and accumulates, line by line, into the ground
 * truth the bench needs to answer "was the correction right".
 */

interface ReviewPanelProps {
  jobId: string
  pageId: string
  line: LayoutLine
  /** The reader's existing judgement on this line, if any. */
  existing: LineReview | null
  /**
   * IIIF Image API service base for this page, when the reader has one.
   * With it, the line is shown at the scan's NATIVE resolution and nothing is
   * stored — the alternative is judging a word from a downscaled preview,
   * which on a newspaper line is roughly 13 pixels tall.
   */
  iiifService?: string | null
  onSaved: (review: LineReview) => void
  onClose: () => void
}

const VERDICT_LABEL: Record<ReviewVerdict, string> = {
  accepted: 'Le moteur a eu raison',
  refused: 'Le moteur a eu tort',
  transcribed: 'Voici ce que je lis',
}

export function ReviewPanel({
  jobId,
  pageId,
  line,
  existing,
  iiifService,
  onSaved,
  onClose,
}: ReviewPanelProps) {
  const [transcription, setTranscription] = useState(existing?.transcription ?? '')
  const [note, setNote] = useState(existing?.note ?? '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // A reader moves line to line; the fields must follow the selection rather
  // than carry the previous line's text into the next one's judgement.
  useEffect(() => {
    setTranscription(existing?.transcription ?? '')
    setNote(existing?.note ?? '')
    setError(null)
  }, [line.line_id, pageId, existing])

  async function save(verdict: ReviewVerdict) {
    if (verdict === 'transcribed' && !transcription.trim()) {
      setError("Une transcription vide n'affirme rien : écrivez ce que vous lisez sur le scan.")
      return
    }
    setSaving(true)
    setError(null)
    try {
      const review: LineReview = {
        page_id: pageId,
        line_id: line.line_id,
        verdict,
        transcription: transcription.trim() || null,
        note: note.trim() || null,
      }
      const saved = await putReviews(jobId, [review])
      const mine = saved.find((r) => r.page_id === pageId && r.line_id === line.line_id)
      if (mine) onSaved(mine)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setSaving(false)
    }
  }

  const declined = line.proposal_declined

  return (
    <aside
      className="border border-slate-700 bg-slate-800 rounded p-4 flex flex-col gap-3"
      aria-label={`Jugement sur la ligne ${line.line_id}`}
    >
      <header className="flex items-baseline justify-between gap-3">
        <span className="font-mono text-[10px] uppercase tracking-wider text-slate-400">
          {line.line_id}
        </span>
        <button
          type="button"
          onClick={onClose}
          className="font-mono text-[10px] text-slate-400 hover:text-slate-200"
        >
          fermer
        </button>
      </header>

      {line.verdict === 'review_required' && (
        <div className="text-xs text-amber-200 space-y-1">
          <p>Relecture requise</p>
          <ul>
            {(line.review_reasons ?? []).map((reason, index) => (
              <li key={`${reason.code}-${index}`}>
                {reason.code}
                {reason.detail ? ` — ${reason.detail}` : ''}
              </li>
            ))}
          </ul>
        </div>
      )}
      <p className="text-xs text-slate-400">
        Les jugements enregistrés ne modifient pas le XML téléchargé.
      </p>

      {iiifService && (
        <figure className="m-0">
          <img
            src={lineRegionUrl(iiifService, line)}
            alt={`Ligne ${line.line_id} sur le scan`}
            className="w-full rounded border border-slate-600 bg-white"
          />
          <figcaption className="font-mono text-[10px] text-slate-500 mt-1">
            résolution native, servie par IIIF — rien n'est stocké
          </figcaption>
        </figure>
      )}

      <dl className="flex flex-col gap-2 text-xs">
        <div>
          <dt className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
            OCR source
          </dt>
          <dd className="font-mono text-slate-200 break-words">{line.ocr_text}</dd>
        </div>
        {line.proposed_text && line.proposed_text !== line.ocr_text && (
          <div>
            <dt className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
              Proposé{declined ? ' — et refusé' : ''}
            </dt>
            <dd className={`font-mono break-words ${declined ? 'text-red-300' : 'text-blue-300'}`}>
              {line.proposed_text}
            </dd>
          </div>
        )}
        <div>
          <dt className="font-mono text-[10px] uppercase tracking-wider text-slate-500">Retenu</dt>
          <dd className="font-mono text-slate-100 break-words">{line.corrected_text}</dd>
        </div>
        {line.verdict && (
          <div>
            <dt className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
              Verdict du moteur
            </dt>
            <dd className="font-mono text-amber-300 break-words">
              {line.verdict}
              {line.verdict_detail ? ` — ${line.verdict_detail}` : ''}
            </dd>
          </div>
        )}
      </dl>

      <label className="flex flex-col gap-1">
        <span className="font-mono text-[10px] uppercase tracking-wider text-slate-500">
          Ce que je lis sur le scan
        </span>
        <textarea
          value={transcription}
          onChange={(e) => setTranscription(e.target.value)}
          rows={2}
          spellCheck={false}
          className="font-mono text-xs bg-slate-900 border border-slate-600 text-slate-100
                     rounded px-2 py-1 focus:outline-none focus:border-amber-500"
        />
      </label>

      <label className="flex flex-col gap-1">
        <span className="font-mono text-[10px] uppercase tracking-wider text-slate-500">Note</span>
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          className="font-mono text-xs bg-slate-900 border border-slate-600 text-slate-100
                     rounded px-2 py-1 focus:outline-none focus:border-amber-500"
        />
      </label>

      {error && (
        <p role="alert" className="text-xs text-red-300">
          {error}
        </p>
      )}

      <div className="flex flex-wrap gap-2">
        {(['accepted', 'refused', 'transcribed'] as const).map((verdict) => (
          <button
            key={verdict}
            type="button"
            disabled={saving}
            onClick={() => save(verdict)}
            aria-pressed={existing?.verdict === verdict}
            className={`font-mono text-[11px] rounded px-3 py-1.5 border transition-colors
                        disabled:opacity-50 focus:outline-none focus:ring-1
                        focus:ring-amber-400 ${
                          existing?.verdict === verdict
                            ? 'bg-amber-500 border-amber-400 text-slate-900'
                            : 'bg-slate-700 border-slate-600 text-slate-200 hover:border-amber-500'
                        }`}
          >
            {VERDICT_LABEL[verdict]}
          </button>
        ))}
      </div>

      {existing?.reviewed_at && (
        <p className="font-mono text-[10px] text-slate-500">Jugé le {existing.reviewed_at}</p>
      )}
    </aside>
  )
}
