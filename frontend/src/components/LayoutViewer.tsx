import { useCallback, useEffect, useRef, useState } from 'react'

import { fetchReviews } from '../api/client'
import { looksLikeService } from '../lib/iiif'
import { lineKey, type LineKey } from '../lib/lineKey'
import { FAMILY, verdictFamily, type VerdictFamily } from '../lib/verdicts'
import { ReviewPanel } from './ReviewPanel'
import type { LayoutData, LayoutLine, LayoutPage, LineReview } from '../types'

// ---------------------------------------------------------------------------
// SVG colour constants (inline — Tailwind classes don't apply to SVG attrs)
// ---------------------------------------------------------------------------

const C = {
  pageBg: '#ffffff',
  blockBorder: '#475569', // slate-600
  textUnchanged: '#94a3b8', // slate-400
  textChanged: '#d97706', // amber-600 (readable on white)
  rectChanged: 'rgba(253,230,138,0.25)',
  hyphenBar: '#f59e0b', // amber-500
} as const

// ---------------------------------------------------------------------------
// SVGOverlay — the annotation layer (blocks + lines + text)
// Rendered either on a white background (no image) or as a transparent
// overlay on top of a scan image.
// ---------------------------------------------------------------------------

interface SVGOverlayProps {
  page: LayoutPage
  side: 'ocr' | 'corrected'
  /** 0 = invisible, 1 = fully opaque. Applied to the whole SVG group. */
  opacity: number
  /** When true, a white page rect is drawn first (standalone mode). */
  withBackground: boolean
  /**
   * Verdict families to keep at full strength. The others are DIMMED, not
   * hidden: a line that copied its neighbour only reads as wrong next to
   * that neighbour, so removing the context removes the evidence.
   */
  active: ReadonlySet<VerdictFamily>
  selectedId: string | null
  onSelect: (lineId: string) => void
}

function SVGOverlay({
  page,
  side,
  opacity,
  withBackground,
  active,
  selectedId,
  onSelect,
}: SVGOverlayProps) {
  const { blocks } = page
  const W = page.page_width || blocks.reduce((m, b) => Math.max(m, b.hpos + b.width), 0)
  const H = page.page_height || blocks.reduce((m, b) => Math.max(m, b.vpos + b.height), 0)

  if (!W || !H) {
    return (
      <div className="p-6 font-mono text-xs text-slate-500 text-center">
        Coordonnées de ligne absentes — impossible d'afficher la mise en page.
      </div>
    )
  }

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      width="100%"
      preserveAspectRatio="xMinYMin meet"
      style={{ display: 'block', position: 'relative', zIndex: 1 }}
    >
      {withBackground && <rect x={0} y={0} width={W} height={H} fill={C.pageBg} />}

      <g opacity={opacity}>
        {blocks.map((block) => (
          <g key={block.block_id}>
            <rect
              x={block.hpos}
              y={block.vpos}
              width={block.width}
              height={block.height}
              fill="none"
              stroke={C.blockBorder}
              strokeWidth={6}
              opacity={withBackground ? 1 : 0.6}
            />

            {block.lines.map((line) => {
              const displayText = side === 'ocr' ? line.ocr_text : line.corrected_text
              const hasHyphen = line.hyphen_role !== 'none'
              const family = verdictFamily(line)
              const isSelected = selectedId === line.line_id
              // Dimmed, not removed — see the `active` prop.
              const dim = active.has(family) ? 1 : 0.16

              if (withBackground) {
                // SVG-only mode: coloured text on white page
                const fontSize = Math.max(line.height * 0.7, 1)
                const textY = line.vpos + line.height * 0.75
                return (
                  <g
                    key={line.line_id}
                    opacity={dim}
                    onClick={() => onSelect(line.line_id)}
                    style={{ cursor: 'pointer' }}
                  >
                    {line.modified && (
                      <rect
                        x={line.hpos}
                        y={line.vpos}
                        width={line.width}
                        height={line.height}
                        fill={C.rectChanged}
                      />
                    )}
                    {hasHyphen && (
                      <rect
                        x={line.hpos}
                        y={line.vpos}
                        width={8}
                        height={line.height}
                        fill={C.hyphenBar}
                      />
                    )}
                    <text
                      x={line.hpos + 4}
                      y={textY}
                      fontSize={fontSize}
                      fill={line.modified ? C.textChanged : C.textUnchanged}
                      textLength={line.width - 8 > 0 ? line.width - 8 : undefined}
                      lengthAdjust="spacingAndGlyphs"
                      style={{ fontFamily: 'serif' }}
                    >
                      {displayText}
                    </text>
                  </g>
                )
              } else {
                // Image overlay mode: semi-opaque bg behind text for readability
                const fontSize = Math.max(line.height * 0.72, 1)
                const textY = line.vpos + line.height * 0.78
                return (
                  <g
                    key={line.line_id}
                    opacity={dim}
                    onClick={() => onSelect(line.line_id)}
                    style={{ cursor: 'pointer' }}
                  >
                    <rect
                      x={line.hpos}
                      y={line.vpos}
                      width={line.width}
                      height={line.height}
                      fill={FAMILY[family].fill}
                      stroke={FAMILY[family].stroke}
                      strokeWidth={isSelected ? 6 : 2}
                    />
                    {hasHyphen && (
                      <rect
                        x={line.hpos}
                        y={line.vpos}
                        width={8}
                        height={line.height}
                        fill={C.hyphenBar}
                        opacity={0.9}
                      />
                    )}
                    <text
                      x={line.hpos + 4}
                      y={textY}
                      fontSize={fontSize}
                      fill="#0f172a"
                      textLength={line.width - 8 > 0 ? line.width - 8 : undefined}
                      lengthAdjust="spacingAndGlyphs"
                      style={{ fontFamily: 'serif' }}
                    >
                      {displayText}
                    </text>
                  </g>
                )
              }
            })}
          </g>
        ))}
      </g>
    </svg>
  )
}

// ---------------------------------------------------------------------------
// PagePanel — one side (ocr | corrected): image background + SVG overlay
// ---------------------------------------------------------------------------

interface PagePanelProps {
  page: LayoutPage
  side: 'ocr' | 'corrected'
  overlayOpacity: number
  active: ReadonlySet<VerdictFamily>
  selectedId: string | null
  onSelect: (lineId: string) => void
}

function PagePanel({ page, side, overlayOpacity, active, selectedId, onSelect }: PagePanelProps) {
  const { blocks } = page
  const W = page.page_width || blocks.reduce((m, b) => Math.max(m, b.hpos + b.width), 0)
  const H = page.page_height || blocks.reduce((m, b) => Math.max(m, b.vpos + b.height), 0)

  if (page.image_url) {
    // The SVG is the in-flow element that establishes the container height
    // via its viewBox aspect ratio. The image sits behind it (position: absolute).
    // This avoids the CSS issue where `height: 100%` on an absolutely-positioned
    // child of a height:auto container resolves to `auto`, not the parent's height.
    return (
      <div style={{ position: 'relative' }}>
        {W && H && (
          <img
            src={page.image_url}
            alt="source scan"
            style={{
              position: 'absolute',
              top: 0,
              left: 0,
              width: '100%',
              height: '100%',
              objectFit: 'fill',
            }}
          />
        )}
        <SVGOverlay
          page={page}
          side={side}
          opacity={overlayOpacity}
          withBackground={!W || !H}
          active={active}
          selectedId={selectedId}
          onSelect={onSelect}
        />
      </div>
    )
  }

  // No image: SVG on white background — opacity still controlled by slider
  return (
    <SVGOverlay
      page={page}
      side={side}
      opacity={overlayOpacity}
      withBackground={true}
      active={active}
      selectedId={selectedId}
      onSelect={onSelect}
    />
  )
}

// ---------------------------------------------------------------------------
// LayoutViewer
// ---------------------------------------------------------------------------

interface LayoutViewerProps {
  data: LayoutData
  /**
   * When given, clicking a line opens the judgement panel and the reader's
   * verdict is persisted against this job. Omit it and the view stays
   * read-only — which is what the demo does before a run has a report.
   */
  jobId?: string
}

export function LayoutViewer({ data, jobId }: LayoutViewerProps) {
  const [pageIdx, setPageIdx] = useState(0)
  const [overlayOpacity, setOverlayOpacity] = useState(0.85)
  // All families on by default: a reviewer opening the page should see
  // what the run did before deciding what to hunt for.
  const [active, setActive] = useState<ReadonlySet<VerdictFamily>>(
    new Set<VerdictFamily>(['kept', 'review', 'refused', 'silent']),
  )
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [reviews, setReviews] = useState<Map<LineKey, LineReview>>(new Map())
  // Remembered per browser, not per job: a reviewer working through one
  // volume pastes the service once and every page of it resolves. The ALTO
  // does not carry the identifier, so guessing it would be inventing
  // provenance — the reader supplies it or the panel shows text only.
  const [iiifService, setIiifService] = useState<string>(
    () => globalThis.localStorage?.getItem('saknussemm.iiif') ?? '',
  )
  const iiifUsable = looksLikeService(iiifService)

  // Judgements already made travel with the job, so a reader can stop and
  // come back without losing the page they had worked through.
  useEffect(() => {
    if (!jobId) return
    let cancelled = false
    fetchReviews(jobId)
      .then(({ reviews: rows }) => {
        if (cancelled) return
        setReviews(new Map(rows.map((r) => [lineKey(r.page_id, r.line_id), r])))
      })
      .catch(() => {
        // A review list that will not load must not take the layout with it:
        // looking at the page is still worth doing.
      })
    return () => {
      cancelled = true
    }
  }, [jobId])

  const toggleFamily = useCallback((family: VerdictFamily) => {
    setActive((current) => {
      const next = new Set(current)
      if (next.has(family)) next.delete(family)
      else next.add(family)
      // Turning the last one off would blank the page and read as a bug.
      return next.size ? next : current
    })
  }, [])
  const leftRef = useRef<HTMLDivElement>(null)
  const rightRef = useRef<HTMLDivElement>(null)
  const syncing = useRef(false)

  // All hooks (useCallback) must come BEFORE any conditional return,
  // otherwise the hook count varies between renders. See PR 2 / B-002.
  const onScrollLeft = useCallback(() => {
    if (syncing.current || !leftRef.current || !rightRef.current) return
    syncing.current = true
    rightRef.current.scrollTop = leftRef.current.scrollTop
    syncing.current = false
  }, [])

  const onScrollRight = useCallback(() => {
    if (syncing.current || !leftRef.current || !rightRef.current) return
    syncing.current = true
    leftRef.current.scrollTop = rightRef.current.scrollTop
    syncing.current = false
  }, [])

  // Guard against empty result sets — without this, currentPage.image_url crashes.
  if (data.pages.length === 0) {
    return (
      <div className="rounded-lg border border-slate-700/60 bg-slate-800/40 p-6 text-center">
        <p className="font-mono text-xs text-slate-500">Aucune mise en page à afficher.</p>
      </div>
    )
  }

  const currentPage = data.pages[pageIdx] ?? data.pages[0]
  const selectedLine: LayoutLine | null = selectedId
    ? (currentPage?.blocks.flatMap((b) => b.lines).find((l) => l.line_id === selectedId) ?? null)
    : null
  const hasImage = !!currentPage.image_url
  const reviewQueue = data.pages.flatMap((p, index) =>
    p.blocks.flatMap((b) =>
      b.lines
        .filter((line) => line.verdict === 'review_required')
        .map((line) => ({ page: p, pageIdx: index, line })),
    ),
  )

  return (
    <div className="rounded-lg border border-slate-700/60 bg-slate-800/40 overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 border-b border-slate-700/60 flex items-center justify-between gap-4 flex-wrap">
        <h3 className="font-serif text-sm font-semibold text-slate-200">
          Visionneuse structurelle
        </h3>

        <div className="flex items-center gap-4 flex-wrap">
          {/* Opacity slider — always visible */}
          <label className="flex items-center gap-2">
            <span className="font-mono text-[10px] text-slate-500 uppercase tracking-wider whitespace-nowrap">
              Texte
            </span>
            <input
              type="range"
              min={0}
              max={100}
              value={Math.round(overlayOpacity * 100)}
              onChange={(e) => setOverlayOpacity(Number(e.target.value) / 100)}
              className="w-24 accent-amber-500 cursor-pointer"
            />
            <span className="font-mono text-[10px] text-amber-400 w-7 text-right">
              {Math.round(overlayOpacity * 100)}%
            </span>
          </label>

          {/* Verdict filter — dims, never hides. A line that copied its
              neighbour only reads as wrong NEXT TO that neighbour, so the
              context has to stay on the page. */}
          <div className="flex items-center gap-1.5">
            {(['kept', 'review', 'refused', 'silent'] as const).map((family) => {
              const on = active.has(family)
              return (
                <button
                  key={family}
                  type="button"
                  onClick={() => toggleFamily(family)}
                  aria-pressed={on}
                  title={`${FAMILY[family].label} — cliquer pour estomper`}
                  className="font-mono text-[10px] uppercase tracking-wider rounded px-2 py-1
                             border transition-opacity focus:outline-none focus:ring-1
                             focus:ring-amber-400"
                  style={{
                    borderColor: FAMILY[family].stroke,
                    color: on ? '#e2e8f0' : '#64748b',
                    background: on ? FAMILY[family].fill : 'transparent',
                    opacity: on ? 1 : 0.55,
                  }}
                >
                  {FAMILY[family].label}
                </button>
              )
            })}
          </div>

          {/* IIIF service — full-resolution line crops, nothing stored */}
          <label className="flex items-center gap-2">
            <span className="font-mono text-[10px] text-slate-500 uppercase tracking-wider whitespace-nowrap">
              IIIF
            </span>
            <input
              type="url"
              value={iiifService}
              placeholder="https://…/iiif/image/v3/ark:/12148/…/f1"
              onChange={(e) => {
                setIiifService(e.target.value)
                globalThis.localStorage?.setItem('saknussemm.iiif', e.target.value)
              }}
              className={`font-mono text-[11px] bg-slate-900 border rounded px-2 py-1 w-56
                          text-slate-200 focus:outline-none focus:border-amber-500 ${
                            iiifService && !iiifUsable ? 'border-red-500' : 'border-slate-600'
                          }`}
            />
            {iiifService && !iiifUsable && (
              <span className="font-mono text-[10px] text-red-400">
                base du service, pas une image
              </span>
            )}
          </label>

          {/* Page selector */}
          {data.pages.length > 1 && (
            <select
              value={pageIdx}
              onChange={(e) => {
                setPageIdx(Number(e.target.value))
                setSelectedId(null)
              }}
              className="font-mono text-xs bg-slate-700 border border-slate-600 text-slate-200
                         rounded px-2 py-1 focus:outline-none focus:border-amber-500"
            >
              {data.pages.map((p, i) => (
                <option key={p.page_id} value={i}>
                  Page {i + 1} — {p.page_id}
                </option>
              ))}
            </select>
          )}
        </div>
      </div>

      {reviewQueue.length > 0 && (
        <section
          aria-label="Lignes à relire"
          className="px-4 py-3 border-b border-amber-800 text-sm text-amber-200"
        >
          <p>{reviewQueue.length} ligne(s) signalée(s) pour relecture</p>
          <p className="text-xs mt-1">
            Les jugements enregistrés ne modifient pas le XML téléchargé.
          </p>
          <ul className="max-h-40 overflow-auto mt-2 space-y-1">
            {reviewQueue.map(({ page, pageIdx: index, line }) => (
              <li key={lineKey(page.page_id, line.line_id)}>
                <button
                  type="button"
                  className="text-left underline underline-offset-2"
                  onClick={() => {
                    setPageIdx(index)
                    setSelectedId(line.line_id)
                  }}
                >
                  Page {index + 1} — {line.line_id}
                  {line.review_reasons?.length
                    ? ` : ${line.review_reasons.map((r) => r.code).join(', ')}`
                    : ''}
                  {reviews.has(lineKey(page.page_id, line.line_id)) ? ' — jugement enregistré' : ''}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Column labels */}
      <div className="grid grid-cols-2 border-b border-slate-700/40 bg-slate-800/60">
        <div
          className="px-3 py-1.5 font-mono text-[10px] text-slate-500 uppercase tracking-wider
                        border-r border-slate-700/40"
        >
          OCR source{hasImage ? ' (scan)' : ''}
        </div>
        <div className="px-3 py-1.5 font-mono text-[10px] text-slate-500 uppercase tracking-wider">
          {reviewQueue.length ? 'Candidat' : 'Corrigé'}
          {hasImage ? ' (scan)' : ''}
        </div>
      </div>

      {/* Dual panels with synchronised scroll */}
      <div className="grid grid-cols-2 divide-x divide-slate-700/40">
        <div ref={leftRef} onScroll={onScrollLeft} className="overflow-auto max-h-[60vh]">
          <PagePanel
            page={currentPage}
            side="ocr"
            overlayOpacity={overlayOpacity}
            active={active}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />
        </div>
        <div ref={rightRef} onScroll={onScrollRight} className="overflow-auto max-h-[60vh]">
          <PagePanel
            page={currentPage}
            side="corrected"
            overlayOpacity={overlayOpacity}
            active={active}
            selectedId={selectedId}
            onSelect={setSelectedId}
          />
        </div>
      </div>

      {/* Judgement panel — only with a job to persist against, and only once
          a line is selected. A reviewer who cannot record a verdict produces
          an impression; one who can produces the ground truth these corpora
          do not ship. */}
      {jobId && selectedLine && (
        <div className="px-4 py-3 border-t border-slate-700/40">
          <ReviewPanel
            jobId={jobId}
            pageId={currentPage.page_id}
            line={selectedLine}
            existing={reviews.get(lineKey(currentPage.page_id, selectedLine.line_id)) ?? null}
            iiifService={iiifUsable ? iiifService : null}
            onSaved={(review) =>
              setReviews((current) => {
                const next = new Map(current)
                next.set(lineKey(review.page_id, review.line_id), review)
                return next
              })
            }
            onClose={() => setSelectedId(null)}
          />
        </div>
      )}

      {/* Legend */}
      <div className="px-4 py-2.5 border-t border-slate-700/40 flex items-center gap-6 flex-wrap">
        <span className="font-mono text-[10px] text-slate-600 uppercase tracking-wider mr-1">
          Légende :
        </span>
        <div className="flex items-center gap-1.5">
          <div
            className="w-4 h-3 rounded-sm border border-amber-400/40"
            style={{ background: 'rgba(253,230,138,0.25)' }}
          />
          <span className="font-mono text-[10px] text-slate-500">ligne modifiée</span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="w-2 h-3 rounded-sm" style={{ background: '#f59e0b' }} />
          <span className="font-mono text-[10px] text-slate-500">césure</span>
        </div>
        <div className="flex items-center gap-1.5">
          <div className="w-4 h-3 rounded-sm border border-slate-600/60" />
          <span className="font-mono text-[10px] text-slate-500">ligne inchangée</span>
        </div>
      </div>
    </div>
  )
}
