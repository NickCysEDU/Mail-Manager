# Handoff

Notes for whoever works on this next: where things are, what breaks if it is
forgotten, how to measure, and what is open. The [handbook](HANDBOOK.md)
describes the app itself.

## Where things are

- **Mail:** `imap_engine.py` (IMAP), `rules_engine.py` and `rulesets.py` (the
  built-in sorter), `llm_engine.py` and `providers.py` (models), `models.py`
  (routing, no Qt), `gui.py`, `triage_table.py`, `settings_dialog.py`.
- **Links and updates:** `link_open.py`; `updates.py` (no Qt) and
  `update_dialog.py`.
- **Touch Bar:** `touchbar.py` (items bound to Qt controls, one bar per
  window, no AppKit) and `touchbar_mac.py` (AppKit through ctypes). Each
  window builds its own in `_give_touch_bar`; any other dialog gets its pages
  and buttons when it comes forward.
- **Attachments and the visualiser:** `attachment_view.py` (the window),
  `attachment_widgets.py` (the picture, `Spectrum`, and the GPU canvas),
  `attachment_audio.py` (analysis, in worker processes), `visualizers.py`
  (scenes; `Rider` is the game), `rider_gl.py` (the game's lit world),
  `rider_sound.py`, `rider_layout.py`, `rider_bests.py`, `trackstyle.py`
  (tempo, beat, downbeat, sections), `harmony.py` (key and chords),
  `beat_clock.py`, `beatmap.py`, `av_sync.py`.

## Rules that break things if forgotten

### Everywhere

- **Every shortcut is unique.** Qt fires neither of two actions bound to the
  same keys. `test_no_two_actions_share_a_shortcut` checks the main window.
- **A self-test line that reports a problem must raise.** `check()` only fails
  on an exception, and `build_app.sh` only refuses to ship on a failure.
- **Never assert on a menu by calling `exec`.** It enters a native modal loop
  that the suite's timeout cannot interrupt.
- **Progress from a worker arrives by queued delivery,** which a test with no
  event loop never sees.
- **Brace shell variables next to curly quotes:** `${IDENTITY}`, never
  `$IDENTITY”`.
- **Before adding a phrase to the sorter, ablate it.** Most phrases written
  for one adversarial set changed nothing; the general ones did the work.
- **Anonymising is checked by re-scoring.** If a score moves, a signal was
  keyed on somebody's name. `tests/test_privacy.py` fails on any address that
  could be a real person's.

### Heavy jobs

- **One track per process, one at a time, with memory checked first.** A batch
  of decodes run beside other heavy work once took the machine down.
- **Scripts that build the viewer and analyse audio need
  `if __name__ == "__main__":`.** The analysis runs in spawned processes, which
  import the main module again.
- **Mutation runs edit source in place.** A killed run leaves the mutant in;
  check `git diff` after one.

### The visualiser window

- **The picture's share of the pane** is worked out in
  `AudioPane._spectrum_budget` from `minimumHeightForWidth` at the pane's
  real width, on resize and on every layout request. The window's minimum
  width comes from its layout, set on first show.
- **The transport line has one height** with the slider or the waveform
  (`addStrut`).
- **A new track keeps the picture open** (`Spectrum.clear(keep_open=True)`);
  the progress bar is drawn outside the new scene's fade.
- **The library's file panel opens a loop pass after the window shows**
  (`MainWindow._visualise_a_file`): opened in the same pass, the Mac's panel
  came up with its sidebar dead. It starts in Music and then where the last
  track came from; files can be dropped on the window (`_add_files`).
- **Every dropdown of fixed wording is a `RoomyCombo(every=True)`**, sized
  by what the style says the widest option needs; measured by hand with an
  allowance for the arrow, the last letter was cut off on the Mac's style.

### The picture

- **One moment a frame.** Everything drawn reads `Spectrum._now`. The pane's
  clock (`_heard`) holds at a seek target until the player moves past it,
  is exact while paused, and closes on the first report after a resume at a
  capped rate.
- **Scene changes** fade over the last frame on the GPU (`_GpuCanvas.hold`,
  `cover`); a change during a change folds the two frames first. The CPU path
  fades from the background.
- **The suite cannot see the GPU.** It runs offscreen, where every test goes
  through the CPU path. `tests/test_gpu_canvas.py` runs scripts on the real
  platform; anything that touches drawing must pass both.
- **On the card:** a framebuffer counts rows from the bottom; a hidden
  widget's resizes are held back until it is shown; keep a reference to a
  `QOpenGLPaintDevice`; reparenting can bring a new context, so
  `initializeGL` drops every buffer; shrink in halves; uniforms go by
  location; `out` is reserved in GLSL 1.20.
- **Never draw text off the GUI thread.** The font database is not
  thread-safe.
- **Gradients go on whole pixels,** and anything clipped to an area takes it
  from a constant: nothing in a frame settles exactly, and a creeping edge
  shows as one pixel changing under a stopped track.

### The game

- **The road is a function of the beat:** `PER_BEAT = (FAR - RIDER_AT) /
  LOOK_BEATS`, set per level in `_set_level`. Read it from the scene.
- **Stopped means stopped.** Every clock in the game runs on track time.
- **A share per frame is a different game per machine.** Slides and shakes
  are shares per sixtieth of a second.
- **`Rider.struck(block)` is what happened to a block.** Do not work it out
  from the craft's lane.
- **Every figure snaps to the nearest beat;** on-beat hits are preferred
  over heavier ones between beats.
- **A seek re-lays the road; a re-count keeps what is in sight.** Blocks in
  sight are carried over through the exact inverse of the road's curve
  (`_when(..., exact=True)`).
- **A corkscrew's turn is in the road samples, not the camera;** towers stand
  on the road with the corkscrew's roll taken out unless inside the tunnel.
- **The city stands upright** (`onRoadUpright`): a tower takes neither the
  road's bank nor a corkscrew's turn, or the skyline twists whenever the
  road does. **The eye is kept clear of the road** between it and the craft
  (`RiderWorld._clear_of_road`), or a crest, or the road behind the craft
  turned further than the craft, put the road through the camera. **A
  craft is one rigid piece** (`onRoadAs`, mirrored by `RiderWorld._rigid`):
  placed in the road's frame where it stands, not point by point along the
  road's line, which bent it over a crest.
- **The results card runs on the wall clock** (`Rider._finished_at`): the
  track has stopped by then, so on the track's clock it never finished
  coming up. A seek back from the finish is a new run from there, with the
  card gone; only a run from the start can keep a best.
- **A plunge is a rush:** where the road falls away, `PACE_FALL` adds to the
  beat's pace (`Rider._falling`), under the level's least warning as ever.
- **A run's log** (`Rider._log`) records each block's outcome until the
  finish; the strip at the end draws it.
- **Sounds:** bump `rider_sound.VERSION` when a sound changes, or the cached
  file is played. Notes are put off with the pane's own timers.

### The Touch Bar

- **An item drives the window's own control** (click, setCurrentIndex,
  setValue), so the bar and the window cannot disagree. A press re-sends that
  item's state, because AppKit has already moved the control under the
  finger.
- **Every AppKit message sent is listed in `touchbar_mac.NEEDED`,** checked
  before anything is drawn; a message a class does not answer ends the
  process. A test fails if the list and what is sent differ.
- **No `NSPickerTouchBarItem`, and no `NSSliderTouchBarItem` with a label or
  width:** both log an AppKit layout complaint. Segmented controls and a
  plain `NSSlider` beside a label do the same jobs.
- **ctypes callbacks cannot return structs,** so a scrubber's entries share
  one width (`_fit`). A scrubber keeps its count until `reloadData`.
- **The suite cannot see the bar.** `tests/test_touchbar_mac.py` runs on the
  real platform, natively and under Rosetta, and checks what AppKit holds;
  `screencapture -b` needs Screen Recording permission.

### Timing

- `trackstyle.choose_tempo` scores each candidate on the drums' fold on the
  beat and on the bar, the snare on the beat and the hats on the eighth.
  `rhythm_of` then halves tempos over `TOO_FAST` without a kick on every
  beat. `_first_beat` picks the bar's first beat from the kick, the snare
  and loudness changes, each scaled with a floor (`FIRST_BEAT_FLOOR`).
- Chords are left out of the downbeat on purpose: they arrive from another
  worker, often after the drums, and would move the bar mid-ride.

## Measuring

```bash
./dev test                     # 4,733 tests
./dev playtest ~/Music/*.mp3   # real records through the real pane
./dev eval                     # the sorter on a labelled set
python tools/corpus.py         # the SpamAssassin corpus
```

`./dev playtest` reports where blocks land against their beats, the road's
speed, and frame cost. Run it one record at a time. No song or frame of one is
ever written into the repository.

Timing changes are checked against a DJ program's beat grids for a private set
of 300 records, outside the repository; only counts are recorded here.

| | before | now |
|---|---|---|
| tempo right (or an octave out) | 269 | 290 |
| on the beat, right tempo | 237 | 256 |
| first beat of the bar agrees (of 193) | 128 | 147 |

The beat being half a beat out (19 records) is unchanged: no rule tried held
up on held-out records.

Sorter, from `./dev eval` and `tools/corpus.py`:

| Set | Job vs not | Exact category |
|---|---|---|
| labelled (102) | 99.0% | 87.3% |
| adversarial (39) | 87.2% | 59.0% |
| held out (24) | 70.8% | 37.5% |

On the SpamAssassin corpus, none of 4,150 genuine messages is filed as job
mail. That must stay at zero.

## Releasing

Set `APP_VERSION`, build with `./dev build` (which signs with the local
certificate from `tools/make_signing_identity.sh`), `./build_dmg.sh`, tag
`vX.Y.Z`, and attach the `.dmg` to a GitHub release. The updater only installs
a build signed with the same certificate as the running copy, with the same
bundle ID and the version the release says, and checks the download against
the size and SHA-256 GitHub reports.

Private vulnerability reporting must be on in the repository's settings, or
About's security link is a 404 for everyone but the owner.

## Open

- **Held-out accuracy is 37.5%.** `./dev tune --corrections` lists real gaps.
- **Nothing learns from a whole conversation;** each message is read alone.
- **Corrections only learn folders,** not that a sender is job-related.
- **`workers._grouped()` opens one connection per account and folder.**
- **The verdict cache ignores UIDVALIDITY changes;** `forget_mailbox` is
  unused.
- **No test opens the built `.app`.**
- **Frame pacing:** about one refresh in ten does not get exactly one frame.
  A display link and a fixed 60 Hz schedule were both worse.
- **The difficulty levels want tuning by play.** They are one table,
  `rider_layout.DIFFICULTY`.
- **The audio allowance is measured on built-in speakers only;** Bluetooth
  reports more, and **Timing…** is the way out.
- **The character classes** (Pointman, Vegas, Pusher, Eraser) need mouse input
  on the grid first.
- **Gatekeeper:** without a paid Developer ID, the first launch needs
  right-click, Open.
- **The Touch Bar has not been checked by eye,** and macOS's customise
  palette for the main window's bar is untested.
