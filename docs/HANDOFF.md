# Handoff

Where this round of work got to, what was measured, and what is worth doing
next. The list at the bottom is meant to be ticked off — everything already
ticked was done in this round, and the untinted boxes below it are what a
future session should pick up.

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
it — anything the fetch does not stream is swept up and classified anyway, and
there is a test for exactly that case.

**The sorter reads the From line.** A careers mailbox is not a person writing
to you however warmly it is written, which stopped eight adversarial rejections
reading as personal notes. A second table of otherwise-too-generic phrases
counts only once a hiring mailbox or a named process has licensed it — the
thing a language model does that a phrase table does not.

**The sorter is four times faster.** Before running a signal's regex, check
that the longest word of its phrase is in the text at all. 43.5 ms per message
to 10.1 ms, with every eval fixture scoring exactly what it scored before.

**Undo works, and works more than once.** An IMAP `COPY` gives the message a
new UID and the old undo used the old one — so it either did nothing or moved a
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
| labelled (102) | 98.0% | 87.3% | 58/59 — 98.3% |
| adversarial (39) | 87.2% | 59.0% | nothing filed; all held for review |
| held out (24) | 70.8% | 37.5% | nothing filed |
| meetings (15) | 100% | 80.0% | 2/2 |
| acknowledgements (11) | 100% | 100% | 7/7 |

**SpamAssassin corpus**, 6,046 real messages: 0 unreadable, 145 messages a
second, **0 of 4,150 ham messages filed as job mail**, 4 sent to Junk (0.10%).
That last row is the one that matters and it must stay at zero.

**Speed.** Rules engine 10.1 ms per message. Lexicon opens in 38 ms using
1.95 MB, down from 70 ms and 10.5 MB, and neither number now grows with the
table. Test suite 2,212 tests in about three minutes on four workers.

---

## Things a future session should know

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

---

## The list

### Done in this round

- [x] **1.** `Ctrl+R` was bound twice, so Qt fired neither. Every shortcut is now
  unique, and there is a check before adding one.
- [x] **2.** The Manage Models button appeared only for some backends.
- [x] **3.** The hotel and restaurant sectors were pruned from the lexicon; they
  cost more than they earned.
- [x] **4.** Learning from corrections, per address and per domain, with a way
  to see and forget it.
- [x] **5.** User-defined sorting rules — a rule that only files and ticks now
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
- [x] **18.** A literal prefilter before every regex — 4.3× on the rules engine.
- [x] **19.** The lexicon as a memory-mapped blob, with the JSON as fallback.
- [x] **20.** The log view is capped at 2,000 blocks.
- [x] **21.** `./dev tune` for training against a real inbox, plus the privacy
  test that stops anything from it reaching the repository.
- [x] **22.** Topic ties are broken by score, then strongest phrase, then hard
  evidence, then a written precedence — not by dict insertion order.
- [x] **23.** `personal_register` no longer fires on mail from a careers
  mailbox.

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
  sender is job-related at all, which is the more valuable half — a recruiter
  writing from a personal Gmail is the case the rules engine will never get.
- [ ] **`_grouped()` in `workers.py` opens one connection per (account, folder)
  pair.** Undoing a batch that was filed into eight folders is eight logins.
- [ ] **The verdict cache never shrinks below `MAX_AGE_DAYS`.** A UIDVALIDITY
  change silently invalidates a whole mailbox's worth and nothing notices;
  `forget_mailbox` exists and nothing calls it.
- [ ] **No test opens the built `.app`.** Every failure mode of PyInstaller
  hidden imports is invisible until somebody runs the bundle by hand.
- [ ] **`rules_engine.py` is 2,700 lines** and the signal tables are most of it.
  They would read better as data than as literals — but only if something needs
  to edit them at run time, which nothing does yet.
