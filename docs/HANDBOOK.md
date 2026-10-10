# Mail Manager handbook

How the app works, how to use it, and how to build it. The
[README](../README.md) is the short version.

## Contents

- [How it works](#how-it-works)
- [Sorting](#sorting)
- [Categories and folders](#categories-and-folders)
- [The window](#the-window)
- [Rules](#rules)
- [Running on its own](#running-on-its-own)
- [Briefing, clearing out and undo](#briefing-clearing-out-and-undo)
- [Attachments and links](#attachments-and-links)
- [Mailboxes](#mailboxes)
- [Updates](#updates)
- [Privacy](#privacy)
- [Developing](#developing)
- [Building and releasing](#building-and-releasing)
- [Troubleshooting](#troubleshooting)
- [Use of AI-assisted tools](#use-of-ai-assisted-tools)

## How it works

1. **Scan.** Connects to each mailbox over IMAP and TLS and fetches the
   messages in the chosen window (past 24 hours, 3 days, 7 days or a custom
   range) with `BODY.PEEK`, so nothing is marked as read. Only the first 64 KB
   of each message is fetched, which keeps the text and skips attachments. A
   message is marked cut short only when its text was: a logo or a PDF after
   the text does not count, so the sorter can still be sure of it.
2. **Read.** HTML is reduced to the text a person would see. Link targets are
   kept.
3. **Sort.** Each message gets a category, a summary, a confidence score and
   the reasoning, from the built-in rule set or from a model.
4. **Review.** Everything lands in a table. Click a row to see the message
   beside the reasoning, and change its folder if you disagree.
5. **Apply.** Ticked messages are copied to their folders, and only once the
   copy is confirmed are the originals flagged `\Deleted` and expunged. Where
   the server supports `UIDPLUS`, only those messages are expunged.

**Stop All** (⌘.) stops everything at once.

## Sorting

| Backend | Key | Cost | Notes |
|---|---|---|---|
| **Built-in rules** | no | free | The default. Runs on your Mac; nothing is sent anywhere. |
| **Ollama** | no | free | A model on your Mac. Slow on older machines. |
| **Gemini** | yes | about $0.02 per 100 messages | |
| **OpenAI-compatible** | usually | about $0.03 per 100 messages | Also LM Studio, OpenRouter, Groq. |
| **Claude** | yes | about $0.15 per 100 messages | Haiku 4.5 unless you pick another. |

Choose in **Settings → Analysis**, or from the ⚙︎ button in the toolbar. Keys
are kept per backend in the Keychain. If a model fails mid-scan, the built-in
rules finish the scan; a rejected key is reported instead.

### The built-in rules

`rules_engine.py` scores 1,097 weighted signals: phrases, senders, links and
the shape of a message. It copes with messy text (mojibake, accents, smart
quotes, zero-width characters, look-alike letters, spaced-out words) and has
phrases in Spanish, French, German and Portuguese for the commonest verdicts.

It also reads the sentence a message is written for (`statements.py`): the
decision ("we will not be moving you forward"), the step asked for ("we ask
that you complete a short questionnaire"), the meeting arranged, the offer
made, the application acknowledged. A grammar covers the ways each is put,
and the context that undoes one: a condition ("if we decide not to move
forward"), a hedge ("you may be invited to"), somebody else's step
("applicants who reach the last round give references"), a quoted reply, or
a feedback survey after a decision. A plain statement is decisive, and the
thanks every rejection opens with is not read as a rival to it. A statement
counts only where something besides the word "application" says hiring, so
a university or a bank receiving an application is not job mail.

Context counts too. Within a conversation, a weak reading takes the
conversation's category, and a calendar invitation from the person you are
already in a hiring process with (a recruiter's "Invitation: Rowan / Sam")
joins that process, though calendars send it as a new message. Both are held
just below the filing threshold, so somebody looks first.

Field overlays add vocabulary for Software, Healthcare, Finance, Academia,
Legal, Sales, Trades, Government, Design and Teaching: 275 extra signals across
the ten fields, chosen under **Model → Local rule set**.

It also knows 24,127 brands with their sectors, 30,231 domains and 4,570
airport codes, bundled with the app. That is how it reads a flight booking or
a receipt that never says what it is. These hints rank; they never decide on
their own, and anything resting mainly on them stays below the filing
threshold.

On 292 messages from a real inbox that it had never seen, labelled by hand
before it ran, it named the exact category for 89% (the version before: 72%)
and filed 137 of the 152 job messages without asking, none of them into the
wrong folder (the version before filed 92). On a hand-written held-out set it
scores 87% exact. Its confidence is capped, and a message that fits two
categories lands in Needs Review.

### Confidence and Needs Review

| | confident | not confident |
| --- | --- | --- |
| **job mail** | its category folder, ticked | `Job Search/Needs Review` |
| **everything else** | filed by topic, if you asked for that | left in your inbox |

The threshold is 0.95 by default. A model's answer is checked before it is
used: an unknown category, a contradiction, or a confidence above 1 sends the
message to Needs Review, and the preview lists what was corrected. A message's
text is passed to a model as data, never as instructions.

## Categories and folders

| Category | What it is |
|---|---|
| **Offer** | An offer, its paperwork, or a negotiation |
| **Interview** | An invitation, a booking link, a confirmed time |
| **Next Steps** | An assessment, a form, references, documents |
| **Received** | An acknowledgement with nothing to do |
| **Networking** | A referral or introduction, with no process yet |
| **Not Interested** | A rejection |
| **Unsolicited** | Recruiter outreach you never asked for |
| **Needs Review** | Job mail the sorter could not place |

Everyday mail is sorted into thirteen topics: Security, Finance, Receipts,
Shipping, Travel, Events, Church, Work, Personal, Social, Newsletters,
Promotions and Spam. The **Sorting** button chooses what happens to it: left
in place, gathered into Needs Review, or filed under `Sorted Mail/`, with as
many or as few of the topics as you want.

Folders are made on the first scan that needs them, under `Job Search/` and
`Sorted Mail/` (both renameable), using the separator your server reports.
Existing folders with the same names are reused.

## The window

The toolbar holds the time window, **Scan & Analyze**, **Apply** and **Stop
All**. Under it: a search box, a category filter, **Show** (everything, job
mail only, ticked only) and the tick shortcuts. The preview pane shows the
message and the analysis; **Links** and **Attachments** open what is in it.

Down the left, the sidebar lists your mailboxes as Mail does: the inbox,
Drafts, Sent, Junk, Trash and Archive, each gathering every account's, then
each account's own folders. Choose one to see its mail: each time you do, it
is looked at again, reading only what is new, and **View → Get New Mail**
(⇧⌘N) looks again at the one on screen. Drag a folder onto another to move
it inside, between two folders to put it beside them, or onto the account's
name for the top level; or right-click it and choose **Move To**. While you
hold it, a bar shows where it will be listed, and the folder it goes into is
outlined. The folder moves on the server with everything in it; the inbox
and the mailboxes the server keeps for itself stay put. The
button at the left of the toolbar, or ⌃⌘S, hides the sidebar and brings it
back. The window
opens on the last month of your inbox, read without marking anything, so it
is never empty. Until a scan reads them those rows say **Not sorted yet**,
and a scan's verdicts then show over them: a message outside the scan's
window stays as it was listed. Mail in any other mailbox can be read and
answered there, but only mail in the inbox is filed.

Right-click the app in the Dock for every window it has open, by title,
the one in front ticked; choose one to bring it forward. **Show Mail
Manager**, **Scan Now**, **Settings** and **New Message** are under them.

Columns can be hidden and resized, and **View → Row height** sets how much of
each summary shows. Dates read as a person would say them, with the exact time
on hover. Settings can be exported to a file and imported on another Mac;
passwords are not in it.

| Key | Action |
|---|---|
| ⌘R | Scan & Analyze |
| ⌘↩ | Apply |
| ⌘. | Stop all |
| ⌘Z | Undo the last filing |
| ⌘F / Esc | Search / clear filters |
| ⌃⌘S | Show or hide the sidebar |
| ⇧⌘N | Get new mail |
| ⌘1 to ⌘4 | Past 24 hours, 3 days, 7 days, custom |
| ⌘A / ⌘⇧A / ⌘D | Tick all / tick confident / clear ticks |
| ⌘B | Briefing |
| ⌘L | Activity log |
| ⌘, | Settings |
| ⌘/ | This list, in the app |

**Settings → Appearance** sets light or dark, contrast (normal, high or
maximum) and spacing, and can tune the layout for reading; turned off, the
layout is as it was. The ? at the top right explains whatever you hover
over.

On a MacBook Pro with a Touch Bar, the bar has **Scan** (**Stop** while it
works), **Apply**, **Undo**, **Show**, the category and the tick shortcuts.
**More** has the visualiser, Briefing, Find, Links and the rest of the
commands; the sliders icon has the time window, the model, hover help and
Settings. Every window has its own; Settings has its pages and their main
choices, and dialogs have their buttons. The visualiser's has play, the
scene (tap it for the list), the strobe, the scene's own button (the game,
the scope's beam, the meters' colours), **Picture**, **Seek** and full
screen; in Music rider the game opens onto every game and every level, and
the speaker beside it onto the game's sounds and their volume. Where there
is room, the shape and what the strobe listens to open onto lists too. The
music's volume is the Control Strip's own. Each does one thing when tapped;
a button that opens onto more opens it, nothing inside opens anything
further, and nothing is held or dragged. Beside the Control Strip there is
room for **Seek** or for a scene's own button, and Seek gives way; with the
Control Strip hidden, everything fits. A slider's ends say what they mean
(quiet and loud, less and more, start and end), and its knob is rounded,
like the system's.

## Rules

**Settings → Rules.** A rule is a list of conditions and a list of actions.
Five ship, switched off, as examples.

Conditions test the job category, everyday topic, sender, domain, subject,
body, confidence, mailbox, age, recipients and attachment names, and whether a
message is bulk, has an attachment, is a reply or came from a machine. A rule
matches on all of its conditions or any of them.

| Action | Effect |
|---|---|
| Draft a reply from a template | Into Drafts, threaded |
| Draft a reply with the model | Into Drafts, from your guidance |
| File it into a folder | Sets the row's folder; nothing moves until Apply |
| Tick it, or leave it unticked | Sets the row's tick |
| Put it in To Delete | Files it there and ticks it |
| Mark as read, or flag it | On the server |
| Leave it where it is | Cancels an earlier rule's filing |
| Stop | Skips later rules for that message |

Rules run top to bottom, so an exception goes at the top with **Stop**.
**Try it on the last scan** shows what would happen without touching anything.

A rule that drafts has limits: at most once in N days per person, only within
chosen hours and days, and never to a no-reply address or an automatic
sender. Patterns that could hang the app, such as `(a+)+`, are refused.
**A rule never sends anything.** Only you do, from a message window.

## Reading and replying

Double-click a message, or press Return on it, and it opens in a window of
its own with the whole message as sent, on its own light page whatever the
app's look. Messages are drawn by WebKit, as in Mail, so a newsletter looks
as its sender designed it; nothing in a message runs, and nothing loads but
its pictures, when they are wanted. With pictures off, a message with no
dark design of its own has any pale text darkened so it can be read. A scan
reads the start of each message; one longer than that is read whole when
it is shown, in the preview or its window, and until it arrives the
message says it is only the start. Along the top: Reply (⌘R), Reply All
(⌘⇧R), Forward (⌘⇧F), Mark as Read or Unread, Flag, Move to, Archive (⌘E),
Junk and Delete (⌘⌫). ⌘↑ and ⌘↓ walk the table in the order it is shown.
Opening a message marks it read, as it would anywhere else. The same actions
are in the **Message** menu and on a right-click in the table, where
**Archive now** and **Delete now** act at once; **File in…** still waits for
Apply. A quick move is undoable like any filing. Rest the pointer on any
control for five seconds and it explains itself; the ? at the end of the
bar explains whatever you point at straight away, as it does in the main
window and the viewer.

A reply opens addressed, with the message quoted under your sign-off, and
goes out through the mailbox it arrived in - any of yours, from the From
list. The sign-off is written on **Settings → Signature**: as many lines as
you like, with formatting, links and a picture, and a switch each for new
messages and for replies; with nothing written there, the name alone is
signed. The bar above the message is laid out as a word processor's: a size
in points (pick one or type one), bigger and smaller, bold, italic,
underline, strikethrough and colour; bullets and numbering (press for the
usual, open for the kind), indents and alignment; a link, a picture, and an
eraser that takes the formatting off. Every button names itself and its key
when rested on, as the message window's do. Attach files with ⌘⇧A or by
dropping them. ⌘↩ sends, ⌘S saves to Drafts, and closing a half-written
message asks. A copy of what you sent lands in Sent, except on Gmail, which
files its own. Forwarding carries the attachments along, up to 25 MB. A
picture in a message travels as a part of its own, which every mail reader
shows.

Mail goes out with the same app password as reading it, to the provider's
own server. A server of your own goes under **Settings → Mailboxes → Server**.

## Running on its own

**Schedule** scans on a timer. *Keep scanning when the app is closed* installs
a launchd agent, so scans continue after you quit. A background scan files
only what would have been ticked. The menu bar icon offers a quick scan and
shows the last result.

**Auto scan**, under **Settings → Mailboxes**, scans as soon as the app
opens, once the inbox is listed. It never stops to ask: if a password or a
key is missing, the log says which and nothing runs.

## Briefing, clearing out and undo

**File → Briefing** reads the last scan back: what needs you first, ranked by
what it costs to miss, then what arrived and where it is going. It opens no
mailbox and calls no model.

**File → Clear out mail** deletes in bulk by sender, subject, age, read state
or list mail. It asks the server for a count first, and Delete is only enabled
for that exact count. Job mail, receipts, bank mail and security notices are
never suggested. **File → Empty a folder** empties one, `To Delete` by
default. Deletion on IMAP is permanent.

**⌘Z** moves the last ten filings back.

## Attachments and links

**Message** shows a message as it was sent, pictures included: they are
fetched from wherever the sender keeps them, which tells the sender it was
opened, so **Settings** can turn them off, and each is then shown as [image].
**Plain text** is the text alone.

**Attachments** opens a viewer for images, audio, PDF and text. Only the part
you open is downloaded, and nothing is ever run:

- A file's bytes decide what it is, not its name.
- Filenames are cleaned, so `../` and right-to-left tricks do nothing.
- SVG and HTML are shown as text, and archives are never expanded.
- Saved files carry the quarantine flag, like a download.

Audio files can be played with a music visualiser, including a game. Its
strobe is off by default; at its fastest it can trigger photosensitive
epilepsy. The strobe set to Bass follows the drums' own beat once the
track has been read, and the game's road is a curve that winds more in the
quiet parts; kicks are told from bass notes by how they arrive. The rave's
lasers come in only for a drop, read from the drums once the track has been
read and placed where the loudness really rises, and sweep a beat at a time
until it ends; its rings cross the room in two beats. The hand strobe has
two keys: hold G and the light stays on until you let go; hold H and it
flashes as fast as it can. A held light also streams rings out of the Neon
tunnel and the rave and lights the city's windows. In full screen, [ and ]
set how long one scene takes to give way to the next. **Visualize an Audio
File** turns the picture on with the first track.

In the game's Puzzle, colours you collect drop into a grid three wide, and
three or more of a colour touching clear and score. A grey you hit drops in
as clutter, broken by a clear beside it; a column that overflows bursts and
costs points. A coloured block you let pass dissolves before it reaches you.
The game you chose stays chosen from one track to the next.

**Links** lists every link in a message by the site it really goes to, to open
or copy. Opening one, from there or from the analysis, first shows the site and
the full address, with names in other alphabets spelled out. Only web and mail
addresses open. "Don't show this again" turns the warning off, and **Settings →
Appearance** turns it back on.

## Mailboxes

**Settings → Mailboxes** holds any number of IMAP accounts. Typing the address
usually picks the server.

| Provider | What to enter |
| --- | --- |
| iCloud | an app-specific password, from account.apple.com |
| Gmail | an app password (needs 2-Step Verification and IMAP on) |
| Outlook, Microsoft 365 | an app password |
| Yahoo, AOL, Fastmail, Zoho, GMX | an app password |
| Proton Mail | through the Proton Bridge app |
| Anything else | its server, port and password |

Each password is kept in the Keychain. With more than one mailbox, the toolbar
chooses which to scan and the table gains a Mailbox column. A work Microsoft
account may have password sign-in turned off by its administrator.

**When Mail Manager opens**, at the foot of the page, sets how much of the
inbox the window lists (the last week, two weeks, month, three months, six
months or year; a month to begin with, and at most the newest thousand
messages), or turns the listing off, and turns Auto scan on.

## Updates

Once a day, while **Settings → Appearance → Look for new versions** is on, the
app asks GitHub for its latest release. Nothing else is sent. **Help → Check
for Updates** asks at any time.

**Update** downloads the disk image and checks it against the size and SHA-256
GitHub gives, then checks the app inside: same bundle ID, the version it should
be, and signed with the same certificate as the copy that is running. Then the
app quits, the new one is put in its place, and it opens. If the app cannot
replace itself (run from the disk image, or a folder it cannot write to), the
button opens the release page instead.

## Privacy

- With the built-in rules, no message text leaves your Mac. With a model,
  message text goes to that service.
- Passwords and keys live in the Keychain. The settings file holds no secrets
  and is `0600`.
- The summaries kept between scans and what the app learns from corrections
  are encrypted on disk with AES-GCM, under a key in the Keychain.
- Logs never contain message bodies or credentials, and are owner-only.
- Attachments are never uploaded.

See [SECURITY.md](../SECURITY.md) for reporting a problem.

## Developing

```bash
./dev demo     # the app with a sample inbox, no setup
./dev run      # against your own mail
./dev dry      # scan for real, with moves disabled
./dev fake     # the pipeline in the terminal, offline
./dev scan     # the pipeline on real mail, read-only
./dev test     # 5,329 tests (with the evaluation sets present)
./dev eval     # sorter accuracy, on a labelled set of your own
```

`./dev` lists everything. No test touches the network or the Keychain:
`tests/conftest.py` has a fake IMAP server and a fake model client.
`./dev scan --uid N --prompt` shows exactly what a model was sent for one
message.

| Module | What it does |
|---|---|
| `main.py` | Entry point, logging, `--self-test` |
| `gui.py` | The main window |
| `triage_table.py` | The table, its filters and the preview pane |
| `webview_mac.py` | Messages drawn by WebKit, on a Mac |
| `sidebar.py` | The mailboxes down the left, and moving folders |
| `settings_dialog.py` | Settings |
| `imap_engine.py` | IMAP: fetching, folders, moves |
| `models.py` | Categories, validation and routing (no Qt, no network) |
| `rules_engine.py`, `rulesets.py` | The built-in sorter and its overlays |
| `llm_engine.py`, `providers.py` | The prompt, the schema and the model backends |
| `html_utils.py` | HTML to text, and link recovery |
| `workers.py`, `pipeline.py` | Background work and cancellation |
| `config.py`, `vault.py` | Settings, the Keychain, encryption at rest |
| `link_open.py` | Links: listing them, and the warning |
| `updates.py`, `update_dialog.py` | Finding and installing new versions |
| `attachment_*.py`, `visualizers.py`, `rider_*.py` | The attachment viewer and the visualiser |

## Building and releasing

```bash
./dev build        # tests, then dist/Mail Manager.app
./build_dmg.sh     # dist/Mail Manager.dmg
```

The app is universal2. `./tools/fetch_universal_python.sh` provides an
interpreter with both architectures, and `build_app.sh` joins the two halves of
any dependency shipped per architecture.

Sign with a stable identity: `./tools/make_signing_identity.sh` makes a
self-signed certificate once, and `build_app.sh` uses it from then on. Without
it every build is a new app to the Keychain, which then asks for permission
again. It does not satisfy Gatekeeper; that needs a paid Developer ID.

The build runs the app's `--self-test` on the finished bundle and will not
ship one that fails.

Finder lays out the disk image's window, and records the background picture
with the path of the image it laid out, on this Mac. `build_dmg.sh` takes that
out of the layout (`tools/dmg_layout.py clean`), then mounts the finished image
and removes it if anything outside the app still names this Mac: the home
folder, the user name, the start-up disk or a disk's UUID.

To release: set `APP_VERSION` in `models.py`, build and sign with the same
certificate as the last release (the updater refuses anything else), tag
`vX.Y.Z`, and attach the `.dmg` to a GitHub release.

## Troubleshooting

- **"iCloud rejected those credentials":** use an app-specific password, not
  your Apple ID password.
- **"The developer cannot be verified":** right-click the app and choose Open,
  once.
- **Fewer messages than expected:** check the per-scan limit in Settings; the
  app says when it was reached.
- **Everything is in Needs Review:** look at a row's preview. An error there
  means the model call failed; low confidence means it was unsure.
- **`BAD [b'unmatch quote']` when signing in:** the password was pasted with a
  line break. Paste it again.
- **The app will not start:** run
  `"/Applications/Mail Manager.app/Contents/MacOS/Mail Manager" --self-test`.

## Use of AI-assisted tools

During development and campaign preparation, the Mail Manager team used
AI-assisted tools in a limited supporting role, including coding assistance,
copy editing, and the preparation of some sample display content.
