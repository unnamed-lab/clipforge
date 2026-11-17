# Video skills integration audit

Date: 2026-11-17

Scope: candidate agent-skill repositories and media tools reviewed while building
the editorial and render pipeline. "Skill" here means an agent instruction bundle
that a coding harness can load. A skill is not a Clipforge runtime service, and
installing one does not add a second media pipeline.

## 1. Decision summary

| Candidate | Type | Licence | Cost | Integration decision |
| --- | --- | --- | --- | --- |
| `iart-ai/tiktok-video-skills` | Agent skill bundle / plugin | MIT | Free | Installed globally for agent guidance. Patterns only; no runtime dependency. |
| `kajisho5/ffmpeg-skill` | Agent skill with scripts/MCP | MIT | Free | Reviewed. No runtime dependency; Clipforge keeps its own FFmpeg renderer. |
| `WyattBlue/auto-editor` | Standalone executable + skill | Unlicense | Free | Optional future subprocess adapter only; never a silent second pass over manual edits. |
| `Breakthrough/PySceneDetect` | Python library / CLI | BSD-3-Clause | Free | Optional scene-analysis candidate. Not installed in the worker venv. |

No candidate is a mandatory paid dependency. No candidate is allowed to replace
the existing FFmpeg render service, the editorial planner, or the quality gate.

## 2. TikTok Video Skills

- Repository: `iart-ai/tiktok-video-skills`.
- Purpose: short-form video guidance, including hooks, captions, lower thirds,
  countdowns and related packaging patterns.
- Type: agent skill bundle / plugin. It provides instructions and helper assets,
  not a hosted API.
- Dependencies: the skill bundle itself; any example tooling is optional and was
  not added to the worker dependency set.
- Install: installed globally under the agent skills directory and exposed by the
  harness as `short-form-video`, `caption-animation`, `countdown-video` and
  `lower-thirds`.
- API: none for Clipforge. There is no runtime import and no network call.
- Overlap: editorial and caption guidance overlaps the Clipforge editorial
  planner and subtitle renderer. Those remain Clipforge-owned because they must
  produce deterministic plans and render specs.
- Verification: activation was checked by listing globally installed skills. This
  audit does not claim that a live TikTok trend or publishing API was tested.
- Use: patterns for hook pacing, caption timing and platform-safe framing.

## 3. FFmpeg Skill

- Repository: `kajisho5/ffmpeg-skill`.
- Purpose: FFmpeg command guidance and reusable media recipes.
- Type: agent skill with scripts and optional MCP surface.
- Licence: MIT.
- Dependencies: the skill's scripts; explicit cloning/fetching repeatedly failed
  in this environment with `RPC failed; curl 18 transfer closed with outstanding
  read data remaining` / early EOF. The upstream licence was verified from the
  repository's raw licence file.
- Install: not made a runtime dependency. The worker already ships a tested
  FFmpeg/FFprobe renderer.
- API: none imported by Clipforge.
- Overlap: high at the command level. Clipforge must retain one renderer because
  composition, caption geometry, EDL and quality gates are tightly coupled to it.
- Use: reference patterns only. No second command builder was added.

## 4. Auto-Editor

- Repository: `WyattBlue/auto-editor`.
- Purpose: remove silence and dead space and produce an edited timeline.
- Type: standalone executable (Nim) plus an agent skill family.
- Licence: The Unlicense.
- Cost: free and local.
- Install: the skill family is installed globally and exposed by the harness
  (`auto-editor`, `auto-editor-effects`, `auto-editor-export`,
  `auto-editor-transcribe`, `auto-editor-transitions`). The executable was not
  added as a mandatory worker dependency.
- API: command line and timeline files. A future adapter would call it as an
  optional subprocess and parse its output into Clipforge's own timing plan.
- Overlap: medium. Clipforge already removes only the middle of long pauses and
  preserves manual edits.
- Integration rule: never run it as a silent second pass over a user's manual
  trim. Any adapter must be explicit, opt-in, produce a reviewable timing plan,
  and be covered by regression tests.
- Current status: not wired into the worker. No claim of runtime support.

## 5. PySceneDetect

- Repository: `Breakthrough/PySceneDetect`.
- Purpose: shot and scene boundary detection.
- Type: Python library and CLI.
- Licence: BSD-3-Clause.
- Cost: free and local.
- Install: not installed in the worker virtual environment. The existing scene
  classifier uses FFmpeg and cheap visual features.
- API: Python API and CLI; an optional adapter would map detected cuts into the
  existing scene evidence model.
- Overlap: high for boundary review. It must not replace the existing review or
  silently change a user's ending.
- Integration rule: optional analysis provider, behind a capability flag, with
  fixtures and no large model downloads. It must never be required for ordinary
  CI.

## 6. Required fonts

The renderer supports the requested families through bundled, redistributable
substitutes. The exact mapping and notices live in
`apps/worker/src/clipforge/rendering/fonts/NOTICE.md`:

| Requested | Bundled substitute |
| --- | --- |
| Montserrat | Montserrat Bold/Black |
| SF Pro Display | Inter |
| The Bold Font | Archivo Black |
| Bebas Neue | Bebas Neue |
| Helvetica / Arial | Lato Bold |
| Impact | Anton |

All bundled files are SIL Open Font License 1.1. Proprietary fonts are not
redistributed. The renderer records the requested name, the actual family and
whether a substitution occurred.

## 7. Quality boundaries

- One media pipeline: FFmpeg via `clipforge.rendering`.
- Skills may inform implementation but cannot become hidden runtime services.
- Every rendered output is probed; exit codes alone are not evidence.
- No long videos or external network calls in ordinary CI.
- No fake controls for montage, multi-camera, reaction synthesis or J/L cuts.
- No claim that an agent skill is "integrated" until its activation and its
  actual output path are verified.

## 8. Tests and verification

- Global skill activation was checked with the skills CLI.
- Worker tests cover composition, captions, EDL, quality gates and editorial
  timelines; the full worker suite passed after the editorial changes.
- Web typecheck, lint, shared tests and web tests were run separately. The web
  production build was attempted but did not complete in this environment before
  the shell restarted; it is not reported as a pass.
- No live TikTok, YouTube, Instagram or Auto-Editor integration test is claimed.

## 9. Open work

- Decide whether PySceneDetect should become an optional scene-evidence provider.
- Decide whether Auto-Editor should become an explicit silence-removal adapter.
- Re-verify FFmpeg Skill and any upstream scripts before copying code.
- Re-run the web production build in a clean environment.
- Record the final integration status here after any adapter is actually wired.
