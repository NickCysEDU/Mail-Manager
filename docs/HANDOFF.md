# Handoff

Notes for whoever works on this next: where things are, what breaks if it is
forgotten, how to measure, and what is open. The [handbook](HANDBOOK.md)
describes the app itself.

## Where things are

- **Mail:** `imap_engine.py` (IMAP), `rules_engine.py` and `rulesets.py` (the
  built-in sorter), `llm_engine.py` and `providers.py` (models), `models.py`
  (routing, no Qt), `gui.py`, `triage_table.py`, `settings_dialog.py`,
  `mail_window.py` (a message's window and the compose window),
  `format_bar.py` (the formatting bar over a rich editor, shared by the
  compose window and Settings' Signature page), `outgoing.py` (SMTP and the
  message as bytes), `icons.py` (every drawn icon).
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
- **A `QDateEdit` with its calendar popup on steps on a click in its own
  frame:** Qt hit-tests the click as a combo box and reads the answer as a
  spin box, and the combo's frame is the spin box's up button. Use
  `widgets.DateField`, which only opens the calendar from the arrow.
- **Quit asks once.** `quit_app` answers `confirm_quit` and sets
  `_quit_confirmed`; the close it then makes must not ask again.
- **The settings Touch Bar maps pages by title,** never by index: a page
  added in the middle moved the rest.
- **The rule list is a `StatusList`:** the name is the item's text and the
  state goes in the STATUS and TONE roles, drawn as a badge by
  `StatusDelegate`; the measuring in `WrappingList` adds the badge's room.
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
- **The sorter reads shapes, not only phrases** (`rules_engine.py`, "Reading,
  not matching"): families of moves a message makes - an offer's terms, a
  rejection's consolations, a second interview's days, a step asked for -
  where two families together are the message and one is coincidence. Each
  reader is gated on who wrote it (a person, a hiring mailbox) and most on
  working context, or a friend arranging a drink reads as an interview.
  Every reader and pattern was ablated on all five sets; two that changed
  nothing were removed. Do the same for anything added.
- **The sorter also reads statements** (`statements.py`): the decline, the
  offer, the step asked for, the meeting arranged and the acknowledgement,
  each a short list of patterns over one normalised sentence, with the
  quoted history of a reply dropped first. Guards: a condition before the
  match (`_CONDITION`), a hedge ("may be", `_HEDGE`), a negation governing
  the verb (at most two words before it, `_NEGATED`), and a feedback survey
  within three sentences of a request (`_FEEDBACK`). In `classify`, a stated
  category gets `STATED_WEIGHT` (decisive), but only where a hiring word
  besides "application" is present and the sender is not a university, court
  or landlord (`hiring_said`); a stated invitation also needs a hiring anchor
  (`anchored`), and an unanchored meeting is capped at
  `SOFT_EVIDENCE_CEILING`. What is stated outranks what phrases suggest in
  the precedence contest (not Unsolicited, which is about origin); the thanks
  an acknowledgement is does not rival a stated later stage
  (`_ABSORB_THE_THANKS`); a lone stated reading is as sure as the rules get,
  unless a rival still qualifies once the message is read again without its
  conditions and hedges (`classify(..., _rereading=True)` on
  `statements.unconditional`). On a one-way video platform the step asked for
  is the interview (`VIDEO_INTERVIEW_DOMAINS`, as the model prompt defines
  it). A talent network's list is judged on its words without the stated
  weight. Every guard was broken on purpose and a test failed; keep it so.
- **Patterns in `statements.py` must hold on mail nobody applied for.**
  `python tools/corpus.py` runs 4,150 genuine messages from a newsletter-era
  corpus; "ham filed as job mail" must stay at 0. Three loose patterns were
  found there ("not considered a deterrent", "when the program is closed",
  "our new publishing schedule"), each now a test.
- **A message is cut short only when its text is** (`imap_engine._text_was_cut`):
  the bytes run out in the last part the parser saw, so a logo or PDF after
  the text leaves it whole; a readable part the server describes in
  BODYSTRUCTURE but never sent means text is missing. Marking every message
  over 64 KB cut capped most applicant-tracking mail below the threshold.
  `tests/conftest.py`'s fake server honours `BODY.PEEK[]<0.N>` like a real one.
- **Transactional topics weigh a long body's last 40% at half**
  (`_head_and_footer`, `FOOTER_SHARE`): "your statement can be viewed any
  time" is under every receipt a payment service sends. Not other topics:
  a parish bulletin is about the parish to its last line.
- **Context, after each message is read alone:** `workers.read_with_the_thread`
  (a conversation) and `workers.read_with_the_correspondent` (a calendar
  invitation from a person you are in a hiring process with). Both carry only
  to weak readings and hold them below the threshold.
- **Labels are never moved toward the engine.** The sets disagree in places,
  and the disagreements are left as they are: job-board blasts are
  Promotions in the older sets and Newsletters in the real-mail ones; a
  one-way video interview reminder is Next Steps in `labelled.json` and
  Interview by the model prompt's definition. Each shows up as a miss on one
  side, and goes to review, which is the safe outcome.
- **Anonymising is checked by re-scoring.** If a score moves, a signal was
  keyed on somebody's name. `tests/test_privacy.py` fails on any address that
  could be a real person's.
- **Run from a terminal, the app has no menu bar until it is activated
  again:** macOS gives an unbundled process the bar only on a later
  activation. `macname.MenuBarWaker` (from `main.py`, never in the built app)
  hands the focus to the Dock once on the first activation and takes it back
  `BACK_AFTER_MS` later, only if the Dock still has it, so it never takes
  focus back from another app; it gives up after `ROUND_TRIP_MS`. The Mac's
  file panel had its sidebar greyed out for the same reason, so
  `AttachmentViewer.add_tracks_when_ready` opens it once the window is active
  (`PANEL_WAIT_MS` at most).
- **Every file panel opens through `widgets.open_file` and its three
  siblings,** which call `macname.refocus_file_panel` first: run from source,
  the panel's sidebar (drawn by another process) still came up grey, and
  clicking away and back fixed it. It watches for the panel to become the
  modal window (`PANEL_LOOK_MS`, up to `PANEL_LOOKS`): here it took over half
  a second to appear, and a fixed 400 ms wait missed it. Then it leaves it
  `PANEL_SETTLE_MS` and hands the focus to the Dock and back, only from the
  Dock. A test fails on any `QFileDialog.get…` call outside `widgets.py`.
  The round trip is tested on the real platform; whether it ungreys the
  sidebar is not, since this session cannot capture the screen.
- **The inbox is two lists** (`MainWindow._listed`, `_sorted`): the messages
  a `ListWorker` read when the window opened (`TriageItem.analysed` False,
  shown as Not sorted yet), and the last scan's rows, shown together by
  `models.one_row_each`. A scan replaces `_sorted`; a message outside its
  window falls back to how it was listed. Filing, the Apply count, undo,
  unfinished work, drafting and the briefing read `_inbox_rows()`, never
  `model.items`, which may be another mailbox: tests fill the window through
  `_on_scan_done`.
- **A row from any mailbox but the one sorted is never filed**
  (`TriageItem.fileable`): undo puts mail back into the sorted mailbox, and
  the folder picker and `set_override` refuse such a row.
- **A move report keeps each mailbox's part** (`MoveReport.add`, `about`):
  merged by UID, it dropped `new_uids`, so no filing from the window was ever
  undoable, and it let one mailbox's message 1 stand for another's.
  `build_move_plans` names each message's own folder.
- **A mailbox is looked at again when chosen by hand** (`_chosen`, and
  Get New Mail), never when the window moves itself (`_go_to`, as a scan
  starting does). A look again reads only UIDs not held at the last look
  (`fetch_window(skip=)`, `_present`, `_known_uids`) and takes out what has
  gone (`Listing.present`, `TriageTableModel.remove_where`), keeping the
  selection; a row a scan read stays as it was read. A new period or new
  mailboxes forget `_present` with the rows.
- **The listings are readers, not tasks** (`_mailbox_workers`, never
  `_workers`), so Scan is never refused for them; Stop All cancels them and
  quitting stops them. A listing replaced since (Settings changed what it
  depends on, see `_mailbox_shape`) is not heard. Nothing at launch asks:
  `_scan_on_open` says in the log what it lacked. Auto scan is the launch's,
  run once (`_scanned_at_launch`): `main.py` starts it with no window when
  the app opens in the menu bar.
- **Hover help waits in some windows** (`helpmode.PATIENT` on the message,
  compose and viewer windows): with the ? off, a control explains itself
  after the pointer has rested on it for `PATIENT_DELAY`; with it on, at once.
- **Every window of its own carries a menu bar** (`mail_window._app_menus`).
  On a Mac the menu bar belongs to the active window, and a window without
  one leaves the main window's up, whose key equivalents Cocoa fires
  regardless of Qt's shortcut context: ⌘R would scan from a message window,
  ⌘↩ would file from the compose window. The bar needs the app roles too
  (Settings, About, Quit), or the app menu loses them.

### Mail going out

- **The windows own nothing.** `MessageWindow` and `ComposeWindow` hand every
  action to the main window - `compose`, `act_on_rows`, `send_mail`,
  `save_draft`, `neighbour_row`, `select_row`, `folder_choices`,
  `known_addresses` - because it holds the table, the accounts and the
  passwords. `tests/test_mail_window.py` has the stand-in that spells out
  the contract.
- **A quick move keeps the row,** marked moved, through the same
  `MoveReport` and undo stack as Apply (`_record_moves`). Nothing removes a
  row from the table but a scan. A message window follows its message by
  `(account_id, uid)`, not by row, so a scan or a sort does not swap the
  message under it.
- **A closed window is counted gone at once** (`_live_mail_windows` wants
  visible): `WA_DeleteOnClose` deletes on the next turn of the loop, and the
  wrapper in between would be found and raised as the open window.
- **An empty `account_id` on a message means the primary mailbox**
  (`_account_id_of`), as the rest of the app already assumed; the answered
  flag and the sent copy need the real id.
- **SMTP keeps nothing.** `SendWorker` appends the copy to Sent itself
  (`IMAPEngine.save_sent`), except for providers in `outgoing.KEEPS_SENT`,
  which file their own. The special folders are found through
  `special_mailbox`, by the server's flag first and the provider's names
  second; a missing Archive is created, a missing Trash is an error.
- **Sending is the part that matters.** A refused send fails; a lost copy or
  flag after it is a note on the status line, never a second attempt.
- **Opening a message marks it read,** on the server through `FlagWorker`
  once there is a password, in the table either way.
- **The message view paints its own light page** (`MailView` sets a
  stylesheet on itself): the palette alone lost to the app's stylesheet in
  the dark look, and dark text sat on a dark ground. The sanitiser darkens
  pale text in a message with no dark background of its own
  (`html_utils.has_dark_background`) and leaves a dark design alone.
- **The mail buttons' icons are drawn** (`icons.py`), in the text colour,
  once per name and colour; the Touch Bar's picture names map to them in
  `mail_window._ICONS`. Both windows' bars are icons alone; every action
  carries its name and key in its tooltip (`format_bar.tip`).
- **The formatting bar is `format_bar.FormatBar`:** the actions are public
  and the window puts them in its menus and on its Touch Bar. The lists and
  the colour are split buttons (`QToolButton[split="true"]` in the theme);
  Qt does not grow a tool button for a styled menu segment, so the rule's
  padding makes the room, and it has to outrank the segment rule. The size
  box is changed by index from the Touch Bar, so `currentIndexChanged`
  applies a size as well as `activated`.
- **The sign-off is kept as a fragment** (`RichEditor.fragment`: the body of
  Qt's document, in `Settings.signature_html`). The compose window asks
  `signature_html(replying=...)`, which honours the two switches and falls
  back to the name alone; nothing ships a name as a default.
- **A picture in a message goes as a related part** (`outgoing.inline_pictures`
  turns each `data:` picture into a `cid:` part under the HTML half): Gmail
  shows nothing for a `data:` picture.
- **A real inbox is the dev set that matters.** Three weeks of it, judged by
  hand, live outside the repository at `~/.mail-manager-eval/inbox/` on the
  machine that has them (`real_dev.json` scores with `tools/evaluate.py
  --file`); the readers of real shapes in `rules_engine.py` (a calendar
  call, a job board's relay, precedence in leagues) came from its misses.
  Nothing from it may enter the tree: no name, address, subject or number,
  even in a test. Invent the test's people.
- **After the sorter, the conversation** (`workers.read_with_the_thread`):
  within a thread it is sure about, weak readings take the thread's
  category at a confidence below the filing threshold. It never touches a
  reading the sorter was sure of.

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
  capped rate. When a track's frames first land (`set_frames`) the clock is
  read afresh: it had fallen behind the player over the long frame that
  landed them, and the catch-up a frame later was counted as a seek, which
  made the rider lay its road and then snap it to where the track was.
- **The plasma is doubled three times before it is stretched**
  (`Plasma.doubled`): stretched from the grid itself, the interpolation
  showed as diamonds a cell wide, which read as a low-resolution picture.
  The scope has no field behind it; its screen is black.
- **The rave reads the track's sections** (`Rave._read_style`, the same
  `trackstyle.read` as the rider) and sweeps its rig on the beat
  (`SWEEP_REST` beats a sweep at rest, one in a drop); a snare jolts the
  sweep and sends a pulse that crosses the room in a beat; big rings close
  by a share of their distance in `RING_BEATS`, so they are seen growing
  evenly. A held hand strobe streams pulses there, rings in the tunnel
  (`Tunnel._throw`) and flickers the city's windows.
- **Two hand strobe keys, two meanings:** G holds the light on until it is
  let go (`hold_flash` sets `SpectrumState.held`, which scenes read as a
  steady light: the city's windows all lit, the tunnel's corridor lit), H
  flashes as fast as it can (`spam_flash`); neither ticks the strobe box.
  Rings and pulses are thrown on a rising edge only, or a held key
  machine-gunned them.
- **The rave's lasers are for the big drops** (`Rave._drop_runs`, from
  `trackstyle` sections once the drums have been read, `style.from_drums`).
  The reading names a verse a drop when it follows a quiet intro, so the rig
  takes only drops at `RIG_LEVEL` that arrive `RIG_LEAD` above their lead-in
  (or after a part without drums), and carries one through the next part
  only within `RIG_CARRY` of its level and for at most its own length: on
  the test records that took the rig from half of each track to under a
  third, verses and rap sections out. Each run is snapped to where
  the loudness really rises and falls (`_snapped`), since the sections came
  up to a bar early on real records; the rig comes up over `RIG_IN` seconds
  and goes over `RIG_OUT` beats. Nothing is lit before the drums are read.
- **The bloom's halo is not blurred before it is added.** A softened halo
  (two binomial passes) took the neon off the rave's wireframe; what made
  the rave look bad in a window was its haze, stretched from a small tile,
  and `HAZE_DOUBLINGS` is the fix for that.
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
- **Puzzle:** a grey hit drops in as clutter (`GREY_CELL`), broken by a clear
  beside it; a block into a full column bursts it and costs `OVERFILL_COST`.
  A colour that goes by is recorded "missed" and dissolves before the craft
  (`DISSOLVE`, `rider_gl._missed`) rather than passing through it dark. The
  game chosen is kept across tracks (`AudioPane.GAME_PREF`).
- **Sounds:** bump `rider_sound.VERSION` when a sound changes, or the cached
  file is played. Notes are put off with the pane's own timers.
- **The road is read on a curve** (Catmull-Rom through four samples, in
  `roadAt` and its Python mirror `_sample`); the samples are nearly a unit
  apart and straight pieces between them had a corner at every one. The
  shaders are the old GLSL dialect: no `min`/`max` on ints, use a ternary.
  Quiet sections turn twice as often (`Rider.CALM_PHRASE`).
- **A kick arrives like one** (`beatmap.attack`, the Kick profile's
  `attack` bar of 2.0): measured on real records, kicks on the drums' beat
  rose 2.3 to 30 times over the fifty milliseconds before them and bass
  notes between beats 1.2 to 1.6. The click across the rest of the spectrum
  told them apart not at all. A smaller rise is kept only where it lands
  with the sure kicks (`beatmap.on_the_beat`): on the drums' own beat from
  `trackstyle.rhythm_of`, which was right on every kit and record where a
  fold of the hard kicks alone was not, at the division the sure kicks
  keep to. Nine recordings: four-on-the-floor kicks on the beat 70 to 95
  per cent at a count near the tempo, from 44 to 65 at half as many again.
  The written trap kit lost most of its kicks to this - short, soft hits on
  the sixteenths, which no real record tried has - and its floor in
  `tests/test_beatmap.py` was lowered with the figures. Test songs are
  never in the tree; the analysis scripts and their output live under
  `~/.mail-manager-eval/songs/`.
- **The Bass strobe follows the drums' beat** (`Spectrum._grid_the_bass`)
  where the coarse map never locked a tempo, lit as hard as the nearest
  kick; a locked map is left alone. The flash falls as a lamp cools, not in
  a straight line.

### The Touch Bar

- **An item drives the window's own control** (click, setCurrentIndex,
  setValue), so the bar and the window cannot disagree. A press re-sends that
  item's state, because AppKit has already moved the control under the
  finger.
- **Every AppKit message sent is listed in `touchbar_mac.NEEDED`,** checked
  before anything is drawn; a message a class does not answer ends the
  process. A test fails if the list and what is sent differ.
- **No `NSPickerTouchBarItem`, and no `NSSliderTouchBarItem` on the bar
  itself:** both log an AppKit layout complaint. Segmented controls and a
  plain `NSSlider` beside a label do the same jobs. The one place AppKit's
  slider item is used is a popover's press-and-hold bar (`Popover(hold=...)`,
  `Renderer._hold`): AppKit hands a held finger only to its own item. It
  logs one line of its own when it builds its slider
  (`_NSLayoutConstraintNumberExceedsLimit`, seen with a bare item and
  nothing of ours set); the native tests set that line aside and fail on
  any other.
- **A held slider works as the system's volume does.** AppKit carries the
  held finger through a private transposer; the slider item's
  (`NSTouchBarSliderPopoverTransposer`) moves the value by the finger's
  movement (dx × range ÷ track width), never to the finger, and
  `NSTouchBarItemOverlay.currentRecommendedOptions` opens the bar beside the
  finger, on the side with more room, when its minimum width fits there.
  Read from AppKit's own code, by disassembly; see `## Open`. So the slider
  is never narrower than `HOLD_LEAST`, to fit either side, and up to
  `HOLD_MOST` it stretches across its side, as the Control Strip's slider
  container (941 points) does. It is not the principal item: that held it
  to the middle and stopped it stretching. Both ends carry an
  `NSSliderAccessory` symbol (`AudioPane.QUIET_LOUD` and its neighbours).
- **AppKit draws its slider badly on this macOS's bar:** a grey panel of
  half the bar's height with square ends, behind a square knob taller than
  it. `Renderer._polish` hides `_NSSliderBackgroundView` and rounds every
  `NSSliderKnob` (`KNOB_ROUNDING`, continuous corners), on every push of a
  slider's state from the first, giving a knob its own layer first: a bar
  not yet shown has none. Found by class name, so on a macOS without those
  classes nothing changes. The visualiser's volume is always a button to tap
  or hold: a plain slider jumps to where it is touched.
- **The live bar can be read in-process** while the Touch Bar is awake
  (`+[NSFunctionRow isDynamicFunctionRowAvailable]`): item views sit in an
  `_NSFunctionRowPanel`, and `renderInContext:` of the root view's layer
  draws what is on the glass. Asleep, with nobody at the keyboard, nothing
  is hosted. `tests/test_touchbar_mac.py` renders a held slider in
  `+[NSAppearance _functionRowAppearance]` in an off-screen window and
  checks the knob's corners and the panel's place in the pixels.
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
./dev test                     # 5,178 tests
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

Sorter, from `./dev eval`, `tools/adversarial.py` and `tools/corpus.py`:

| Set | Job vs not | Exact category | Filed, of those right |
|---|---|---|---|
| labelled (102, dev) | 100% | 94.1% | 71 of 72 |
| adversarial (39, dev) | 100% | 97.4% | 5 of 5 |
| retired held-out (34) | 91.2% | 82.4% | 11 of 12 |
| held out (39, measured once) | 92.3% | 87.2% | 20 of 21 |

A private set from a real inbox, split before anything was read into 508 to
study and 292 held out, labelled by hand before the sorter ran on it, lives
outside the repository with the scripts that score it the way a scan does
(`~/.mail-manager-eval/inbox/score.py`). Held out, measured once against the
version before: exact category 72.3% to 88.7%, job mail filed without asking
92 to 137 of 152, none of it into the wrong folder either time. Three
everyday messages were taken for job mail at full confidence; two were a
talent community's invitations, read as an interview and an offer by
patterns that have since been tightened, so the held-out figure after that
(89.4%) is not a clean one.

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

- **The sorter's scores** (exact category): labelled 94.1%, meetings 93.3%,
  acknowledgements 100%, adversarial 97.4%, oblique 100%, retired held-out
  82.4% - all dev sets now. The held-out set (`holdout.json`, 39 messages,
  written fresh on 7 October 2026 and measured once): 87.2% exact, 92.3% job
  against not, 20 of the 21 it filed right. That is the number to quote for
  mail it has not seen. Its one wrong filing, and the retired set's, are the
  two confident mistakes left that the version before did not make: onboarding
  paperwork after an accepted offer read as Next Steps, and a person's "we
  got your CV" read as an interview. Do not fix them against those sets;
  write the next set first. `./dev tune --corrections` lists real gaps.
- **A conversation is read together only after the fact** (the two
  `workers.read_with_*` passes); each message is still classified alone.
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
- **The Touch Bar has not been checked by eye or with a finger.** Its
  drawing is checked in pixels and its layout was read live once, but where
  a held bar opens and how far it stretches come from AppKit's code: the bar
  was asleep whenever it could have been measured. macOS's customise palette
  for the main window's bar is untested.
- **Other mailboxes are listed over the inbox's period** and read only:
  there is no paging beyond `workers.LIST_MOST`, no unread count in the
  sidebar, and no filing from them.
- **The rules page's dropdowns could not be made to cut an option short** on
  this Mac, at 760 or 980 wide, in light or dark, at the normal or the
  larger type; every combo now asks room for all of its options
  (`RoomyCombo(every=True)`). If a cut-off is seen again, note the page's
  size and the appearance settings.
