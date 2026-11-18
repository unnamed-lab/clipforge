/** Playback boundary helpers for the source-video preview. */

export const PLAYBACK_END_TOLERANCE_MS = 80;

export function clipEndReached(
  currentMs: number,
  endMs: number,
  toleranceMs = PLAYBACK_END_TOLERANCE_MS,
): boolean {
  return currentMs >= Math.max(0, endMs - toleranceMs);
}

export function clipStartReached(
  currentMs: number,
  startMs: number,
  toleranceMs = PLAYBACK_END_TOLERANCE_MS,
): boolean {
  return currentMs < startMs - toleranceMs;
}

/** Resume inside the selection, or replay from its in point when outside/ended. */
export function playbackStartMs(
  currentMs: number,
  startMs: number,
  endMs: number,
  toleranceMs = PLAYBACK_END_TOLERANCE_MS,
): number {
  if (clipStartReached(currentMs, startMs, toleranceMs) || clipEndReached(currentMs, endMs, toleranceMs)) {
    return startMs;
  }
  return currentMs;
}
