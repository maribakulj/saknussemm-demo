import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { LayoutBlock, LayoutData, LayoutLine, LayoutPage } from '../types'
import { LayoutViewer } from './LayoutViewer'
import { downloadReviews, touchReviewActivity } from '../api/client'

vi.mock('../api/client', () => ({
  fetchReviews: vi.fn(async () => ({ reviews: [] })),
  putReviews: vi.fn(async (_job, reviews) => reviews),
  touchReviewActivity: vi.fn(async () => {}),
  downloadReviews: vi.fn(async () => {}),
}))

// ---------------------------------------------------------------------------
// Builders
// ---------------------------------------------------------------------------

function line(over: Partial<LayoutLine> = {}): LayoutLine {
  return {
    line_id: 'L1',
    hpos: 10,
    vpos: 10,
    width: 300,
    height: 20,
    ocr_text: 'texte ocr',
    corrected_text: 'texte corrigé',
    modified: false,
    hyphen_role: 'none',
    verdict: null,
    verdict_detail: null,
    proposed_text: null,
    proposal_declined: false,
    ...over,
  }
}

function block(lines: LayoutLine[], over: Partial<LayoutBlock> = {}): LayoutBlock {
  return { block_id: 'B1', hpos: 5, vpos: 5, width: 400, height: 200, lines, ...over }
}

function page(blocks: LayoutBlock[], over: Partial<LayoutPage> = {}): LayoutPage {
  return {
    page_id: 'p1',
    page_index: 0,
    page_width: 500,
    page_height: 700,
    image_url: null,
    blocks,
    ...over,
  }
}

function data(pages: LayoutPage[]): LayoutData {
  return { job_id: 'j1', pages }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('LayoutViewer', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })
  it('renews retention only after reader interaction and offers an explicit export', async () => {
    const clock = vi.spyOn(Date, 'now').mockReturnValue(1000)
    vi.mocked(touchReviewActivity).mockClear()
    vi.mocked(downloadReviews).mockClear()
    render(<LayoutViewer jobId="j1" data={data([page([block([line()])])])} />)
    expect(touchReviewActivity).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: /Exporter les jugements/ }))
    await waitFor(() => expect(downloadReviews).toHaveBeenCalledWith('j1'))
    expect(touchReviewActivity).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: /Retenue/ }))
    expect(touchReviewActivity).toHaveBeenCalledTimes(1)
    clock.mockReturnValue(62000)
    fireEvent.click(screen.getByRole('button', { name: /Retenue/ }))
    await waitFor(() => expect(touchReviewActivity).toHaveBeenCalledTimes(2))
    expect(screen.getByText(/mémoire temporaire/)).toBeInTheDocument()
  })
  it('binds a verified IIIF service to this page and job without inheriting a saved global URL', () => {
    vi.stubGlobal('localStorage', { getItem: () => 'https://example.org/old/f9' })
    const pages = [
      page([block([line({ verdict: 'review_required' })])]),
      page([block([line({ verdict: 'review_required' })])], { page_id: 'p2', page_index: 1 }),
    ]
    const { rerender } = render(<LayoutViewer jobId="j1" data={data(pages)} />)
    const input = screen.getByRole('textbox', { name: /IIIF/ })
    expect(input).toHaveValue('')
    fireEvent.change(input, { target: { value: 'https://example.org/volume/f1' } })
    fireEvent.click(screen.getByRole('button', { name: /Page 1.*L1/ }))
    expect(screen.queryByAltText('Ligne L1 sur le scan')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: /coordonnées XML.*pixels/i }))
    expect(screen.getByAltText('Ligne L1 sur le scan')).toHaveAttribute(
      'src',
      expect.stringContaining('/volume/f1/'),
    )
    fireEvent.click(screen.getByRole('button', { name: /Page 2.*L1/ }))
    expect(input).toHaveValue('')
    expect(screen.queryByAltText('Ligne L1 sur le scan')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Page 1.*L1/ }))
    expect(input).toHaveValue('https://example.org/volume/f1')
    expect(screen.getByAltText('Ligne L1 sur le scan')).toBeInTheDocument()
    fireEvent.change(input, { target: { value: 'https://example.org/other/f3' } })
    expect(screen.queryByAltText('Ligne L1 sur le scan')).not.toBeInTheDocument()
    rerender(<LayoutViewer jobId="j2" data={{ ...data(pages), job_id: 'j2' }} />)
    expect(screen.getByRole('textbox', { name: /IIIF/ })).toHaveValue('')
    expect(screen.queryByAltText('Ligne L1 sur le scan')).not.toBeInTheDocument()
  })

  it('opens a flagged line on another page and keeps it flagged after a human judgement', async () => {
    const flagged = line({
      verdict: 'review_required',
      modified: true,
      review_reasons: [{ code: 'digits_changed', detail: '1789 added' }],
    })
    render(
      <LayoutViewer
        jobId="j1"
        data={data([
          page([block([line()])]),
          page([block([flagged])], { page_id: 'p2', page_index: 1 }),
        ])}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: /Page 2.*L1/ }))
    expect(screen.getByRole('combobox')).toHaveValue('1')
    expect(
      screen.getByRole('complementary', { name: /Jugement sur la ligne L1/ }),
    ).toHaveTextContent('digits_changed — 1789 added')
    fireEvent.click(screen.getByRole('button', { name: /Le moteur a eu raison/ }))
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: /Page 2.*L1.*jugement enregistré/ }),
      ).toBeInTheDocument(),
    )
    expect(screen.getByText(/1 ligne\(s\) signalée\(s\) pour relecture/)).toBeInTheDocument()
    expect(screen.getAllByText(/ne modifient pas.*XML/).length).toBeGreaterThan(0)
  })

  it('shows the empty message when there are no pages', () => {
    render(<LayoutViewer data={data([])} />)
    expect(screen.getByText(/aucune mise en page/i)).toBeInTheDocument()
  })

  it('reports missing coordinates instead of rendering an empty SVG', () => {
    const p = page([], { page_width: 0, page_height: 0 })
    render(<LayoutViewer data={data([p])} />)
    // Both panels (OCR + corrected) fall back to the explanatory message.
    expect(screen.getAllByText(/coordonnées de ligne absentes/i)).toHaveLength(2)
  })

  it('renders OCR text on the left panel and corrected text on the right', () => {
    const p = page([block([line({ modified: true })])])
    const { container } = render(<LayoutViewer data={data([p])} />)
    expect(container.querySelectorAll('svg')).toHaveLength(2)
    expect(screen.getByText('texte ocr')).toBeInTheDocument()
    expect(screen.getByText('texte corrigé')).toBeInTheDocument()
    // Legend present.
    expect(screen.getByText('ligne modifiée')).toBeInTheDocument()
    expect(screen.getByText('césure')).toBeInTheDocument()
  })

  it('derives page size from blocks when page_width/height are missing', () => {
    const p = page([block([line()])], { page_width: 0, page_height: 0 })
    const { container } = render(<LayoutViewer data={data([p])} />)
    const svg = container.querySelector('svg')
    // W = block.hpos + block.width = 405, H = 205
    expect(svg?.getAttribute('viewBox')).toBe('0 0 405 205')
  })

  it('draws the hyphen bar and the modified highlight in SVG-only mode', () => {
    const p = page([
      block([
        line({ line_id: 'Lmod', modified: true }),
        line({ line_id: 'Lhyp', vpos: 40, hyphen_role: 'HypPart1' }),
      ]),
    ])
    const { container } = render(<LayoutViewer data={data([p])} />)
    const rects = Array.from(container.querySelectorAll('rect'))
    // Modified-line highlight (per panel).
    expect(rects.filter((r) => r.getAttribute('fill') === 'rgba(253,230,138,0.25)')).toHaveLength(2)
    // Hyphen bar: fixed 8px-wide amber rect (per panel).
    const bars = rects.filter(
      (r) => r.getAttribute('fill') === '#f59e0b' && r.getAttribute('width') === '8',
    )
    expect(bars).toHaveLength(2)
  })

  it('omits textLength on lines too narrow to justify', () => {
    const p = page([block([line({ width: 6 })])])
    const { container } = render(<LayoutViewer data={data([p])} />)
    const texts = Array.from(container.querySelectorAll('text'))
    expect(texts.length).toBeGreaterThan(0)
    for (const t of texts) {
      expect(t.getAttribute('textLength')).toBeNull()
    }
  })

  it('updates the overlay opacity from the slider', () => {
    const p = page([block([line()])])
    const { container } = render(<LayoutViewer data={data([p])} />)
    expect(screen.getByText('85%')).toBeInTheDocument()

    fireEvent.change(container.querySelector('input[type="range"]')!, { target: { value: '40' } })

    expect(screen.getByText('40%')).toBeInTheDocument()
    const groups = Array.from(container.querySelectorAll('svg > g'))
    expect(groups.some((g) => g.getAttribute('opacity') === '0.4')).toBe(true)
  })

  it('switches pages through the selector when several pages exist', () => {
    const p1 = page([block([line({ ocr_text: 'page un' })])])
    const p2 = page([block([line({ line_id: 'L2', ocr_text: 'page deux' })])], {
      page_id: 'p2',
      page_index: 1,
    })
    render(<LayoutViewer data={data([p1, p2])} />)

    expect(screen.getByText('page un')).toBeInTheDocument()
    expect(screen.queryByText('page deux')).not.toBeInTheDocument()

    fireEvent.change(screen.getByRole('combobox'), { target: { value: '1' } })

    expect(screen.getByText('page deux')).toBeInTheDocument()
    expect(screen.queryByText('page un')).not.toBeInTheDocument()
  })

  it('hides the page selector for a single page', () => {
    render(<LayoutViewer data={data([page([block([line()])])])} />)
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })

  it('renders the scan image behind a transparent overlay when image_url is set', () => {
    const p = page([block([line({ modified: true })])], { image_url: '/img/p1.jpg' })
    const { container } = render(<LayoutViewer data={data([p])} />)

    const imgs = Array.from(container.querySelectorAll('img'))
    expect(imgs).toHaveLength(2)
    expect(imgs[0]).toHaveAttribute('src', '/img/p1.jpg')
    // Column labels flag scan mode.
    expect(screen.getByText(/ocr source \(scan\)/i)).toBeInTheDocument()

    // Overlay mode: the line rect is coloured by VERDICT, not by "did the
    // text change". A modified line with no guard verdict is `kept` — blue,
    // the proof-reader's pencil for what stands.
    const rects = Array.from(container.querySelectorAll('rect'))
    expect(rects.some((r) => r.getAttribute('fill') === 'rgba(29,78,216,0.18)')).toBe(true)
  })

  it('colours a refused line differently from a kept one', () => {
    // The distinction the whole review view exists for: a line that kept its
    // OCR text because a guard REFUSED a proposal is not the same case as a
    // line nobody proposed anything for, and a reviewer must see which.
    const p = page(
      [
        block([
          line({ line_id: 'kept', modified: true }),
          line({ line_id: 'refused', verdict: 'too_different_from_source' }),
        ]),
      ],
      { image_url: '/img/p1.jpg' },
    )
    const { container } = render(<LayoutViewer data={data([p])} />)
    const fills = Array.from(container.querySelectorAll('rect')).map((r) => r.getAttribute('fill'))
    expect(fills).toContain('rgba(29,78,216,0.18)')
    expect(fills).toContain('rgba(185,28,28,0.20)')
  })

  it('the verdict filter dims a family instead of removing it', () => {
    // Dimming, not hiding: a line that copied its neighbour only reads as
    // wrong NEXT TO that neighbour, so the context has to stay on the page.
    const p = page([block([line({ line_id: 'refused', verdict: 'too_different_from_source' })])], {
      image_url: '/img/p1.jpg',
    })
    const { container } = render(<LayoutViewer data={data([p])} />)
    const before = container.querySelectorAll('rect').length

    fireEvent.click(screen.getByRole('button', { name: /refusée/i }))

    expect(container.querySelectorAll('rect').length).toBe(before)
    const dimmed = Array.from(container.querySelectorAll('g')).some(
      (g) => g.getAttribute('opacity') === '0.16',
    )
    expect(dimmed).toBe(true)
  })

  it('synchronises scroll positions between the two panels', () => {
    const p = page([block([line()])])
    const { container } = render(<LayoutViewer data={data([p])} />)
    const [left, right] = Array.from(container.querySelectorAll('.overflow-auto'))

    fireEvent.scroll(left, { target: { scrollTop: 120 } })
    expect(right.scrollTop).toBe(120)

    fireEvent.scroll(right, { target: { scrollTop: 40 } })
    expect(left.scrollTop).toBe(40)
  })
})
