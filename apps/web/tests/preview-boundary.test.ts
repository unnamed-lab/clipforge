import { describe, expect, it } from "vitest";

import {
  PLAYBACK_END_TOLERANCE_MS,
  clipEndReached,
  playbackStartMs,
} from "../src/lib/preview-boundary";

describe("preview boundary", () => {
  it("treats the tolerance window at the out point as the end", () => {
    // given
    const end = 80_000;

    // when / then
    expect(clipEndReached(end - PLAYBACK_END_TOLERANCE_MS - 1, end)).toBe(false);
    expect(clipEndReached(end - PLAYBACK_END_TOLERANCE_MS, end)).toBe(true);
    expect(clipEndReached(end + 500, end)).toBe(true);
  });

  it("replays from the in point after the selection has ended", () => {
    // given
    const start = 130_000;
    const end = 200_000;

    // when / then
    expect(playbackStartMs(end, start, end)).toBe(start);
    expect(playbackStartMs(end + 1_000, start, end)).toBe(start);
  });

  it("continues from the current time while inside the selection", () => {
    // given
    const start = 130_000;
    const end = 200_000;

    // when / then
    expect(playbackStartMs(160_000, start, end)).toBe(160_000);
  });

  it("replays when the playhead was scrubbed before the in point", () => {
    // given / when / then
    expect(playbackStartMs(100_000, 130_000, 200_000)).toBe(130_000);
  });
});
