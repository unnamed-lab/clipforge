"""Time-aware face-cam tracking.

The detector is deliberately cheap, so these tests exercise the layer that turns
noisy per-frame readings into stable, reviewable ranges rather than the detector
itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from clipforge.media.facecam import (
    STATE_MANUAL,
    STATE_MISSING,
    STATE_UNCERTAIN,
    STATE_VISIBLE,
    FaceCamTrack,
    TrackRange,
    ranges_for_span,
    track_facecam,
)


@dataclass
class Sample:
    time_ms: int
    features: dict
    facecam_candidates: list[dict] | None = None


def sample(time_ms: int, rect: tuple[float, float, float, float] | None, *, confidence: float = 0.8) -> Sample:
    features = {
        "facecam_present": 1.0 if rect else 0.0,
        "facecam_confidence": confidence if rect else 0.0,
    }
    if rect:
        features.update(
            {
                "facecam_x": rect[0],
                "facecam_y": rect[1],
                "facecam_width": rect[2],
                "facecam_height": rect[3],
            }
        )
    return Sample(time_ms=time_ms, features=features)


def panel_candidate(
    rect: tuple[float, float, float, float],
    *,
    face: tuple[float, float, float, float] | None = None,
    score: float = 0.72,
    border: float = 0.14,
    interior: float = 0.60,
) -> dict:
    x, y, width, height = rect
    face = face or (x + width * 0.45, y + height * 0.55, width * 0.18, height * 0.32)
    fx, fy, fw, fh = face
    return {
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "area": width * height,
        "score": score,
        "border": border,
        "interior": interior,
        "edge": 0.2,
        "face_inside": 1.0,
        "face_score": 0.9,
        "face_cx": fx + fw / 2,
        "face_cy": fy + fh / 2,
        "face_w": fw,
        "face_h": fh,
    }


def candidate_sample(time_ms: int, *candidates: dict) -> Sample:
    return Sample(time_ms=time_ms, features={}, facecam_candidates=list(candidates))


def _ranges(track: FaceCamTrack) -> list[tuple[int, int, str]]:
    return [(item.start_ms, item.end_ms, item.state) for item in track.ranges]


def test_fixed_corner_plate_produces_one_stable_visible_range():
    # given five seconds of a still webcam in the corner
    samples = [sample(index * 1_000, (0.70, 0.65, 0.25, 0.30)) for index in range(5)]

    # when
    track = track_facecam(samples, 0, 5_000)

    # then
    assert _ranges(track) == [(0, 5_000, STATE_VISIBLE)]
    assert track.ranges[0].rect == (0.70, 0.65, 0.25, 0.30)
    assert track.warnings == []


def test_person_movement_inside_the_plate_does_not_create_a_new_range():
    # given a plate whose detected box moves a few percent between samples
    samples = [
        sample(0, (0.70, 0.65, 0.25, 0.30)),
        sample(1_000, (0.715, 0.655, 0.25, 0.30)),
        sample(2_000, (0.705, 0.645, 0.25, 0.30)),
        sample(3_000, (0.725, 0.660, 0.25, 0.30)),
    ]

    # when
    track = track_facecam(samples, 0, 4_000)

    # then
    assert _ranges(track) == [(0, 4_000, STATE_VISIBLE)]


def test_sustained_move_creates_a_new_range_after_confirmation():
    # given a plate that jumps across the frame and stays there
    samples = [sample(index * 1_000, (0.70, 0.65, 0.25, 0.30)) for index in range(4)]
    samples += [sample((4 + index) * 1_000, (0.05, 0.05, 0.25, 0.30)) for index in range(3)]

    # when
    track = track_facecam(samples, 0, 7_000)

    # then
    assert _ranges(track) == [
        (0, 4_000, STATE_VISIBLE),
        (4_000, 5_000, STATE_UNCERTAIN),
        (5_000, 7_000, STATE_VISIBLE),
    ]
    assert track.ranges[-1].rect == (0.05, 0.05, 0.25, 0.30)


def test_zoom_is_treated_as_a_new_plate_only_after_confirmation():
    # given a webcam that doubles in apparent size
    samples = [sample(index * 1_000, (0.68, 0.62, 0.26, 0.30)) for index in range(3)]
    samples += [sample((3 + index) * 1_000, (0.56, 0.46, 0.40, 0.46)) for index in range(3)]

    # when
    track = track_facecam(samples, 0, 6_000)

    # then
    assert track.ranges[-1].state == STATE_VISIBLE
    assert track.ranges[-1].rect is not None
    assert track.ranges[-1].rect[2] > 0.35


def test_single_missing_sample_keeps_the_last_plate_as_uncertain():
    # given one dropped detection between visible samples
    samples = [
        sample(0, (0.70, 0.65, 0.25, 0.30)),
        sample(1_000, None),
        sample(2_000, (0.70, 0.65, 0.25, 0.30)),
    ]

    # when
    track = track_facecam(samples, 0, 3_000)

    # then
    states = [item.state for item in track.ranges]
    assert STATE_UNCERTAIN in states
    uncertain = next(item for item in track.ranges if item.state == STATE_UNCERTAIN)
    assert uncertain.rect == (0.70, 0.65, 0.25, 0.30)


def test_two_missing_samples_end_the_face_cam_range():
    # given a webcam that disappears and does not return
    samples = [sample(index * 1_000, (0.70, 0.65, 0.25, 0.30)) for index in range(3)]
    samples += [sample((3 + index) * 1_000, None) for index in range(3)]

    # when
    track = track_facecam(samples, 0, 6_000)

    # then
    assert _ranges(track) == [
        (0, 3_000, STATE_VISIBLE),
        (3_000, 4_000, STATE_UNCERTAIN),
        (4_000, 6_000, STATE_MISSING),
    ]
    assert track.ranges[-1].rect is None
    assert any("disappears" in warning for warning in track.warnings)


def test_low_confidence_readings_are_not_treated_as_a_face_cam():
    # given a skin-coloured patch the detector is not sure about
    samples = [sample(index * 1_000, (0.70, 0.65, 0.25, 0.30), confidence=0.2) for index in range(4)]

    # when
    track = track_facecam(samples, 0, 4_000)

    # then
    assert all(item.state == STATE_MISSING for item in track.ranges)
    assert all(item.rect is None for item in track.ranges)
    assert any("No face-cam" in warning for warning in track.warnings)


def test_manual_override_wins_for_its_exact_time_range():
    # given a tracked plate with a user correction in the middle
    samples = [sample(index * 1_000, (0.70, 0.65, 0.25, 0.30)) for index in range(6)]
    override = {
        "start_ms": 2_000,
        "end_ms": 4_000,
        "rect": {"x_pct": 0.08, "y_pct": 0.10, "width_pct": 0.20, "height_pct": 0.22},
        "reason": "Camera moved to the left",
    }

    # when
    track = track_facecam(samples, 0, 6_000, overrides=[override])

    # then
    manual = [item for item in track.ranges if item.state == STATE_MANUAL]
    assert len(manual) == 1
    assert (manual[0].start_ms, manual[0].end_ms) == (2_000, 4_000)
    assert manual[0].rect == (0.08, 0.10, 0.20, 0.22)
    assert manual[0].reason == "Camera moved to the left"


def test_ranges_for_span_intersects_without_losing_the_plate():
    # given a tracked range that starts before the output segment
    track = FaceCamTrack(
        ranges=[
            TrackRange(
                start_ms=0,
                end_ms=10_000,
                state=STATE_VISIBLE,
                rect=(0.7, 0.6, 0.2, 0.25),
                confidence=0.8,
                reason="stable",
            )
        ]
    )

    # when
    ranges = ranges_for_span(track, 3_000, 7_000)

    # then
    assert len(ranges) == 1
    assert (ranges[0].start_ms, ranges[0].end_ms) == (3_000, 7_000)
    assert ranges[0].rect == (0.7, 0.6, 0.2, 0.25)


def test_empty_window_reports_missing_instead_of_inventing_a_plate():
    # given / when
    track = track_facecam([], 0, 5_000)

    # then
    assert _ranges(track) == [(0, 5_000, STATE_MISSING)]
    assert any("No face-cam samples" in warning for warning in track.warnings)


def test_unrelated_in_game_face_does_not_replace_the_accepted_webcam_panel():
    # given a streamer panel and a stronger-looking game face elsewhere
    streamer = (0.02, 0.32, 0.19, 0.22)
    game_face = (0.58, 0.08, 0.20, 0.24)
    samples = [
        candidate_sample(index * 1_000, panel_candidate(streamer))
        for index in range(3)
    ]
    samples += [
        candidate_sample(
            (3 + index) * 1_000,
            panel_candidate(streamer, score=0.30, border=0.02, face=(0.08, 0.41, 0.03, 0.07)),
            panel_candidate(game_face, score=0.92, border=0.20, face=(0.60, 0.12, 0.16, 0.20)),
        )
        for index in range(4)
    ]

    # when
    track = track_facecam(samples, 0, 7_000)

    # then
    assert _ranges(track) == [(0, 7_000, STATE_VISIBLE)]
    rect = track.ranges[0].rect
    assert rect is not None
    assert abs(rect[0] - 0.02) < 0.03 and abs(rect[1] - 0.32) < 0.03
    assert rect[2] < 0.24


def test_full_screen_camera_face_is_not_accepted_as_an_embedded_webcam():
    # given a large face with no compact, distinct panel evidence
    full_screen = panel_candidate(
        (0.20, 0.25, 0.55, 0.55),
        face=(0.40, 0.36, 0.18, 0.30),
        score=0.62,
        border=0.04,
        interior=0.12,
    )
    samples = [candidate_sample(index * 1_000, full_screen) for index in range(4)]

    # when
    track = track_facecam(samples, 0, 4_000)

    # then
    assert _ranges(track) == [(0, 4_000, STATE_MISSING)]


def test_webcam_panel_survives_a_brief_face_loss_when_the_boundary_remains():
    # given an accepted panel, then a frame with the panel boundary but no face
    panel = (0.02, 0.32, 0.19, 0.22)
    boundary_only = {
        **panel_candidate(panel, score=0.55, border=0.20),
        "face_inside": 0.0,
        "face_score": 0.0,
        "face_cx": 0.0,
        "face_cy": 0.0,
        "face_w": 0.0,
        "face_h": 0.0,
    }
    samples = [candidate_sample(index * 1_000, panel_candidate(panel)) for index in range(2)]
    samples.append(candidate_sample(2_000, boundary_only))
    samples += [candidate_sample((3 + index) * 1_000, panel_candidate(panel)) for index in range(2)]

    # when
    track = track_facecam(samples, 0, 5_000)

    # then
    assert any(item.state == STATE_UNCERTAIN for item in track.ranges)
    assert all(item.rect is not None for item in track.ranges)
    assert all(abs(item.rect[0] - panel[0]) < 0.03 for item in track.ranges if item.rect)


def test_a_genuinely_larger_bordered_panel_is_adopted_after_persistence():
    # given a small plate that disappears and a bordered larger plate
    small = (0.02, 0.32, 0.19, 0.22)
    large = (0.04, 0.18, 0.36, 0.38)
    samples = [candidate_sample(index * 1_000, panel_candidate(small)) for index in range(3)]
    samples += [
        candidate_sample(
            (3 + index) * 1_000,
            panel_candidate(
                large,
                face=(0.12, 0.28, 0.08, 0.12),
                score=0.68,
                border=0.12,
                interior=0.55,
            ),
        )
        for index in range(5)
    ]

    # when
    track = track_facecam(samples, 0, 8_000)

    # then
    assert track.ranges[-1].state == STATE_VISIBLE
    assert track.ranges[-1].rect is not None
    assert track.ranges[-1].rect[2] > 0.30


def test_track_from_region_seed_keeps_the_user_rectangle_as_its_start():
    # given a user seed and a tracked panel that stays compatible
    panel = (0.05, 0.30, 0.20, 0.23)
    samples = [candidate_sample(index * 1_000, panel_candidate(panel)) for index in range(4)]
    override = {
        "start_ms": 0,
        "end_ms": 4_000,
        "mode": "track",
        "rect": {"x_pct": 0.05, "y_pct": 0.30, "width_pct": 0.20, "height_pct": 0.23},
        "reason": "seed",
    }

    # when
    track = track_facecam(samples, 0, 4_000, overrides=[override])

    # then
    assert track.ranges[0].state == STATE_VISIBLE
    assert track.ranges[0].rect is not None
    assert abs(track.ranges[0].rect[0] - 0.05) < 0.02
