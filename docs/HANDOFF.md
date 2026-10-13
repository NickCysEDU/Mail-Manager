# Handoff

Notes for whoever works on this next: where things are, what breaks if it is
forgotten, how to measure, and what is open. The [handbook](HANDBOOK.md)
describes the app itself.

## Where things are

- **Mail:** `imap_engine.py` (IMAP), `rules_engine.py` and `rulesets.py` (the
  built-in sorter), `llm_engine.py` and `providers.py` (models), `models.py`
  (routing, no Qt), `gui.py`, `triage_table.py`, `settings_dialog.py`,
  `mail_window.py` (a message's window and the compose window),
  `webview_mac.py` (messages drawn by WebKit), `sidebar.py` (the
  mailboxes down the left),
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

- **A model never says its rows moved without warning first.**
  `layoutChanged` without `layoutAboutToBeChanged` freed the filter's map
  while the table's current row pointed into it, and the next click on a
  message crashed (`test_no_crashes`, which also checks the source).
- **An application-wide event filter is one object for the application.**
  One per window put every event through a filter for each window ever
  made: the suite, which makes hundreds, crawled, and workers crashed in
  app-wide font changes (`OpenWindows.serve`).
- **A menu row the pointer rests on opens** (`widgets.RestingOpensMenus`,
  installed in `main.py`): Qt's Mac menus lost the row after a visit to
  another row's submenu, whatever the style hints said.
- **The type comes from the system's font each time** (`theme.base_font`):
  built on the application's, the reading layout grew it with every change
  and never gave it back.
- **A menu rebuilt from its own entry's signal frees the old entries
  later** (`deleteLater`), and makes them its own children: made by the
  window, forty actions and six menus were left behind on each rebuild.
- **No running QThread may be a closing window's child.** Qt aborts the
  process when one is destroyed. The viewer's fetches were its children,
  so closing it while a part was still coming took the app down (found when
  CI's reaping of widgets hit it). They are kept in
  `attachment_view._FETCHING` until they finish, cut off from the viewer
  when it closes (`_let_fetches_go`), and waited for at quit, after the
  main window has closed the viewer and with it the connection they read.
  A thread still running when Python exits aborts it all the same;
  `shiboken6.invalidate` does not stop that. The viewer's copies of parts
  (`_temp`, mode 700) go in its sweep, which skips the widgets a reaped
  window has lost and still deletes the files, and whatever no sweep
  reached goes at exit (`_COPIES`, `atexit`): the app could quit before a
  closing viewer's sweep came round.
- **Nothing the app writes outside its folders outlives what it was for.**
  The viewer keeps one copy a pane, the player's and the PDF view's, and
  lets the last go as the next comes (`_let_go_of_copy`). Each viewer's
  folder is claimed with its process (`leftovers.claim`), and at every
  start a thread sweeps the temporary folder of `mm-attach-` and
  `mm-update-` folders whose process has gone, or unclaimed and an hour
  old, never one still mounted (`main._clear_leftovers`,
  `leftovers.sweep`). An update that fails or is stopped removes what it
  downloaded and unpacked beside the app, and a start removes one left
  ten minutes, unless it holds the old app (`updates.clear_stale_staging`).

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
- **What a person writes, and what only borrows its words** (`rules_engine.py`,
  tests in `tests/test_reading_people.py`). Stage phrases a form, a lender, a
  shop or a friend writes as readily as an employer (`GENERIC_STAGE_PHRASES`:
  "please complete", "your offer", "introduce you to", "no action is
  required") say which stage only once something says hiring. A candidate
  platform writes for employers (`PLATFORM_SENDER_DOMAINS`); a people team
  writing about the job the reader already has is work mail (`_CURRENT_JOB`,
  unless `_BEING_HIRED`), and a school's, a hospital's or a court's own people
  team is an employer, not another world (`_EMPLOYER_SIDE`, computed once as
  `elsewhere_sender`). A job title applied for (`_APPLIED_FOR_A_TITLE`), an
  agency and "your onsite" (`_YOUR_ONSITE`) say hiring by themselves; an
  offer's business from a person writing for an organisation in their own
  name (`statements.SOFT_EVIDENT`, `_NAMED_MAILBOX`) is read without a word of
  hiring and held below filing, since a house has offers too. People's news
  has its own readers: networking (`networking_score`, which needs
  `_CAREER_CUE`, never fires on being asked to find somebody, inside a
  process or beside a programme being sold, `_PROGRAMME_SOLD`), headhunting
  (`headhunt_score`, its later families only beside `_HIRING_ANCHOR`) and a
  recruiter's own follow-up (`_follows_up_itself`). Ahead of all of it,
  `elsewhere_now` vetoes the job reading: another world's application, the
  reader interviewing, an interview for a programme or a publication
  (`_MEDIA_INTERVIEW`, `_ON_A_PROGRAMME`), money asked for the way a swindle
  asks, a school's or a board's list; a scam domain, words written to a
  machine, a one-time code or a password reset, and a sale to people looking
  for work (`_SALE`) zero the job evidence. In bulk mail an interview nobody
  is offered is discounted. A digest of several applications is job mail of
  no one stage.
- **Church mail follows the prompt, and only when the sender is a church.**
  `church_sender` returns the word and whether it settles it: a
  congregation's own name does; a church word run into a domain, a
  denomination alone or a saint's domain count only beside churchly words
  (`churchly`) or a death notice (`mourned`, held below filing). Never a
  hospital, a school, a government, a firm the lexicon knows, a civil parish
  or a person called Church. Church mail then beats every topic but security
  and spam, and never in a scam's shape; a sender that may be a church and was
  not confirmed keeps its verdict under `SOFT_EVIDENCE_CEILING`.
- **A swindle borrows a voice.** `favour_scam` (gift cards, their codes sent
  on, pressed for) and `job_swindle` (a job paid out through the reader)
  discount the personal, work and church words they wear; the favour scam
  then outranks them outright. Both are flagged, not junked unasked. The
  same idea for the everyday topics: no courier or shop writes from somebody's
  own mailbox (`_MAIL_PROVIDERS`), or bills from one and asks to be rung
  about the charge (`_CALL_ABOUT_A_CHARGE`); a parcel is not held for a
  fee of a few dollars paid "here" (the last alternative of `_SWINDLE[2]`);
  and the reader's pay is their employer's (`_PAYROLL`).
- **Every guard in the sorter was broken on purpose and a test failed** - 130
  of them in the October work and 49 of 51 for Transactions (the two left
  are below), one test per way of saying a thing (a test
  that says it three ways passes with any two). Run each mutant with
  `PYTHONDONTWRITEBYTECODE=1` and the target's `__pycache__` cleared: two
  same-size mutants of one file written in the same second run the first
  one's bytecode, and a guard looks tested when it is not.
- **Write a held-out set, then do not write rules from memory of it.** The
  same hand writes the sets and the rules. Two pattern alternatives added
  after the third set was written echo its wording with no dev-set source
  (`_INTERVIEWER` "run the technical part of the interview",
  `_INTERVIEW_OFFERED` "pre-selected to interview"); one more ("pay period")
  was caught and taken out before the fifth was measured. Before measuring a
  set, compare every code fragment added since it was written against its
  text and the dev data: a fragment found only in the held-out set is an echo.
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
- **Transactions is money or goods already on the move; Finance is what is
  still to come.** Receipts and Shipping became one topic on 12 October, and
  a payment confirmed by a bank moved out of Finance. Words that only say
  whose money it is (a card ending in, a balance, a direct deposit, autopay,
  a bank's domain: `MONEY_ACCOUNT_SIGNALS`) go to whichever of the two the
  rest of the message is about, and to Finance on a tie or when no payment
  is reported (`says_money_moved`, which leaves out `_SPENT`: "charged" is
  said of batteries). Weak words of the two old kinds now add up, so a
  phrase either side could shrug off can now decide: "track your" (a site, a
  habit) and a bare "paid" (a paid subscription) were narrowed for that, and
  the SpamAssassin corpus was compared verdict by verdict against the commit
  before. Old names still parse (`models._MERGED_TOPICS`) in saved settings,
  cached verdicts and a model's answer; folders already made are left alone,
  and the verdict cache's recipe carries the topic list
  (`verdict_cache.TOPICS`), so the first scan after a change like this reads
  everything again. Untested: the solicitation discount on Transactions,
  renamed from the two old topics and never exercised; and putting
  "requested" back into `_MONEY_IN` changes no verdict, because
  `_MONEY_ASKED` still wins.
- **A business's own record of money that moved is not junk for its trade**
  (`worded_spam`, discounted unless the shape says scam or the sender is a
  free mail account): the words that mark junk elsewhere are a trade's own
  words in its receipts, a casino's, an exchange's or a bank's wire.
  What counts as moved (`says_money_moved`) needs an amount said as one
  (`_AN_AMOUNT`): "once you have received 100 orders" is a chain letter's
  count. Untested: discounting inside a scam's shape too would change no
  verdict, since the shape outscores the transaction it wears.
- **The sets were relabelled by a rule written before any output was read**
  (12 October): RECEIPT and SHIPPING became TRANSACTIONS by search and
  replace, and Finance only where the message confirms one movement of money
  that has happened. The held-out sets' Finance rows were judged after the
  engine was frozen, and are listed in `tests/test_sorter_safety.py`.
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
- **`./dev demo|run|dry` start the app through macOS** (`launch()` in
  `dev`), from a bundle in `build/dev.noindex/` that runs the checkout, as
  the built app is started. Run straight from a terminal the process has to
  make itself active, and now and then macOS made it the front app without
  activating it: in `log show --predicate 'eventMessage CONTAINS
  "SETFRONT"'` the app came to the front and nothing followed, no Dock round
  trip (`MenuBarWaker` waits for an activation, and none came), no Touch
  Bar, no text cursor service. A panel opened from the View menu then came
  up with its sidebar grey until a click activated the app; launched through
  macOS it comes up active. The bundle asks for native execution
  (`LSRequiresNativeExecution`): an app whose executable is a script is
  otherwise run under Rosetta on Apple silicon, Python with it, and the
  Keychain prompted for the other slice. The terminal's environment goes
  over in a file the bundle sources and deletes, never as `open --env`,
  since any account can list a command line, with each value single-quoted
  (`%q` let bash expand a `~` in PATH). `tests/test_dev_launch.py` runs it.
  Away from a terminal, `./dev` runs Python directly, as before.
- **The inbox opens newest first, every time** (`_restore_geometry`): the
  header's saved state carries its sort, and a sort by category or oldest
  first came back on every launch. A header clicked sorts for the session.
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
- **Restyling the app costs about a third of a second** (`theme.apply` sets
  the stylesheet on every live widget). Settings previews a look as it is
  chosen, but not while it fills itself in (`_loading`), and Cancel puts the
  look back only if something was previewed (`previewed`): it opened in a
  second before, and every model chosen without a key opens it. The arrows'
  style is built once (`theme._arrows_in_place` finds it under the
  stylesheet's own style): asked only by type, every change of look built a
  new one and restyled every widget once more, a fifth of the time
  (`tests/test_theme.py`). A test that puts back an empty stylesheet leaves
  the arrows' style as the application's own, which the app never does; a
  look laid over that draws arrows without it, and setting another style
  from there crashed the process. The arrows are checked in a process of
  their own.
- **Hover help waits in some windows** (`helpmode.PATIENT` on the message,
  compose and viewer windows): with the ? off, a control explains itself
  after the pointer has rested on it for `PATIENT_DELAY`; with it on, at once.
- **Every window of its own carries a menu bar** (`mail_window._app_menus`).
  On a Mac the menu bar belongs to the active window, and a window without
  one leaves the main window's up, whose key equivalents Cocoa fires
  regardless of Qt's shortcut context: ⌘R would scan from a message window,
  ⌘↩ would file from the compose window. The bar needs the app roles too
  (Settings, About, Quit), or the app menu loses them.
- **The background scan finds its own certificates** (`main.py`,
  `--scan-once` calls `certs.ensure()`): launchd starts it with nothing
  set, and the bundle Python was built to look for exists only where it
  was built, so without it the agent could verify no server.
- **The Dock's list of windows keeps its own** (`OpenWindows._seen`, the
  windows the app made, as each was shown). Asking for every top-level
  widget now and then met one whose memory was no longer a widget, and the
  process went down with it (`test_the_list_keeps_its_own_windows`). Two
  such walks remain, checked on the real screen and left alone:
  `touchbar._focus_changed` (cocoa only) and `widgets._menu_at` (a pointer
  resting in a menu).
- **The agent's job follows the app's home** (`scheduler.agent_plist_path`):
  with `ICLOUD_TRIAGE_HOME` set it is written there and launchd is never
  asked. launchd starts a job without the moved home, so one written from a
  test would scan the real account every day (`tests/test_scheduler.py`).

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
- **A forward's attachments are part of it** (`ComposeWindow.carry`): they
  arrive after the window opens, and a forward nobody touched still closes
  without asking whether to keep it. Counted as written, they made closing
  ask, and in the tests the unanswered question kept the window open into
  later tests, where it turned up in the Dock's list of windows. The test
  fixtures now throw away anything left half written.
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
  even in a test. Invent the whole message, not only its people: the
  wording, the role, the times, dates and IDs. A test that keeps a real
  message's shape with its own words still tells who wrote it. Before
  committing, compare the added lines with the private sets and the inbox
  (six-word runs, and numbers) with the scans kept beside them.
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
  width comes from its layout, set on first show. A fresh reading
  (`_budget_settled`) invalidates the layouts, which asks for a layout
  again: another is asked for only when what it reads has changed
  (`_layout_reading`), and none while the picture is full screen. Without
  both, the window behind a full-screen picture read it 1,100 times a
  second, all the while.
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
  (`Tunnel._throw`) and flickers the city's windows. A strobe hit's pulse
  crosses faster the more is going on in the part it is sent in
  (`PULSE_HURRY`, by `Section.level`): its own pace in the calmest part,
  twice that in the busiest. A snare's keeps to the beat.
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
- **On the card the bloom is worked out there** (`glow_gl`,
  `PostProcess.glow_on_card`): the frame halved, blurred by a Gaussian and
  added back with its two fringe copies, nothing read back. It was an
  eighth-size copy stretched back up, which glowed in blocks: along a thin
  line its middle wandered 2.2 real pixels off the line, and at full screen
  the rave's wireframe tiled and crawled as it turned. Keep its width at
  what the blocks spread it (`glow_spread`, half a block): a softened halo
  (two binomial passes, wider) took the neon off the wireframe. A card that
  will not build it gets the small copy (`_halo_on_gpu`, `last_halo`),
  logged once; the CPU path still stretches a small copy. What made the
  rave look bad in a window was its haze, stretched from a small tile, and
  `HAZE_DOUBLINGS` is the fix for that. The glow is as wide in points at
  any pixel ratio (`glow_spread`), so a single real pixel at 1x where it is
  two at 2x: a test of how far it reaches measures within that spread, not
  a fixed count of real pixels. One that looked one to five rows below the
  lit band failed on the hosted runner, whose screen is 1x, and passed on
  every Retina Mac. `QT_SCALE_FACTOR=0.5` makes a Retina Mac's screen 1x for
  a run, which is how it was reproduced and how to check such tests.
- **On a hosted runner, showing a window keeps drawing for a while:** the
  window server's exposes arrive late, and each makes Qt draw the canvas. A
  test that counts frames waits until the canvas has drawn nothing for half
  a second before it changes anything (`test_a_widget_over_it_changing_
  asks_for_no_frame`), not a fixed second.
- **macOS's spelling service can be slow to answer on a hosted runner:**
  the Touch Bar test of the words it offers once found none within five
  seconds there; it now waits up to thirty, ending as soon as they come.
  Why it was slow was not found. With the words never shown it still fails.
- **A wide line on the card is a real pen** (`visualizers.stroke`). The
  stack of hairlines is a CPU speed-up; on the card each pass went through
  the samples on its own, and the wireframe came out ridged down each line
  and lit twice where lines crossed.
- **Scene changes** fade over the last frame on the GPU (`_GpuCanvas.hold`,
  `cover`); a change during a change folds the two frames first. The CPU path
  fades from the background.
- **On the card, frames start at the screen's refreshes**
  (`refreshes_mac`, `Spectrum._refreshed`): a display link the window's
  view makes (macOS 14 and later) calls on the main run loop at every
  refresh, and the ticks owed are counted from the refreshes' own
  timestamps, exactly one a refresh within a tenth of sixty. The timer
  then only watches, and takes the frames back after `REFRESH_LOST` ticks
  without one, and whenever frames are paced slower than sixty, during an
  analysis, off the card or hidden; the refreshes have them again only
  after `REFRESH_STEADY` in a row at the screen's pace (a hosted runner's
  virtual screen sent two in a second and a half, and took the frames at
  each). A sleeping screen sends none, so
  `TestTheScreensRefreshesKeepTheFrames` skips there and on a hosted
  runner, by what the pane's own window is sent through a display link of
  the runtime's beside the pane's.
  `requestUpdate` on the window was
  tried first: a widget window answers it by repainting all of itself, so
  refreshes came every 28 ms and the canvas was drawn twice. A pane's link
  goes with it (`weakref.finalize`): a window taken away sends no refresh
  to find the pane gone.
- **The music's clock moves a sixtieth a tick on the card** (`_heard`'s
  `advance`), leaning on the player by at most `LEAN`; time the frames
  skipped (`_skipped`) goes into the next tick, or a stall left the blocks
  behind the beat for a second while it crept back.
- **What changes beside or over the picture changes just after a frame**
  (`Spectrum.after_the_frame`): the full-screen bar's seek and clock
  (`FULL_BAR`) and the window's waveform and clock (`TRANSPORT`). Changed
  just before a frame, the window was put together twice in one refresh
  and the frame shown a refresh late; changed in the same pass as the
  canvas, Qt repaints the whole window (seen with a bare `QOpenGLWidget`
  and `QSlider` too), every control on the bar. The seek bar draws itself
  only when its handle moves a pixel (`SeekBar.sliderChange`), the bar's
  opacity effect is on only while it fades, the pane's paint no longer
  asks the canvas for another frame, and a canvas asked again with
  nothing new puts up the picture it drew (`_picture_due`).
- **One frame in four waits for the card** to be measured
  (`MEASURE_EVERY`); every frame waiting held the main thread 9 to 12 ms.
  That frame starts with the card idle, too: nothing else waits for it now
  the bloom is not read back, and measured with earlier frames still
  queued, the rave drawn frame after frame read 13 ms against its own 8
  and gave up half its samples.
  The full-screen controls no longer halve the rate on the card
  (`GIVE_WAY` is for the CPU path).
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
- **A playhead gone back is a jump, counted or not** (`Rider._advance`).
  The pane counts seeks, not a track started again from the top or the
  next one after the end, and the road kept the last pass's beats: decided
  again from beat 0, it stopped short of them, and building `_marks` across
  the gap raised (`KeyError: 15`, 13 October), which put the flat ride on
  for the rest of the session. `_decide_lunges` also starts its tables
  again if asked from behind them.
- **The world is let go after `Spectrum.WORLD_SLIPS` bad frames,** not one
  (`_paint_scene`): a bad frame is drawn flat and the world tried again.
- **The flat ride is drawn as the world is** (`Rider.paint`): the sky, sun,
  hills and stars (`_sky`), towers either side (`_pillars`, `_tower`), a
  jet (`_ship`, `FLAT_JET`) and the world's own numbers (`rider_gl.Hud`,
  through `_flat_numbers`; the old card only if that will not load). It is
  also what draws on the card when the world fails or is not wanted, and
  the painter on the card has none of the advanced blend modes: Multiply
  there drew the hit's veil as a pale sheet over the frame and logged
  "Unsupported composition mode" every frame. Use SourceOver, Plus and the
  plain Porter-Duff modes only in anything that may be painted on the card.
  The strobe is eased and held below `FLAT_FLASH`, an answer's edge flash
  is held under `POP_FLASH_MOST`, and a corkscrew's tunnel is as dark as the
  world's; at full strength each of them flashed the whole picture. The
  towers' sky behind the road is kept dim enough to read a block against
  in the brightest passage (`TestTheRiderUnderAPlaythrough`).
- **Option-Shift-C while the rider is on screen draws it flat by choice**
  (`AudioPane.classic_chord`, `toggle_classic`, `Spectrum.set_classic`),
  kept in `viewer.json` as `classic_rider`. Deliberately not in the keys
  list or the handbook: a hidden setting, two modifiers so it is never hit
  by accident, and nothing in any other scene.
- **A corkscrew's turn is in the road samples, not the camera;** towers stand
  on the road with the corkscrew's roll taken out unless inside the tunnel.
- **The city stands upright** (`onRoadUpright`): a tower takes neither the
  road's bank nor a corkscrew's turn, or the skyline twists whenever the
  road does. **The eye is kept clear of the road** between it and the craft
  (`RiderWorld._clear_of_road`), or a crest, or the road behind the craft
  turned further than the craft, put the road through the camera. **A
  craft is one rigid piece** (`onRoadAs`, mirrored by `RiderWorld._rigid`):
  placed in the road's frame where it stands, not point by point along the
  road's line, which bent it over a crest. **The view opens with a punch
  on a spring** (`RiderWorld._widen`, `PUNCH_SPRING`), a kick's or a
  prize's: opened in one frame, it jumped up to six and a half degrees at
  a block taken, and the craft jumped with it, which players saw as the
  craft stuttering. The lights and the flash still come in one frame.
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
  The game chosen is kept across tracks (`AudioPane.GAME_PREF`).
- **A colour taken breaks up where the craft met it**, gone in `DISSOLVE`
  (`rider_gl._breaking`, and `Rider._fizzle` on the CPU path), so it comes
  on whole rather than being drawn into the ship; a coin or a power-up
  still is. A colour that goes by is recorded "missed" and goes on past and
  off the road, as a grey dodged does: it used to be the one that broke up.
  A take is the commonest pop, so on the card it throws `PRIZE_DIM` of the
  light the rarer ones do, and its break-up stays near its own colour
  (`BREAK_HEAT`, `BREAK_WHITE`, `BREAK_SPARK`): at full light and
  white-hot, a run of takes washed the road out. Measured on the card as
  the light a take adds over the same ride with all four at nought, the
  values in place leave about a third of it. The craft's size test clears
  the road, since a take just before its frame hid part of the craft.
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
- **No `NSPickerTouchBarItem` and no `NSSliderTouchBarItem`:** both log
  an AppKit layout complaint, which the native tests fail on. Segmented
  controls and a plain `NSSlider` beside a label do the same jobs.
- **Nothing on a bar is held or dragged.** 1.9 had a popover's
  press-and-hold bar (an `NSSliderTouchBarItem`, opened beside the finger
  through AppKit's private overlay); it was taken out in 1.10 because a
  held button drove something other than what it was named for, and its
  slider stuttered. A popover opens on a tap; `test_touchbar_mac` checks
  that none has a press-and-hold bar.
- **A knob under a finger is never sent its value back.** Each step of a
  drag reaches Python after AppKit has drawn it; sent back, the value
  arrived after the finger had moved on and pulled the knob to where it had
  been, so the knob stuttered behind the finger. `Bar.press` records what
  AppKit shows instead of sending it, and the renderer leaves a knob alone
  when it is within half a step of the value (a finger leaves it between
  whole values). After `HELD_FOR` the bar looks again, once (`_let_go`).
- **The visualiser's bar fits beside the Control Strip:** 685 points for
  the app's items with the strip folded, 613 inside a popover once its close
  button is in. Measured in AppKit's own bar appearance, in which a button
  is never narrower than 72 points (`test_the_visualisers_bar_fits_beside_
  the_control_strip`, every scene, windowed and full screen). What does not
  fit gives way by priority: Seek is `lower`, between normal and low, so a
  scene's own button keeps its place; back, forward and the viewer's own
  buttons are low.
- **Suggestions while writing** (`touchbar.Suggestions`, behind the
  writing window's speech-bubble button) are macOS's own QuickType words:
  `NSSpellChecker`'s `requestCandidatesForSelectedRange:…`, asked for the
  paragraph the cursor is in, positions in UTF-16 units as Qt and AppKit
  both count them. macOS answers on a thread of its own, to one block made
  for good (`_answer_block`); the answer is retained there and queued to
  the main thread through `_Answers`, and only a list's latest request is
  shown. A word chosen replaces what it was offered for, unless that
  paragraph has changed since (`Suggestions.act`).
- **The writing window's bar wants 833 points beside the Control Strip's
  685** (it wanted 753 before Suggestions): Link (`low`) and Numbers
  (`lower`) give way, Suggestions is `high`. Bold, Italic and Underline in
  one segmented control would save 75 points (180 against 255), the two
  list buttons 8. `test_the_writing_windows_bar_fits_beside_the_control_
  strip` measures it as the visualiser's is measured (`MEASURING`).
- **AppKit draws a slider's knob on the bar as a square:**
  `Renderer._polish` rounds every `NSSliderKnob` (`KNOB_ROUNDING`,
  continuous corners) on every push of a slider's state, giving a knob its
  own layer first: a bar not yet shown has none. Found by class name, so on
  a macOS without the class nothing changes.
- **The live bar can be read in-process** while the Touch Bar is awake
  (`+[NSFunctionRow isDynamicFunctionRowAvailable]`): item views sit in an
  `_NSFunctionRowPanel`, and `renderInContext:` of the root view's layer
  draws what is on the glass. Asleep, with nobody at the keyboard, nothing
  is hosted, and with the lid shut there is no bar at all.
  `tests/test_touchbar_mac.py` renders a slider in
  `+[NSAppearance _functionRowAppearance]` in an off-screen window and
  checks the knob's corners in the pixels. On the hosted runner (macOS 14)
  AppKit says it "did not get back knob metrics" for that look and draws no
  knob at all, its own sliders' included; the test skips there only when a
  slider AppKit made itself has no knob either. Whether a real macOS 14 bar
  draws the knob is not known: no such Mac has been tried.
- **A hosted runner draws the visualiser very slowly** (each step of the
  bar's measurement took over five seconds, against a fifth of one here;
  on 9 October one under Rosetta measured every scene windowed inside two
  minutes and was still going full screen when they ran out, so that test
  has more time).
  A test that measures the bar stops the picture's frame clock, measures
  one item at a time (a host keeping every item laid them all out again for
  each new one, each slider logging AppKit's knob warnings on macOS 14),
  and shows the full-screen window as a window: the bar is the same, and
  under Rosetta a runner never finished moving it into a Space of its own.
  `_run` reports what the probe last said when it runs out of time.
- **Tests that use hdiutil take turns** (`conftest.hdiutil_alone()`, a lock
  across the test workers): with the image layout's tests making and
  mounting images on one worker while the updater's opened one on another,
  the disk image service failed a call, "Device not configured", for longer
  than the retries waited.
- **A real-screen script stays behind** unless its test is about coming
  forward: `setActivationPolicy:` 2 (prohibited) right after its
  `QApplication`. Brought forward, each took the focus from whoever was at
  the Mac (the Touch Bar's file twenty times a run) and from other tests:
  another app coming forward closes an open menu, so the model menu's test
  failed whenever such a file ran beside it. The graphics card's scripts
  (`test_gpu_canvas.HEAD`, the rider's too) still come forward: a window
  that cannot be in front is never given a frame to draw. The menu bar's
  tests come forward on purpose.
- **The model menu's test watches the front** (every 25 ms, NSWorkspace):
  a try during which another app came forward says nothing either way and
  is made again, up to three times, then skipped with what came forward.
  With nothing else coming forward, a menu that closes or a row that does
  not open fails it. A lock making real-screen tests take turns was tried
  instead: it pushed the hosted runner past its thirty minutes, where the
  Touch Bar's measurements under Rosetta take minutes each.
- **Nothing inside a popover opens another.** A popover or a menu-style
  choice in a popover's bar closed the whole bar back to the top when it
  opened (the visualiser's Game held its game and level as menus);
  `touchbar.Popover` refuses such children. Choices inside a popover are
  segments; a list of them goes at the top level.
- **Under Rosetta a scrubber in a popover of several items aborts the
  process** (AppKit: "Attempt to use unknown class"); natively it works.
  The visualiser's shape and strobe-source lists are top-level popover
  choices for that reason as well.
- **A bar's items are set again only where they changed** (`_Handle.arranged`):
  `setDefaultItemIdentifiers:` resets a bar, and an open popover with it.
- **The volume is the Control Strip's.** The visualiser's bar has none of
  its own; the game sounds' level stays, in the Game sounds popover.
- **ctypes callbacks cannot return structs,** so a scrubber's entries share
  one width (`_fit`). A scrubber keeps its count until `reloadData`.
- **The suite cannot see the bar.** `tests/test_touchbar_mac.py` runs on the
  real platform, natively and under Rosetta, and checks what AppKit holds;
  `screencapture -b` needs Screen Recording permission.

### Reading mail

- **Messages are drawn by WebKit** (`webview_mac.MessageWebView`), in a
  `WKWebView` put in the Qt window with `QWindow.fromWinId` and
  `createWindowContainer`. `triage_table.message_view()` chooses it on the
  Mac's own platform and falls back to Qt's `MailView` offscreen, in the
  suite, or if WebKit will not start; both take the same calls. Qt's
  engine knows little CSS: a newsletter came out with its phone and its
  desktop layouts one under the other.
- **What a message may do:** its HTML is sanitised first
  (`sanitise_for_view`; with pictures on, `keep_urls` keeps a style's
  pictures); page script is off (`allowsContentJavaScript`); a
  Content-Security-Policy in the page lets in inline style and pictures
  alone, and pictures only when wanted; the data store keeps nothing; link
  previews are off (a firm press loads the page). Every navigation but the
  app's own load, a reload and a jump within the page is cancelled; a link
  clicked is handed to the app (`anchorClicked`), which shows where it goes.
  Loads from `loadHTMLString` leave no history, so going back cannot show
  another message (tested). A renderer that dies is redrawn once, then the
  message is shown as text: a message built to crash WebKit would loop.
- **The sanitiser never gives up on a message:** Python's parser raises on
  a "<![" that opens no section it knows, so a second pass shows each as
  text, and failing that the message's words are shown.
- **A window's title is a sender's words** where it is a message's
  subject: the Dock's list shows it on one line, shortened, with "&" as
  itself (`OpenWindows.entry_for`).
- **Blocks from ctypes:** a global block (`_BLOCK_IS_GLOBAL`) is made per
  call and kept until it has returned; freed inside its own call, the
  trampoline went with it and the process with that. The decision handler
  is answered exactly once, whatever happens.
- **The view is retained once and released on `destroyed`;** Qt keeps its
  own hold on a foreign view for the container's life.
- **The app's own script still runs** with page script off
  (`evaluate`): it keeps the scroll place when more of a message arrives.
- **A message cut short by the scan's 64 KB read is read whole when shown**
  (`MainWindow.read_whole`, `IMAPEngine.fetch_whole`, up to `WHOLE_BYTES`,
  read-only): about one HTML message in eight was cut. Once a message;
  after a failure not again for `WHOLE_RETRY` seconds, and the view says
  what is missing (`missing_from`).

### Mailboxes

- **A folder moves with one RENAME** (`IMAPEngine.move_folder`), refused
  before anything is sent for the inbox and the special mailboxes, a move
  into itself and a name already taken. The sidebar asks for it
  (`moveRequested`) from a drop or **Move To**, and never rearranges its own
  tree: the server's word redraws it. Qt only moves items itself when a
  drag's source is the tree, which no test can make without a real drag,
  so that the override stops it is unchecked by the suite.
- **While a folder is held, a bar shows where it will be listed**
  (`MailboxList.landing`, `_rows_inside`): the rows are alphabetical, so the
  place is worked out from the names the server will have, not from where
  the pointer is, and the folder it goes into is outlined. Qt's own drop
  indicator is off. Near a row's top or bottom (`EDGE`) a drop goes beside
  the row rather than into it.

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
./dev test                     # 5,763 tests
./dev playtest ~/Music/*.mp3   # real records through the real pane
./dev eval                     # the sorter on a labelled set
python tools/corpus.py         # the SpamAssassin corpus
```

`tests/test_monkey.py` is somebody who has never seen the app: hundreds
of seeded random clicks, keys, filters, looks, menus, dialogs, message
windows and visualiser changes on the demo, in a process of its own;
`MONKEY_SEED` and `MONKEY_STEPS` replay or lengthen a run. `python
tools/stress.py` throws hostile input at every parser, the viewer's
sanitiser and the page WebKit is given included; it found the viewer
showing nothing of a message with a stray `<![`.

A test's subprocess has a timeout under the suite's own sixty seconds: one
longer was left running, a core at full tilt, when pytest gave up first.

`./dev playtest` reports where blocks land against their beats, the road's
speed, and frame cost. Run it one record at a time. No song or frame of one is
ever written into the repository.

Frames are measured against the screen. A script outside the repository
plays a record muted in the real viewer, notes every `frameSwapped` and the
tick each frame drew, and takes the refreshes' times from a CVDisplayLink;
for each refresh it finds the newest frame handed over a little before it,
and counts the refreshes that showed exactly one new tick. Run one record a
process. On this Mac (60 Hz), with the deadline up to 2 ms before the
refresh:

| | timer (1.12.0) | refreshes |
|---|---|---|
| Rider, window | 91.0% | 99.3% |
| Rave, window | 95.0% | 99.5% |
| Rider, full screen | 99.4% | 98.7% |
| Rave, full screen | 100% | 99.3% |
| Rider, controls up | 30 a second | 99.3% |
| Rave, controls up | 30 a second | 98.9% |

The timer's figures depend on where its phase falls in the session; at the
worst place for the deadline, they were 64 to 71 per cent.

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
| labelled (102, dev) | 100% | 94.1% | 72 of 73 |
| adversarial (39, dev) | 100% | 97.4% | 5 of 5 |
| stress (248, dev, written 11 October) | 97.2% | 85.1% | 82 of 82 |
| retired held-out (434, dev now) | 98.6% | 93.5% | 183 of 183 |
| held out (116, measured at its start and once at the end) | 94.8% | 84.5% | 43 of 44 |

A private set from a real inbox, split before anything was read into 508 to
study and 292 held out, labelled by hand before the sorter ran on it, lives
outside the repository with the scripts that score it the way a scan does
(`~/.mail-manager-eval/inbox/score.py`). Held out, measured once against the
version before: exact category 72.3% to 88.7%, job mail filed without asking
92 to 137 of 152, none of it into the wrong folder either time. Three
everyday messages were taken for job mail at full confidence; two were a
talent community's invitations, read as an interview and an offer by
patterns that have since been tightened, so the held-out figure after that
(89.4%) is not a clean one. Measured once more on 11 October, after the
sorter learnt to read what people write: 89.7% exact, 137 of 152 job
messages filed, none into the wrong folder, one everyday message still filed
as job mail.

On the SpamAssassin corpus, none of 4,150 genuine messages is filed as job
mail. That must stay at zero. 93 of them are read as job mail at any
confidence (118 before 11 October), 3 are sent to junk.

## Releasing

Set `APP_VERSION`, build with `./dev build` (which signs with the local
certificate from `tools/make_signing_identity.sh`), `./build_dmg.sh`, tag
`vX.Y.Z`, and attach the `.dmg` to a GitHub release. The updater only installs
a build signed with the same certificate as the running copy, with the same
bundle ID and the version the release says, and checks the download against
the size and SHA-256 GitHub reports.

The image's window layout must name nothing of the Mac that built it, and
Finder's own records it with the working image's path, the start-up disk's
name and a disk UUID. `tools/dmg_layout.py` cleans it and checks the finished
image; `build_dmg.sh` removes an image that fails. Finder ignores a
`.DS_Store` written afresh by ds_store (a record-for-record copy came up with
default icons and window), so the cleaning edits Finder's file in place and
then zeroes every byte the file does not use: Finder's earlier copies of the
records lie in its freed blocks. After changing either, open the image and
look: the window's size, the icons' places and the background picture.
Finder reads the picture through the alias alone; its access time moves
when the window opens, and does not without one.

The image carries no extended attribute but Finder's, and no record of its
writes. macOS marks every file a build writes with where it came from
(`com.apple.provenance`), and no process started by the build may take the
mark off; `build_dmg.sh` has launchd run the removal (`unmark`), last of
all, with `xattr -s` so that links lose theirs too and what a link points
at (the Mac's own Applications folder) is never touched; without it the
image's links kept the mark. `.fseventsd/no_log` is staged, so fseventsd never records what is
written to the image (without it the image carried the build's record).
`dmg_layout.py check` fails on either.

The build's label is the bare commit (`git describe --exclude='*'`): with
tags, every build after an annotated tag was named from that tag.

Private vulnerability reporting must be on in the repository's settings, or
About's security link is a 404 for everyone but the owner.

## Open

- **PySide6 6.12.0 is kept out** (`requirements.txt` caps it below 6.12).
  On it the suite loses test workers: a segfault inside PySide converting
  `QApplication.topLevelWidgets()` for `OpenWindows.windows()` (the native
  stack ends in `PySide::getWrapperForQObject`), and an abort while
  `reap_deleted_widgets` deletes widgets. pytest-xdist then waits for the
  dead workers for ever: CI ran into its 30-minute limit at 99%. Reproduced
  here with CI's exact versions. Before lifting the cap, run the whole suite
  on the new version; `OpenWindows` could keep its own list of windows
  rather than ask for every top-level widget.
- **6.11.2 has the same crash, rarely.** CI's failure on 7e5b298 was this,
  not the screen. On 9 October two of four full runs
  here lost a worker to it: a bus error in `OpenWindows.windows()` and a
  segfault in `theme.apply` while PySide reported an error from a combo
  box's `changeEvent`. Then xdist either waited for the dead worker or
  stopped with an internal error (`KeyError: <WorkerController gw4>`). The
  native stacks say what is wrong: `topLevelWidgets()` hands back a pointer
  whose memory is no longer a widget, with no destructor on the stack, so
  something freed or overwrote a live widget earlier. Not the cause: a
  widget dropped on a worker thread (PySide deletes it on the main thread),
  images over borrowed buffers (there are none), the CoreAudio reads in
  `av_sync.py`, the Touch Bar and web view code (cocoa only). One worker's
  files run in one process in this order, `test_models`,
  `test_imap_engine`, `test_window_tools`, `test_providers`,
  `test_attachments`, `test_config`, `test_gui_dialogs`, `test_gui`,
  `test_rider_world`, `test_mail_client`, `test_viewing_modes`, brought it
  out in `test_mail_client` once in three runs with `MallocScribble=1
  PYTHONMALLOC=debug`, and not in three with `MallocNanoZone=0
  MallocErrorAbort=1 MallocScribble=1`. The Dock's list no longer walks
  (see Everywhere). That removes where most of these landed, not whatever
  spoils the widget: if a run still loses a worker, read its report in
  `~/Library/Logs/DiagnosticReports` before anything else.
- **A script on the card crashed the same way once** (12 October, full
  local run): `test_the_world_flies_the_level_s_craft_and_it_hides_no_more`
  got exit -11 from its own process, which segfaulted constructing a
  `QOpenGLWidget` (`PySide::SignalManager::retrieveMetaObject` under
  `Sbk_QOpenGLWidget_Init`). The script builds and drops four rider panes
  in a row; its class passed three times alone. A fresh process, so not the
  workers' corruption carried over, but the same library.
- **A hosted runner's graphics vary from run to run.** On 8 October one
  drew the rider's world at least sixty times slower than a Mac: four world
  scripts each ran out their three minutes and the run its half hour, where
  the 1.8 and 1.9 runs finished in sixteen minutes. `test_rider_world.py`
  now draws sixteen frames of the world first and skips its scripts on the
  card when a frame takes over `WORLD_FRAME_MOST` (100 ms; about seven
  here). A script that fails there is not a reason to skip.
- **The corkscrew city test failed once** in a full local run on 10
  October (`test_the_towers_stay_upright_whatever_the_road_does`: the turned
  road's frame differed from the level one by more than the city did,
  30,220 against 21,915), and passed alone three times and with its file
  on four workers. If it fails again, look at the two frames it compares
  before touching the threshold.
- **`test_the_towers_stay_upright_whatever_the_road_does` failed once** in
  about a dozen runs of its file here (the towers ahead differed by more
  than 1%); never alone in eight, and in ten more runs of its file the
  towers moved 0.000% each time. Not reproduced.
- **The visualiser window keeps its minimum in step with its controls**
  (`AttachmentViewer._keep_room_for_controls`, on every layout request).
  Taken once, at the first showing, it left out the visualiser's row of
  controls, which arrives with a track: the window could be made narrower
  than that row (by 11 to 15 pixels), and a box layout measures a
  height-for-width item at its own minimum width, so the words under the
  row were measured wider than they were laid out and came out a line
  short. The dark look's slightly wider buttons made it show; a Touch Bar
  test that chose Always dark in Settings and never put the look back made
  later tests in the same worker dark, which is why it came and went.
- **The sorter's scores** (exact category): labelled 94.1%, stress 85.1%,
  meetings 93.3%, acknowledgements 100%, adversarial 97.4%, oblique 100%,
  retired held-out 93.5% - all dev sets now. The held-out set
  (`holdout.json`, 116 messages, written on 11 October 2026, measured at its
  start and once at the end): 84.5% exact, 94.8% job against not, 43 of the
  44 it filed right. That is the number to quote for mail it has not seen,
  with the caveats in `test_the_held_out_set_is_never_used_to_tune` (its
  author knew the last set's weak spots, and a few of its sentences repeat the
  unit tests' wording, so it reads a little high). Its one
  wrong filing is the confident mistake left; do not fix it against this set,
  write the next set first. `./dev tune --corrections` lists real gaps.
- **A label to settle with the owner:** job-board digests ("New today: a role
  in a town", "N new jobs matching your search") are Promotion in
  `labelled.json` and `meetings.json` but Newsletter in the real inbox's study
  labels, and the prompt's definitions read as Newsletter. They stay
  Promotion until the owner says which; `test_sorter_safety` pins
  `labelled.json`.
- **Known gaps in the sorter:** an advert about interviews (a workshop on
  interview skills) can still land in job review at low confidence; a
  forwarded courier notice from a friend's own address waits for review
  instead of being filed.
- **A conversation is read together only after the fact** (the two
  `workers.read_with_*` passes); each message is still classified alone.
- **Corrections only learn folders,** not that a sender is job-related.
- **`workers._grouped()` opens one connection per account and folder.**
- **The verdict cache ignores UIDVALIDITY changes;** `forget_mailbox` is
  unused.
- **No test opens the built `.app`.**
- **Where in the refresh the screen takes its frame is not measured.**
  Nothing here can see the glass (no screen recording), so frames are
  drawn just after each refresh on Apple's account of it: anything handed
  over before the next refresh is shown at it. The numbers in Measuring
  hold if that is so; if the deadline sat a third of the way in, where
  the frames are handed over, about half of them would miss it, so that is
  what to check first if the picture ever stutters on a Mac where the old
  timer did not.
- **The difficulty levels want tuning by play.** They are one table,
  `rider_layout.DIFFICULTY`.
- **The audio allowance is measured on built-in speakers only;** Bluetooth
  reports more, and **Timing…** is the way out.
- **The character classes** (Pointman, Vegas, Pusher, Eraser) need mouse input
  on the grid first.
- **Gatekeeper:** without a paid Developer ID, the first launch needs
  right-click, Open.
- **The Touch Bar was used by hand on a real bar on 9 October**, after
  1.11.0, and works as built. Its drawing is also checked in pixels and its
  room measured in AppKit's own appearance. macOS's customise palette for
  the main window's bar is still untested.
- **Other mailboxes are listed over the inbox's period** and read only:
  there is no paging beyond `workers.LIST_MOST`, no unread count in the
  sidebar, and no filing from them.
- **The rules page's dropdowns could not be made to cut an option short** on
  this Mac, at 760 or 980 wide, in light or dark, at the normal or the
  larger type; every combo now asks room for all of its options
  (`RoomyCombo(every=True)`). If a cut-off is seen again, note the page's
  size and the appearance settings.
