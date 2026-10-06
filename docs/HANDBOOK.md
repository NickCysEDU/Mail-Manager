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
   of each message is fetched, which keeps the text and skips attachments.
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

`rules_engine.py` scores 1,071 weighted signals: phrases, senders, links and
the shape of a message. It copes with messy text (mojibake, accents, smart
quotes, zero-width characters, look-alike letters, spaced-out words) and has
phrases in Spanish, French, German and Portuguese for the commonest verdicts.

Field overlays add vocabulary for Software, Healthcare, Finance, Academia,
Legal, Sales, Trades, Government, Design and Teaching: 275 extra signals across
the ten fields, chosen under **Model → Local rule set**.

It also knows 24,127 brands with their sectors, 30,231 domains and 4,570
airport codes, bundled with the app. That is how it reads a flight booking or
a receipt that never says what it is. These hints rank; they never decide on
their own, and anything resting mainly on them stays below the filing
threshold.

On 102 real messages it agreed with a language model on job versus not job
99% of the time and on the exact category 87%. Its confidence is capped, and a
message that fits two categories lands in Needs Review.

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
| ⌘1 to ⌘4 | Past 24 hours, 3 days, 7 days, custom |
| ⌘A / ⌘⇧A / ⌘D | Tick all / tick confident / clear ticks |
| ⌘B | Briefing |
| ⌘L | Activity log |
| ⌘, | Settings |
| ⌘/ | This list, in the app |

**Settings → Appearance** sets light or dark, contrast (normal, high or
maximum) and spacing, and can tune the layout for reading. The ? at the top
right explains whatever you hover over.

On a MacBook Pro with a Touch Bar, the bar has **Scan** (**Stop** while it
works), **Apply**, **Undo**, **Show**, the category and the tick shortcuts.
**More** has the visualiser, Briefing, Find, Links and the rest of the
commands; the sliders icon has the time window, the model, hover help and
Settings. Every window has its own: the visualiser's has play, the scenes,
the strobe, the game's and the scope's settings and the picture's; Settings
has its pages and their main choices; dialogs have their buttons.

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
**Nothing is ever sent.**

## Running on its own

**Schedule** scans on a timer. *Keep scanning when the app is closed* installs
a launchd agent, so scans continue after you quit. A background scan files
only what would have been ticked. The menu bar icon offers a quick scan and
shows the last result.

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

**Attachments** opens a viewer for images, audio, PDF and text. Only the part
you open is downloaded, and nothing is ever run:

- A file's bytes decide what it is, not its name.
- Filenames are cleaned, so `../` and right-to-left tricks do nothing.
- SVG and HTML are shown as text, and archives are never expanded.
- Saved files carry the quarantine flag, like a download.

Audio files can be played with a music visualiser, including a game. Its
strobe is off by default; at its fastest it can trigger photosensitive
epilepsy.

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
./dev test     # 4,706 tests (with the evaluation sets present)
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

It is also shown in the app, under **Help → About Mail Manager**.
