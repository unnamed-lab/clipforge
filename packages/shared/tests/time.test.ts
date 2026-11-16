import { describe, expect, it } from "vitest";

import { formatBytes, formatDuration, formatTimecode, parseTimecodeInput, scoreTone } from "../src/time";

describe("timecode formatting", () => {
  it("formats minutes and seconds without an hour field", () => {
    expect(formatTimecode(65_000)).toBe("1:05");
  });

  it("formats hours when present", () => {
    expect(formatTimecode(3_725_000)).toBe("1:02:05");
  });

  it("clamps negative values to zero", () => {
    expect(formatTimecode(-500)).toBe("0:00");
  });
});

describe("timecode parsing", () => {
  it("parses plain seconds", () => {
    expect(parseTimecodeInput("12.5")).toBe(12_500);
  });

  it("parses mm:ss and hh:mm:ss", () => {
    expect(parseTimecodeInput("1:05")).toBe(65_000);
    expect(parseTimecodeInput("1:02:05")).toBe(3_725_000);
  });

  it("rejects malformed input", () => {
    expect(parseTimecodeInput("1:xx")).toBeNull();
    expect(parseTimecodeInput("")).toBeNull();
  });
});

describe("human-readable units", () => {
  it("formats durations across unit boundaries", () => {
    expect(formatDuration(900)).toBe("900 ms");
    expect(formatDuration(65_000)).toBe("1m 5s");
  });

  it("formats bytes and treats absent values as unknown", () => {
    expect(formatBytes(1_536)).toBe("1.5 KB");
    expect(formatBytes(null)).toBe("—");
  });
});

describe("scoreTone", () => {
  it("maps scores to three bands", () => {
    expect(scoreTone(80)).toBe("high");
    expect(scoreTone(60)).toBe("mid");
    expect(scoreTone(10)).toBe("low");
  });
});
