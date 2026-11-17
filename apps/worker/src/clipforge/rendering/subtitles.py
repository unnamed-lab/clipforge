"""Measured ASS subtitle generation with per-word highlighting.

Captions are laid out with the real bundled font metrics, not character counts.
A chunk is never truncated: if a phrase is too wide it is split at a natural
boundary, then the whole span is reduced within ``min_font_scale`` before a
review warning is raised.

There is exactly one event per word so the highlight is exact, but each event
keeps the whole chunk on screen until the next word starts. This avoids the
one-frame gaps that a naive word-start/word-end event pair creates.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from clipforge.rendering.fonts import FontSelection, resolve_font, text_width
from clipforge.rendering.spec import CaptionSettings, CompositionDecision, RenderSpec

ALIGNMENT = {
    ("bottom", "left"): 1, ("bottom", "center"): 2, ("bottom", "right"): 3,
    ("center", "left"): 4, ("center", "center"): 5, ("center", "right"): 6,
    ("top", "left"): 7, ("top", "center"): 8, ("top", "right"): 9,
}
CHUNK_GAP_MS = 700
CHUNK_TAIL_MS = 160
LINE_HEIGHT = 1.16
MIN_CHUNK_WORDS = 2
STRONG_BREAKS = (".", "!", "?", "…")
SOFT_BREAKS = (",", ";", ":")


@dataclass
class CaptionWord:
    start_ms: int
    end_ms: int
    text: str


@dataclass
class CaptionChunk:
    words: list[CaptionWord]
    lines: list[list[str]]
    font_size: int
    margin_v: int
    width_px: float
    block_height_px: float
    overflow: bool = False


@dataclass
class CaptionReport:
    used: bool = False
    font: dict = field(default_factory=dict)
    chunks: int = 0
    lines: int = 0
    max_line_width_px: float = 0.0
    max_line_width_limit_px: float = 0.0
    max_block_height_px: float = 0.0
    bounds_ok: bool = True
    warnings: list[str] = field(default_factory=list)
    bands: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "used": self.used,
            "font": self.font,
            "chunks": self.chunks,
            "lines": self.lines,
            "max_line_width_px": round(self.max_line_width_px, 1),
            "max_line_width_limit_px": round(self.max_line_width_limit_px, 1),
            "max_block_height_px": round(self.max_block_height_px, 1),
            "bounds_ok": self.bounds_ok,
            "warnings": self.warnings,
            "bands": self.bands,
        }


@dataclass
class CaptionBuild:
    ass: str
    srt: str
    report: CaptionReport


def _ass_color(hex_colour: str) -> str:
    value = hex_colour.lstrip("#")
    red, green, blue = value[0:2], value[2:4], value[4:6]
    return f"&H00{blue}{green}{red}".upper()


def _ass_time(ms: int) -> str:
    ms = max(0, int(ms))
    hours, remainder = divmod(ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    centis = millis // 10
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centis:02d}"


def _escape(text: str) -> str:
    cleaned = text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")
    return cleaned.replace("\n", " ").strip()


def _scaled(value: float, reference: float) -> int:
    return max(1, int(round(value * reference / 1080)))


def _band(composition: CompositionDecision | None, captions: CaptionSettings) -> tuple[float, float]:
    if composition and captions.avoid_visual_overlap:
        return composition.caption_band.top_pct, composition.caption_band.bottom_pct
    if captions.position == "top":
        return 0.04, 0.34
    if captions.position == "center":
        return 0.34, 0.66
    return 0.62, 0.90


def _wrap(
    words: list[CaptionWord],
    font: FontSelection,
    size: int,
    max_width: float,
    max_words_per_line: int,
    uppercase: bool,
    max_chars_per_line: int = 0,
) -> tuple[list[list[str]], list[float]]:
    """Greedy measured wrap. Never drops or truncates a word."""
    lines: list[list[str]] = []
    widths: list[float] = []
    current: list[str] = []
    current_chars = 0
    for word in words:
        token = word.text.upper() if uppercase else word.text
        candidate = current + [token]
        candidate_text = " ".join(candidate)
        too_many_words = len(candidate) > max_words_per_line
        too_many_chars = bool(max_chars_per_line) and current_chars + len(token) > max_chars_per_line
        too_wide = text_width(candidate_text, font, size) > max_width + 0.5
        if current and (too_many_words or too_many_chars or too_wide):
            lines.append(current)
            widths.append(text_width(" ".join(current), font, size))
            current = [token]
            current_chars = len(token)
        else:
            current = candidate
            current_chars += len(token) + (1 if len(current) > 1 else 0)
    if current:
        lines.append(current)
        widths.append(text_width(" ".join(current), font, size))
    return lines, widths


def _required_size(
    words: list[CaptionWord],
    font: FontSelection,
    base_size: int,
    min_size: int,
    max_width: float,
    captions: CaptionSettings,
) -> tuple[int, list[list[str]], list[float]]:
    size = base_size
    while size >= min_size:
        lines, widths = _wrap(
            words, font, size, max_width, captions.max_words_per_line,
            captions.uppercase, captions.max_chars_per_line,
        )
        if len(lines) <= captions.max_lines and max(widths or [0]) <= max_width + 0.5:
            return size, lines, widths
        if size == min_size:
            return size, lines, widths
        size = max(min_size, size - 2)
    lines, widths = _wrap(
        words, font, min_size, max_width, captions.max_words_per_line,
        captions.uppercase, captions.max_chars_per_line,
    )
    return min_size, lines, widths


def _ends_phrase(text: str) -> int:
    stripped = text.rstrip()
    if stripped.endswith(STRONG_BREAKS):
        return 2
    if stripped.endswith(SOFT_BREAKS):
        return 3
    return 0


def _projected_cps(current: list[CaptionWord], word: CaptionWord) -> float:
    start = current[0].start_ms
    end = max(word.end_ms, current[-1].end_ms)
    seconds = max(0.25, (end - start) / 1000)
    chars = sum(len(item.text) + 1 for item in current) + len(word.text)
    return chars / seconds


def _chunk(
    words: list[CaptionWord],
    captions: CaptionSettings,
    font: FontSelection,
    base_size: int,
    max_width: float,
) -> list[list[CaptionWord]]:
    capacity = max(1, captions.max_words_per_line * captions.max_lines)
    char_capacity = captions.max_chars_per_line * captions.max_lines if captions.max_chars_per_line else 0
    chunks: list[list[CaptionWord]] = []
    current: list[CaptionWord] = []
    current_chars = 0
    for word in words:
        if current:
            gap = word.start_ms - current[-1].end_ms
            lines, _ = _wrap(
                current + [word], font, base_size, max_width, captions.max_words_per_line,
                captions.uppercase, captions.max_chars_per_line,
            )
            phrase_break = _ends_phrase(current[-1].text)
            too_fast = bool(captions.reading_speed_cps) and _projected_cps(current, word) > captions.reading_speed_cps
            should_break = (
                gap >= CHUNK_GAP_MS
                or len(current) >= capacity
                or len(lines) > captions.max_lines
                or (char_capacity and current_chars + len(word.text) > char_capacity)
                or (phrase_break and len(current) >= phrase_break)
                or (too_fast and len(current) >= MIN_CHUNK_WORDS)
            )
            if should_break:
                chunks.append(current)
                current = []
                current_chars = 0
        current.append(word)
        current_chars += len(word.text) + 1
    if current:
        chunks.append(current)
    return chunks


def _place(
    chunk: list[CaptionWord],
    lines: list[list[str]],
    size: int,
    captions: CaptionSettings,
    height: int,
    band: tuple[float, float],
) -> tuple[int, float, bool]:
    line_height = max(1, int(round(size * LINE_HEIGHT)))
    block = len(lines) * line_height + max(4, int(round(captions.outline_width * 2)))
    padding = max(6, int(round(height * 0.008)))
    band_top = int(round(band[0] * height))
    band_bottom = int(round(band[1] * height))
    margin = _scaled(captions.margin_v, height)
    overflow = False

    if captions.position == "bottom":
        block_bottom = height - margin
        block_top = block_bottom - block
        if block_top < band_top + padding:
            block_top = band_top + padding
            block_bottom = block_top + block
            margin = height - block_bottom
        if block_bottom > band_bottom - padding:
            block_bottom = band_bottom - padding
            margin = height - block_bottom
            block_top = block_bottom - block
            if block_top < band_top:
                overflow = True
                block_top = band_top + padding
                block_bottom = block_top + block
                margin = height - block_bottom
        margin = max(0, int(round(margin)))
    elif captions.position == "top":
        margin = band_top + padding
        if margin + block > band_bottom - padding:
            margin = max(band_top + padding, band_bottom - padding - block)
            overflow = margin + block > band_bottom
    else:
        margin = 0
        overflow = block > (band_bottom - band_top)

    return margin, block, overflow


def _render_event(
    chunk: CaptionChunk,
    active_index: int,
    settings: CaptionSettings,
    start_ms: int,
    end_ms: int,
    base_size: int,
) -> str:
    highlight = _ass_color(settings.highlight_color)
    primary = _ass_color(settings.primary_color)
    effect = ""
    if settings.animation == "fade":
        effect = "{\\fad(80,80)}"
    elif settings.animation == "pop":
        effect = "{\\fscx86\\fscy86\\t(0,110,\\fscx100\\fscy100)}"
    if chunk.font_size != base_size:
        effect += f"{{\\fs{chunk.font_size}}}"
    flat_index = 0
    rendered_lines: list[str] = []
    for line in chunk.lines:
        pieces: list[str] = []
        for token in line:
            escaped = _escape(token.upper() if settings.uppercase else token)
            if settings.highlight == "word" and flat_index == active_index:
                pieces.append(f"{{\\c{highlight}}}{escaped}{{\\c{primary}}}")
            else:
                pieces.append(escaped)
            flat_index += 1
        rendered_lines.append(" ".join(pieces))
    text = effect + "\\N".join(rendered_lines)
    return (
        f"Dialogue: 0,{_ass_time(start_ms)},{_ass_time(end_ms)},Caption,,0,0,"
        f"{chunk.margin_v},,{text}"
    )


def _word_stream(raw_words: list[dict], span_start: int, span_end: int, offset: int) -> list[CaptionWord]:
    words: list[CaptionWord] = []
    for raw in raw_words:
        begin = max(span_start, int(raw.get("start_ms", span_start)))
        finish = min(span_end, int(raw.get("end_ms", begin + 120)))
        text = str(raw.get("word", "")).strip()
        if not text or finish <= begin:
            continue
        words.append(
            CaptionWord(
                start_ms=offset + (begin - span_start),
                end_ms=offset + max(begin - span_start + 60, finish - span_start),
                text=text,
            )
        )
    return words


def build_captions(
    spans: list[tuple[int, int]],
    words_per_span: list[list[dict]],
    spec: RenderSpec,
    width: int,
    height: int,
    compositions: list[CompositionDecision | None] | None = None,
) -> CaptionBuild:
    captions = spec.captions
    base_size = max(18, int(round(captions.font_size * height / 1080)))
    min_size = max(18, int(round(base_size * captions.min_font_scale)))
    margin_h = max(8, int(round(captions.margin_h * width / 1080)))
    max_width = min(width * captions.max_line_width_pct, width - 2 * margin_h)

    font = resolve_font(captions.font_family, weight=captions.weight, bold=captions.bold)
    outline = max(0.0, captions.outline_width * (height / 1080))
    border_style = 3 if captions.background else 1
    if captions.background:
        alpha = int(round((100 - captions.background_opacity) / 100 * 255))
        back_colour = f"&H{alpha:02X}000000"
    else:
        back_colour = "&HFF000000"
    align = ALIGNMENT[(captions.position, captions.alignment)]
    bold_flag = -1 if font.file == "Inter-Variable.ttf" and font.weight >= 700 else 0

    header = [
        "[Script Info]",
        "; Generated by Clipforge",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "; 0 lets libass wrap only when a measured line still overflows",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Caption,{font},{size},{primary},&H00FFFFFF,{outline_col},{back},"
        "{bold},0,0,0,100,100,0,0,{border},{outline:.1f},{shadow:.1f},{align},"
        "{margin_h},{margin_h},{margin},{encoding}".format(
            font=font.family,
            size=base_size,
            primary=_ass_color(captions.primary_color),
            outline_col=_ass_color(captions.outline_color),
            back=back_colour,
            bold=bold_flag,
            border=border_style,
            outline=outline,
            shadow=captions.shadow,
            align=align,
            margin_h=margin_h,
            margin=max(0, _scaled(captions.margin_v, height)),
            encoding=1,
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    events: list[str] = []
    srt_blocks: list[str] = []
    srt_index = 1
    report = CaptionReport(
        used=False,
        font={
            "requested": font.requested,
            "family": font.family,
            "file": font.file,
            "substituted": font.substituted,
            "note": font.note,
        },
        max_line_width_limit_px=max_width,
    )
    if font.substituted:
        report.warnings.append(
            f"{font.requested} is not bundled; {font.family} is used as the redistributable substitute."
        )

    timeline_cursor = 0
    for index, ((span_start, span_end), raw_words) in enumerate(zip(spans, words_per_span)):
        span_length = max(0, span_end - span_start)
        composition = compositions[index] if compositions and index < len(compositions) else None
        band = _band(composition, captions)
        words = _word_stream(raw_words, span_start, span_end, timeline_cursor)
        if words:
            report.used = True
            report.bands.append(
                {
                    "start_ms": timeline_cursor,
                    "end_ms": timeline_cursor + span_length,
                    "top_pct": band[0],
                    "bottom_pct": band[1],
                    "mode": composition.mode if composition else "global",
                }
            )
            chunks = _chunk(words, captions, font, base_size, max_width)
            required = [
                _required_size(chunk, font, base_size, min_size, max_width, captions)[0]
                for chunk in chunks
            ]
            span_size = min(required) if required else base_size

            for chunk_words in chunks:
                size, lines, widths = _required_size(
                    chunk_words, font, span_size, min_size, max_width, captions
                )
                if len(lines) > captions.max_lines:
                    size, lines, widths = _required_size(
                        chunk_words, font, min_size, min_size, max_width, captions
                    )
                margin, block, overflow = _place(
                    chunk_words, lines, size, captions, height, band
                )
                width_px = max(widths or [0.0])
                if width_px > max_width + 0.5:
                    overflow = True
                    long_word = max(chunk_words, key=lambda item: len(item.text))
                    report.warnings.append(
                        f"Caption word '{long_word.text}' is wider than the safe text area even at "
                        f"{size}px; libass may wrap it."
                    )
                if block > (band[1] - band[0]) * height:
                    report.warnings.append(
                        "The caption block is taller than the safe band left by the picture; "
                        "captions are placed over the picture edge."
                    )
                chunk = CaptionChunk(
                    words=chunk_words,
                    lines=lines,
                    font_size=size,
                    margin_v=margin,
                    width_px=width_px,
                    block_height_px=block,
                    overflow=overflow,
                )
                chunk_start = chunk.words[0].start_ms
                chunk_end = min(timeline_cursor + span_length, chunk.words[-1].end_ms + CHUNK_TAIL_MS)
                if chunk_end <= chunk_start:
                    continue
                report.chunks += 1
                report.lines += len(lines)
                report.max_line_width_px = max(report.max_line_width_px, width_px)
                report.max_block_height_px = max(report.max_block_height_px, block)
                if overflow:
                    report.bounds_ok = False
                for word_index, word in enumerate(chunk.words):
                    start = max(chunk_start, word.start_ms)
                    if len(chunk.words) == 1:
                        end = max(start + 60, word.end_ms)
                    elif word_index + 1 < len(chunk.words):
                        end = max(start + 60, chunk.words[word_index + 1].start_ms)
                    else:
                        end = chunk_end
                    end = min(end, chunk_end)
                    if end <= start:
                        continue
                    events.append(
                        _render_event(chunk, word_index, captions, start, end, span_size)
                    )
                srt_begin = chunk_start
                srt_end = max(srt_begin + 200, chunk_end)
                text = " ".join(word.text for word in chunk.words)
                srt_blocks.append(f"{srt_index}\n{_srt_time(srt_begin)} --> {_srt_time(srt_end)}\n{text}\n")
                srt_index += 1
        timeline_cursor += span_length

    if report.max_line_width_px > width:
        report.bounds_ok = False
    report.warnings = list(dict.fromkeys(report.warnings))
    return CaptionBuild(
        ass="\n".join(header + events) + "\n",
        srt="\n".join(srt_blocks),
        report=report,
    )


def build_ass(
    spans: list[tuple[int, int]],
    words_per_span: list[list[dict]],
    spec: RenderSpec,
    width: int,
    height: int,
    compositions: list[CompositionDecision | None] | None = None,
) -> str:
    return build_captions(spans, words_per_span, spec, width, height, compositions).ass


def _srt_time(ms: int) -> str:
    ms = max(0, int(ms))
    hours, remainder = divmod(ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def build_srt(
    spans: list[tuple[int, int]],
    words_per_span: list[list[dict]],
    spec: RenderSpec | None = None,
    width: int = 1080,
    height: int = 1920,
    compositions: list[CompositionDecision | None] | None = None,
) -> str:
    """Sidecar subtitles on the assembled timeline (no styling)."""
    if spec is not None:
        return build_captions(spans, words_per_span, spec, width, height, compositions).srt
    blocks: list[str] = []
    cursor = 0
    index = 1
    for (span_start, span_end), raw_words in zip(spans, words_per_span):
        span_length = max(0, span_end - span_start)
        words = _word_stream(raw_words, span_start, span_end, cursor)
        for chunk_start in range(0, len(words), 8):
            chunk = words[chunk_start : chunk_start + 8]
            if not chunk:
                continue
            begin = chunk[0].start_ms
            end = max(begin + 200, chunk[-1].end_ms)
            text = " ".join(word.text for word in chunk)
            blocks.append(f"{index}\n{_srt_time(begin)} --> {_srt_time(end)}\n{text}\n")
            index += 1
        cursor += span_length
    return "\n".join(blocks)
