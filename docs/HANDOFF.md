# Handoff

Where this round of work got to, what was measured, and what is worth doing
next. The list at the bottom is meant to be ticked off, everything already
ticked was done in this round, and the untinted boxes below it are what a
future session should pick up.

---

## Round three: Music rider, and the road it runs on

The visualiser pane has nine scenes. One of them, **Music rider**, is a
game, and this round turned it into a replica of Audiosurf - built to the
blueprint in `audiosurf clone guidelines.rtf` in the working folder and
then to Audiosurf 2's own documented mechanics. It is four games on one
road now, and almost everything below was found by *measuring* rather
than by reading the code.

### The one tool that matters

    ./dev playtest ~/Music/*.mp3

Plays real records through the real pane, headless, frame by frame, and
reports where each block landed against its beat, what the road's speed
did, whether it ever went backwards or stood empty, what a player who
dodges perfectly is still hit by, how many coins that player took, how
many corkscrews the track earned, and what a frame costs. Nearly every
fault in this list was found there and then written back into the suite
as a test. A test says a block arrives on the beat when the chart is
three evenly spaced kicks; a record says whether it does when the
detector heard the kick 40 ms late and the tempo came out at 87.3.

No song, path or frame of one is ever written into the repository.
`*.png` is in the ignore file for exactly this reason: `--save` writes a
frame of the scene while somebody's music is playing.

### The four games

| Game | What it is |
|---|---|
| **Mono** | Grey against colour. A chain: 1, 5, 9 … capped at 200, broken by a grey. Clean finish +30%. A side bumper shatters the first grey free and comes back over 8 s. |
| **Ninja** | The same road with 4 hazard slots in 7 rather than 2 - fifteen hazards a minute against seven on a real record. Clean finish +60%. |
| **Wakeboard** | **Up** leaves the road. A jump is paid by how much of a crest it left from: a measurement, not a switch, so half a crest is half the points. Nothing touches you or is collected in the air. |
| **Puzzle** | The grid: 3 wide, 6 deep, 4-connected, 750 ms fuse, cascading gravity, quadratic cluster scoring, overfill stun. |

Coins sit where you have to be brave to take them - beside a single
obstacle, or in a lane a wall is *about to close*, ending 0.3 s before it
arrives. Corkscrews turn the whole world over at the song's loudest
moments, with a power block at the mouth of each.

### Invariants. Break these and the scene breaks

Each one cost a round of measurement to find. Each has a test.

- **One clock.** The road's position is a function of the beat:
  `PER_BEAT = (FAR - RIDER_AT) / LOOK_BEATS`, so a block laid on beat *n*
  sits at *n* of them and arrives exactly on it. Before this there were
  two clocks and blocks slid over the ground.
- **`_origin` is not `_grid`.** `_grid` is re-derived every frame and
  phase-locked; `_origin` is a *fixed* distance origin. A distance
  measured from something that walks with you is always the same
  distance, and that is a road that never moves.
- **A block must be read against the road.** The road, the lamp and the
  sky behind it are held near the floor on purpose. An obstacle and the
  road it stood on once differed in hue and not in brightness - 0.400
  against 0.401 - which is invisible. Anything that brightens the middle
  of the frame has to be measured against `TestTheRoadIsBuiltFromTheSong`
  and the coin and block contrast tests before it ships.
- **Stopped means stopped.** Every clock in the scene runs on the
  track's own time: the shake, the sparks, the field, the camera easing,
  the envelope followers, the fuse, the bumper's recharge and the origin's
  own phase correction. That last one crept ten units of road a second
  under a stopped song.
- **The same game on every machine.** A share *per frame* is a different
  game on every machine. The lane slide and the camera's shake are shares
  per sixtieth of a second: the dodge window holds between 50 and 67 ms
  from 30 fps to 144, and was 167 ms at 30 and 25 at 120.
- **Nothing the analysis says can close the window.** `SpectrumState.settle()`
  forces every number back into its range once a frame, before any scene
  sees it. A nan is *truthy*, and a nan tempo used to raise out of paint
  on the first frame.

### Measured, at the end of this round

- A block arrives a median of **10 ms** from its own beat on a real
  record; the road runs 4.8 to 19 units a second around a mean of 12.3,
  with a floor at 45% of the mean rather than stopping.
- A beat changes **43% of the frame** and swings its brightness 0.064.
  It was 7% and 0.026, which is what "the beat isn't felt" measured as.
  The beat hits at the frame's *edge* and on the craft, never in the
  middle, so block contrast is identical on the beat and off it.
- A frame costs **8.7 ms median at 1920x1080** against a 16.7 ms budget.
  On the graphics card, at a Retina full screen's every pixel with 4x
  multisampling and all of the polish, it is **10.2 ms at 2880x1800**,
  where the CPU took 41.9 and had to draw at half the resolution.
- A scene's first frame costs 12-16 ms rather than up to 168. The font
  machinery is warmed on a worker thread and a scene is drawn invisibly
  for its first six frames.
- The road bends up to 1.37 lane widths and is within a third of a lane
  of straight 63% of the time. It was straight **100%** of the time: the
  stereo lean was summed with its own bias in it, and summing a biased
  signal gives a ramp, which with the camera pinned to the road is
  exactly what straight looks like.
- 14,625 hostile inputs through `tools/stress.py`: nothing raised,
  nothing hung.
- Across eight real records of different genres: zero hits for a player
  that dodges properly, zero backwards steps, a block a median of 8 to
  15 ms from its beat, and the road bare between 0 and 3.4 per cent of
  the time. Before the quiet-passage fill, two of those eight were bare
  for a quarter of the run and one stretch ran 8.6 seconds.

### Watch out for

- **Never draw text off the GUI thread.** The font machinery's one-off
  cost is 145 ms, and moving it to a worker thread to keep it off the
  frame clock segfaults the process:
  `QCoreTextFontDatabase::populateFamilyAliases` is not thread-safe and
  races the GUI thread doing the same thing. It is warmed on the GUI
  thread while the pane is built, where a stall costs nothing because
  nothing is animating yet. A crash is not a trade for a stall.
- **Anything drawn as a gradient goes on whole pixels.** The same
  asymptote bites twice: a filled path's antialiased edge rounds through
  a millionth of a pixel unmoved, and a radial gradient's smooth ramp
  does not. The craft's halo is placed and sized on integers for that
  reason, and the lamp's fill area is computed from a constant. Both
  were found the same way - one pixel of a 640x360 frame changing by one
  step of red between two frames a second apart with the track stopped -
  and `xxxx_xxxxxxx_xx_xxxxxx_xxxxx_xxxxx_xxx_xxxxx_xx_xxxxxxx` is the
  test that catches it every time.
- **Nothing in a frame settles exactly.** The pane's level envelopes
  ease towards a held row, and the clock closes on the playhead, both
  asymptotically - so under a *stopped* track every number still creeps
  in the tenth decimal place. A gradient's interior rounds through that
  without moving; the edge of the region it is painted into does not,
  because a step function on a creeping number is a step. Anything
  clipped to an area has to compute that area from a constant, not from
  a value that creeps. Measured as exactly one pixel of a 640x360 frame
  changing by one step of red, which is the sort of thing that is
  invisible and still wrong.
- **Measure before culling for speed.** Dropping a block's reflection
  past a distance took a third of the frame's fills out and saved 0.2 ms
  of 8.9 - and spent the thing that makes a block sit on the road at
  exactly the distances a player reads. The test caught it. Four
  gradient fills of a band also cost more than one fill of the whole
  frame, because the per-call setup dominates the pixels.
- **Mutation runs edit the source in place.** `finally` restores it - but
  a killed run does not, and a previous session left the game box
  hard-wired to two modes and `_collide` returning early in mid-air. A
  green full suite is the proof that no mutant survives, because every
  mutant written for this scene is caught by a test.
- **A test that means "figures" must exclude coins.** A coin trail is
  three things a sixth of a second apart on purpose.
- **`_pulse` is an attribute, not a method.** The beat phase. The rim
  flash is `_rim`.
- **The pane draws on the graphics card, and the suite cannot see it.**
  `_GpuCanvas` in `attachment_widgets.py` is a `QOpenGLWidget` laid over
  the pane; `_paint_on_gpu` runs the same `_paint` into a 4x multisampled
  framebuffer at the screen's real resolution, and the polish goes on
  after. The suite runs on the offscreen platform, where no context can
  be made, so every other test goes through the CPU path - which is also
  the fallback. `tests/test_gpu_canvas.py` is the only file that runs the
  card: each test is a subprocess on the real platform reporting JSON,
  and it skips on a machine with no context. Anything that touches
  drawing has to pass both. `MAIL_MANAGER_GPU=0` forces the CPU path.
  `CardSharpness` measures each frame *finished* (`glFinish`), because
  on a card the calls return long before the drawing is done, and it
  judges on a median of thirty, never a running average.
- **Traps on the card, each found the hard way.**
  A framebuffer counts rows from the bottom: a sub-rectangle read from
  one has to be flipped, and it only shows when the scene is not
  centred. A hidden widget's resize events are held back until it is
  shown, so the canvas is resized in `update()` as well as
  `resizeEvent`, or it draws a pane of no height. A `QOpenGLPaintDevice`
  made as a temporary is collected while it is being painted on - keep
  a reference. Reparenting into full screen can bring a new context, so
  `initializeGL` throws away every framebuffer. Shrinking on the card is
  done in halves; one blit reads a pixel in sixteen and the bloom comes
  out beaded. Never monkeypatch `QGuiApplication.instance` in a test -
  pytest-qt's teardown calls it; `_platform_name()` is the seam.

---

## Round two: non-job mail, church, and going public

**Non-job mail had no menu.** Job mail could be ticked and filed in one
click; everything else sat in the table doing nothing, could not be ticked,
and said "Leave in place" without any hint that this was a setting rather
than a fact. The setting lived in two places, one of them a submenu inside
the button for choosing which AI to use. There is now a **Sorting** button in
the toolbar holding all three questions - what to sort, what happens to the
rest, which topics earn a folder - and the third of those had no interface at
all before. A row that will not tick says which of the two reasons applies and
offers to change it.

**Church is a topic.** 161 signals, and the gap that made it work was
denominations - the word in the masthead and the footer of every parish
mailing, and the most portable vocabulary in the whole table. Four rows in the
labelled set were parish mail labelled Newsletter and Personal because Church
did not exist when they were labelled; all four now classify correctly, and
real mail wrongly sent to Junk fell from 0.10% to 0.07%.

**Form loses to subject.** Newsletter, Promotion and Personal describe how a
message is written; every other topic describes what it is about. A parish
bulletin is a newsletter in form and church mail in substance. So a subject
topic with real evidence now beats a form topic, and that is the rule that
made the church newsletters land correctly.

**The repository can be published.** The labelled set was built from a real
inbox and named the people who wrote to it, the companies that interviewed
them, the church they attend, and two people who had died. tools/anonymise.py
replaces the names and keeps the language, verified by re-scoring rather than
by reading: 87.3% exact, unchanged. Four wrong versions were caught that way.
It refuses to guess at names in running prose and flags those rows instead;
the two it flagged were rewritten by hand.

**Two switches can only be thrown after the repository is public.** GitHub
refuses both while it is private, and About links to the first of them:

- **Private vulnerability reporting** - Settings → Code security. This is what
  makes `/security/advisories/new` accept a report from somebody who is not a
  maintainer, and that URL is the "Security concern…" button in About. Until
  it is on, that button leads to a 404 for everyone but the owner.
- **The DMG on the release** has to be rebuilt from the published commit, so
  that the build stamp in About names a commit a reader can actually look up.

Dependabot alerts and its automated security fixes are already on; those two
GitHub does allow on a private repository.

**Everything that can explain itself does.** Nineteen of twenty-nine controls
in the main window had no tooltip, which in help mode means no explanation at
all. tests/test_first_run.py walks every clickable, typeable and draggable
widget in the window, the preview and the settings dialog, and fails on any
that cannot explain itself.

---

## What changed, in one paragraph each

**The window is four files instead of one.** `gui.py` had reached 6,541 lines.
It is now `widgets.py` (shared pieces), `triage_table.py` (the table and the
preview), `settings_dialog.py` (everything configurable) and `gui.py` (the
window itself), with every symbol re-exported so no import anywhere else
changed. Pruning the imports afterwards found a real bug: `MainWindow` had two
`resizeEvent` methods, and the second had been silently shadowing the first
since they were written.

**It learns from being corrected.** Move a message somewhere the app did not
suggest and it writes that down. An exact address needs one correction; a
domain needs two from two different people, and never at a shared mail host.
The most recent correction always wins, so changing your mind takes effect
immediately. The Folders tab lists what it learned, says why, and has a Forget
button.

**It does not pay twice for the same answer.** Verdicts are kept between scans,
keyed on mailbox, UID and a hash of every setting that changes what a verdict
would be. Re-scanning the same window costs nothing. Failures and rules-engine
fallbacks are deliberately never kept.

**It sorts while it fetches.** The two halves of a scan ran end to end, each
idle while the other worked. They now overlap. Correctness does not depend on
it, anything the fetch does not stream is swept up and classified anyway, and
there is a test for exactly that case.

**The sorter reads the From line.** A careers mailbox is not a person writing
to you however warmly it is written, which stopped eight adversarial rejections
reading as personal notes. A second table of otherwise-too-generic phrases
counts only once a hiring mailbox or a named process has licensed it, the
thing a language model does that a phrase table does not.

**The sorter is four times faster.** Before running a signal's regex, check
that the longest word of its phrase is in the text at all. 43.5 ms per message
to 10.1 ms, with every eval fixture scoring exactly what it scored before.

**Undo works, and works more than once.** An IMAP `COPY` gives the message a
new UID and the old undo used the old one, so it either did nothing or moved a
stranger. It now reads the server's `COPYUID` receipt, and refuses to guess when
there is not one. The stack is ten deep.

**Nothing personal can reach the repository.** `tests/test_privacy.py` reads
every tracked file and fails on an address that could belong to a real person.
It found three in the existing tests on the day it was written.

---

## Where it stands, measured

Run `./dev eval`, `./dev test` and `python tools/corpus.py` to reproduce any of
these.

| Set | Job vs not | Exact category | Of those it filed |
|---|---|---|---|
| labelled (102) | 99.0% | 87.3% | 54/55, 98.2% |
| adversarial (39) | 87.2% | 59.0% | nothing filed; all held for review |
| held out (24) | 70.8% | 37.5% | nothing filed |
| meetings (15) | 100% | 80.0% | 2/2 |
| acknowledgements (11) | 100% | 100% | 7/7 |

**SpamAssassin corpus**, 6,046 real messages: 0 unreadable, ~140 messages a
second, **0 of 4,150 ham messages filed as job mail**, 3 sent to Junk (0.07%).
That first figure is the one that matters and it must stay at zero.

**Speed.** Rules engine 10.1 ms per message. Lexicon opens in 38 ms using
1.95 MB, down from 70 ms and 10.5 MB, and neither number now grows with the
table. Test suite 3,956 tests (with the evaluation sets present) in about three minutes on four workers.

---

## Things a future session should know

- **The audio analysis runs in worker processes** (`attachment_audio._worker`,
  spawned). Three consequences. Any script that constructs the viewer and
  analyses a track must put its body under `if __name__ == "__main__":` -
  a spawned worker imports the main module again, and an unguarded script
  runs itself once per worker. `main.py` calls `multiprocessing.freeze_support()`
  first, which is what stops a worker in the built app opening a second
  window; the self-test runs a worker to prove it. And a test that
  monkeypatches `analyse` or `onset_frames` does not reach the workers - set
  `attachment_audio.WORKERS = False` to test the thread path.
- **The kit, the traces and the bands arrive in any order now.** The drums run
  alongside the picture, so they can land first; `set_beats` merges the kit
  back in rather than replacing the table, and the relay holds the kit until
  the bands have gone out. The road's contour comes with the bands and is
  final (`set_contour`); `set_traces` leaves it alone.
- **Brace a shell variable that touches a curly quote.** `"as “$IDENTITY”"`
  is read by macOS's bash as a variable whose name includes the quote's
  bytes; under `set -u` it stopped `build_app.sh` after the bundle was
  made and before it was signed or started - only on machines with a
  signing identity, which is why it went unseen. `${IDENTITY}`.
  `tests/test_abuse.py` checks every script for it.
- **`./dev test` runs `-n 4 --dist loadfile`.** Whole files per worker, because
  the Qt tests share one `QApplication` per process. A single file is about two
  seconds; the whole suite is about three minutes. Serial was six.
- **Never assert on a menu by calling `exec`.** It enters a native modal loop
  that pytest's thread-based timeout cannot interrupt, and the suite hangs
  rather than failing. `build_table_menu()` and `build_header_menu()` return the
  menu; the caller shows it.
- **Progress signals emitted from the classifying thread arrive by queued
  delivery.** Correct in the app, invisible to a test with no event loop. The
  final progress update is emitted from the scan thread for that reason.
- **The eval fixtures are hand-written stand-ins.** `./dev tune` scores against
  a real inbox and prints only counts and confidence bands. `--write` produces
  a labelled set and refuses to write anywhere inside the repository.
- **Before adding a phrase to the sorter, ablate it.** Twenty-six of the
  conditional phrases first written for the adversarial set were shaped too
  closely to it; removing them changed none of the four numbers above. The
  general ones did all the work.
- **`tools/anonymise.py` protects what the sorter reads, taken from the
  engine.** Rewriting a host the rules engine has a signal for changes the
  verdict and the fixture then measures a different app. That list is derived
  from `SCHEDULING_LINK_DOMAINS`, `ASSESSMENT_LINK_DOMAINS` and every
  sender-field signal, never typed out beside it.
- **Anonymising is verified by re-scoring, not by reading.** Four wrong
  versions were caught that way, including one that took the set from 87% to
  57%. If a score moves, a signal was keyed on somebody's name.
- **The eval fixtures are the only place `.example` is not required.** Public
  vendor names - Workday, iCIMS, Calendly, Instagram - stay, because the
  sorter names them. `tests/test_privacy.py` knows the difference.

---

## The list

### Music rider, next

- [ ] **The four Audiosurf character classes are not built.** Pointman's
  LIFO buffer, Vegas's shuffle, Pusher, Eraser. They all act on the
  puzzle grid with mouse clicks, and this scene has no mouse input - so
  they need an input design before they need code.
- [x] **Run the playtest over a batch rather than one record.** Done,
  over eight of different genres, and it immediately found two things a
  single track had hidden for the whole project: the road going bare for
  8.6 seconds where the drums stopped, and a tempo octave error that
  drove one track at twice its own speed. Run it one song at a time in
  one process - a previous session killed the machine by running the
  batch next to two other heavy jobs - and run it after anything that
  touches the chart, the clock or the road.
- [x] **A GPU renderer.** Done, and without the new dependency or the
  separate repository this item used to predict: Qt's own OpenGL paint
  engine ships in PySide6 and draws the same QPainter calls, so no scene
  changed. `_GpuCanvas` and `_paint_on_gpu` in `attachment_widgets.py`.
  A Retina full screen is drawn at every real pixel with 4x multisampling
  - it was half resolution, stretched, which is what "soft" was. At
  2880x1800 on an M1: 10.2 ms with every effect, against 41.9 on the CPU.
- [x] **Profile the scene at 4K and above.** Measured on the card on an
  M1 at MacBook Air, 14-inch and 16-inch MacBook Pro, 5K and 6K sizes:
  8.9, 11.2, 13.8, 22.7 and 32.3 ms at full quality. `CardSharpness`
  governs it - samples first, then the logical resolution - and a 5K
  display settles at 10 ms, a 6K at 9.6. The numbers are on the class.
- [ ] **Bigger spectacle now has a budget to spend.** The card draws the
  scene at 2880x1800 in under 5 ms, so real particle counts, a denser
  road and a proper bloom are affordable on a MacBook's own screen.
  Anything added has to be measured against the 5K and 6K rungs as well,
  because on a small card that is where it costs.

### Done in this round

- [x] **1.** `Ctrl+R` was bound twice, so Qt fired neither. Every shortcut is now
  unique, and there is a check before adding one.
- [x] **2.** The Manage Models button appeared only for some backends.
- [x] **3.** The hotel and restaurant sectors were pruned from the lexicon; they
  cost more than they earned.
- [x] **4.** Learning from corrections, per address and per domain, with a way
  to see and forget it.
- [x] **5.** User-defined sorting rules, a rule that only files and ticks now
  runs after every scan, since it touches nobody's mailbox.
- [x] **6.** Incremental scan: verdicts kept between runs, keyed on a settings
  hash so a changed model invalidates them.
- [x] **7.** Conversation threading, by references first and by subject and
  correspondent second. Offered, never enforced.
- [x] **8.** Multi-level undo, and the `COPYUID` fix that made undo work at all.
- [x] **9.** Per-account folder roots.
- [x] **10.** The preview names the phrases that fired and the categories that
  nearly won.
- [x] **11.** Accessible names on every control the window owns, in one method
  so "is anything missing" is answerable.
- [x] **12.** `gui.py` split into four files.
- [x] **13.** An empty grid says which of the three reasons it is empty, and
  offers a Clear filters button for the only one with a fix.
- [x] **14.** Preview pane below the table or beside it.
- [x] **15.** Multi-select bulk re-file, tick, untick and copy from the table's
  context menu.
- [x] **16.** Column widths and hidden columns reset from the header menu.
- [x] **17.** Fetching and classifying overlap.
- [x] **18.** A literal prefilter before every regex, 4.3× on the rules engine.
- [x] **19.** The lexicon as a memory-mapped blob, with the JSON as fallback.
- [x] **20.** The log view is capped at 2,000 blocks.
- [x] **21.** `./dev tune` for training against a real inbox, plus the privacy
  test that stops anything from it reaching the repository.
- [x] **22.** Topic ties are broken by score, then strongest phrase, then hard
  evidence, then a written precedence, not by dict insertion order.
- [x] **23.** `personal_register` no longer fires on mail from a careers
  mailbox.

### Done in round two

- [x] **Non-job mail has a menu.** The Sorting button, holding what to sort,
  what happens to the rest, and which topics get a folder.
- [x] **A row that will not tick says why**, and offers the one-click fix.
- [x] **Church is a topic**, with 161 signals, trained against a xxxx xxxxxx
  xxxxxxx xxxx.
- [x] **Form loses to subject** when the subject has real evidence.
- [x] **Every control can explain itself**, with a test that keeps it that way.
- [x] **The fixtures carry no real identities**, with a test on every run.
- [x] **CI**: tests, eval scores and a self-test on macOS; pyflakes on Linux.
- [x] **The log is `0600`** and no longer records a subject line anywhere.
- [x] **The README says what is true** - test count, accuracy, topic count,
  and where the sorting control actually is.
- [x] **The screenshot is drawn by `./dev screenshot`**, from the app, on any
  machine, in two seconds.
- [x] **The two lexicon forms are checked to be the same build**, so a
  rebuilt JSON with a stale blob cannot ship silently.
- [x] **The DMG was built, mounted, installed and run** - the installed copy
  reports itself frozen, finds its own certificates, and loads the mapped
  lexicon.

### Worth doing next

Ordered by what they would be worth, not by effort.

- [ ] **Held-out accuracy is 37.5%.** It is the only number that has not moved,
  and it is the one that describes mail the app has never seen. Start with
  `./dev tune --corrections`: every disagreement there is a real gap with a real
  example behind it.
- [ ] **Nothing learns from a whole conversation.** Threading groups messages
  and the sorter still reads each one alone. A confident verdict on one message
  is strong evidence about its siblings, and "Re: (no other context)" is exactly
  the message the sorter cannot read.
- [ ] **The corrections memory only learns folders.** It could learn that a
  sender is job-related at all, which is the more valuable half, a recruiter
  writing from a personal Gmail is the case the rules engine will never get.
- [ ] **`_grouped()` in `workers.py` opens one connection per (account, folder)
  pair.** Undoing a batch that was filed into eight folders is eight logins.
- [ ] **The verdict cache never shrinks below `MAX_AGE_DAYS`.** A UIDVALIDITY
  change silently invalidates a whole mailbox's worth and nothing notices;
  `forget_mailbox` exists and nothing calls it.
- [ ] **No test opens the built `.app`.** Every failure mode of PyInstaller
  hidden imports is invisible until somebody runs the bundle by hand.
- [ ] **`rules_engine.py` is 2,900 lines** and the signal tables are most of it.
  They would read better as data than as literals, but only if something needs
  to edit them at run time, which nothing does yet.
- [ ] **The corrections memory could learn a topic, not just a folder.** It
  already knows a church sender files to Church; it does not yet conclude that
  the *next* message from that sender is church mail. That is the mechanism
  that would have caught the two parish notices with no church vocabulary in
  them at all.
- [ ] **The app is signed ad-hoc**, so Gatekeeper refuses it until somebody
  right-clicks and chooses Open. A paid Apple Developer certificate and
  notarisation would remove that, and it is the single biggest thing standing
  between this and "double-click to install".
