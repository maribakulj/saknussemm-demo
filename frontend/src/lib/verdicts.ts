// ---------------------------------------------------------------------------
// Verdict palette — what the engine decided, and why
// ---------------------------------------------------------------------------
//
// Separate review referrals from corrections that survived the guards:
//
//   kept      the correction survived every guard
//   review    the proposed correction requires a reader before acceptance
//   refused   something was proposed and a guard declined it — the cases
//             worth a human eye, since each is either a caught hallucination
//             or a good correction thrown away
//   silent    nothing was proposed, or nothing changed; no judgement to make
//
// Colours are the proof-reader's two pencils: blue marks what stands, red
// marks what was struck out. Amber sits between them for a line the engine
// never got an answer for.

export type VerdictFamily = 'kept' | 'review' | 'refused' | 'silent'

const REFUSAL_CODES = new Set([
  'too_different_from_source',
  'closer_to_previous_line',
  'closer_to_next_line',
  'absorbs_previous_line',
  'absorbs_next_line',
  'hyphen_pair_fallback',
  'boundary_migration_forward',
  'boundary_migration_backward',
  'adjacent_duplicate_detected',
  'adjacent_duplicate_pair_atomicity',
  'orphan_hyphen_completed',
  'hyphen_unit_fallback',
])

export function verdictFamily(line: { verdict: string | null; modified: boolean }): VerdictFamily {
  if (line.verdict === 'review_required') return 'review'
  if (line.verdict && REFUSAL_CODES.has(line.verdict)) return 'refused'
  if (line.verdict === 'all_attempts_exhausted') return 'silent'
  return line.modified ? 'kept' : 'silent'
}

export const FAMILY = {
  review: { stroke: '#b45309', fill: 'rgba(245,158,11,0.22)', label: 'À relire' },
  kept: { stroke: '#1d4ed8', fill: 'rgba(29,78,216,0.18)', label: 'Retenue' },
  refused: { stroke: '#b91c1c', fill: 'rgba(185,28,28,0.20)', label: 'Refusée' },
  silent: { stroke: '#a1a1aa', fill: 'rgba(161,161,170,0.10)', label: 'Sans objet' },
} as const
