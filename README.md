<div align="center">

<img src="docs/assets/icon-128.png" width="112" height="112" alt="Mail Manager">

# Mail Manager

**Sort your mail on your Mac, in seconds, without an API key.**

[Download](https://github.com/NickCysEDU/Mail-Manager/releases/latest) ·
[Handbook](docs/HANDBOOK.md) ·
[Security](SECURITY.md) ·
[Contributing](CONTRIBUTING.md)

![macOS 13+](https://img.shields.io/badge/macOS-13%2B-black)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)
![Tests](https://img.shields.io/badge/tests-2%2C485%20passing-brightgreen)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

<img src="docs/screenshot.png" width="900" alt="The approval table, with a summary, category, destination folder and confidence score for every message">

</div>

---

## What it does

You applied to forty places. Your inbox is now rejections, assessment links,
"we received your application", recruiter spam, and one interview invitation you
have not spotted yet.

Mail Manager reads the window you choose, works out what each message is, and
shows you a list. You tick the ones you want filed. It files them.

It starts on the job search, because that is the inbox that gets worst fastest.
It will sort the rest of your mail too, into as many or as few topics as you
want, and it does the whole job on your Mac unless you tell it otherwise.

- **Nothing moves without you.** Every message gets a tick box. Only ticked ones
  are filed.
- **Uncertainty goes to a folder, not a guess.** Anything below the confidence
  bar lands in `Needs Review` instead of a category folder.
- **It creates its own folders.** You do not touch Mail.
- **Read-only until you say otherwise.** Messages are fetched with `BODY.PEEK`,
  so nothing is marked as read.
- **And it will clear the rest out.** Deleting thousands of messages by sender,
  subject or age takes a handful of commands rather than a round trip each.

## Install

The download is a **universal** app: one file that runs natively on Apple
silicon and on Intel, with no Rosetta and no choosing between two downloads.

**[Download the latest `.dmg`](https://github.com/NickCysEDU/Mail-Manager/releases/latest)**,
open it, and drag Mail Manager onto Applications.

On first launch macOS will say the developer cannot be verified, because this
build is not signed with a paid Apple certificate. Right-click the app in
Applications and choose **Open**. That is needed once.

<details>
<summary>Or build it yourself</summary>

```bash
git clone https://github.com/NickCysEDU/Mail-Manager.git
cd Mail-Manager
./dev build          # virtualenv, dependencies, tests, then the .app
./dev install        # copies it to /Applications
./build_dmg.sh       # or make the disk image
```

Needs macOS 13+ and Python 3.11+ (`brew install python@3.12`).
</details>

## First run

A four-page setup asks for two things:

1. **Your iCloud address and an app-specific password.** iCloud rejects your
   normal Apple ID password over IMAP. Make one at
   [account.apple.com](https://account.apple.com), under Sign-In and Security.
2. **Which sorter to use.** The default runs on your Mac and needs nothing else.

Then press **Scan & Analyze**.

## Sorting without an API key

The built-in sorter is a rule set, not a model: 1,071 weighted signals
covering the language hiring mail actually uses, plus structural features like
subject shape, sender domain, and whether an instruction is a real request or a
hypothetical one.

Measured against a language model's verdicts on 102 real messages:

| | |
|---|---|
| Job-related or not | **98%** agreement |
| Exact category | **83%** agreement |
| Of the messages it was confident enough to file | **98% correct** |

It is instant, free, private, and deterministic. When it is unsure it says so,
which is the behaviour that matters: the messages it files are almost always
right, and the rest wait for you.

Eleven field-specific overlays add vocabulary for **Software, Healthcare,
Finance, Academia, Legal, Sales, Trades, Government, Design and Teaching**.

<details>
<summary>Or use a language model instead</summary>

| Backend | Key | Cost per ~100 emails |
|---|---|---|
| **Built-in rules** | none | free, nothing leaves your Mac |
| **Ollama** (local model) | none | free, nothing leaves your Mac |
| **Gemini** | yes | about $0.02 |
| **OpenAI-compatible** | usually | about $0.03 |
| **Claude** | yes | about $0.15 |

Switch from the gear button in the toolbar. Keys are stored per backend in the
macOS Keychain. If a backend is unreachable mid-scan, the built-in sorter takes
over so the scan still finishes.
</details>

## Categories

| | Category | |
|---|---|---|
| 🟣 | **Offer** | An offer, its paperwork, or a negotiation |
| 🟢 | **Interview** | An invitation, a booking link, a scheduled time |
| 🔵 | **Next Steps** | An assessment, a form, references, documents |
| 🟦 | **Received** | An acknowledgement with nothing to do |
| 🟪 | **Networking** | A referral or an introduction, with no formal process |
| 🔴 | **Not Interested** | A rejection |
| ⚪ | **Unsolicited** | Cold recruiter outreach you never asked for |
| 🟠 | **Needs Review** | Ambiguous, or below the confidence bar |

Mail that is not part of your job search is described (Finance, Newsletters,
Security, Personal, and so on) and **left in your inbox**.

## Folders

Created for you, in your iCloud account, on the first scan:

```
Job Search/
├── Interview
├── Next Steps
├── Offers
├── Received
├── Networking
├── Not Interested
├── Unsolicited
└── Needs Review
```

Existing folders with those names are reused, not duplicated, and the check is
case-insensitive. You can rename the parent folder in Settings.

## Sorting more than a job search

The sorter was always scoring two things: which job-search category a message
belongs to, and which of thirteen everyday topics it is. Only the first got
folders. The **Sorting** button in the toolbar is where you choose which
distinctions are worth a folder of their own:

| Profile | Job folders | Everything else |
| --- | --- | --- |
| **Job search** *(default)* | seven | left in your inbox |
| **Job search and everyday mail** | seven | filed under `Sorted Mail/` |
| **Everyday mail** | one | filed under `Sorted Mail/` |
| **Essentials only** | one | six folders: security, finance, receipts, travel, newsletters, promotions |

The everyday topics are Security, Finance, Receipts, Shipping, Travel, Events,
Church, Work, Personal, Social, Newsletters, Promotions and Junk. All of it
runs on the same offline rules, so none of it needs an API key.

The same menu decides what happens to everything that is not job mail - left
where it is, gathered into `Needs Review`, or filed by topic - and which of
the thirteen topics earn a folder. A row that cannot be ticked says which of
those choices is the reason, and offers to change it.

Changing any of this refiles the rows already on screen, immediately: where a
message goes is a decision about a verdict rather than a new verdict, so there
is no mailbox to reopen and no model to ask. Nothing moves until you press
Apply, as ever.

## More than one mailbox

Mail Manager talks plain IMAP, so it is not limited to iCloud. Add mailboxes in
**Settings → Mailboxes**; typing the address is usually enough, since the domain
picks the server for you:

| | Server | What to paste |
| --- | --- | --- |
| iCloud | `imap.mail.me.com` | app-specific password |
| Gmail | `imap.gmail.com` | app password (needs 2-Step Verification, and IMAP enabled in Gmail) |
| Outlook / Microsoft 365 | `outlook.office365.com` | app password |
| Yahoo, AOL | | app password |
| Fastmail, Zoho, GMX | | app password |
| Proton Mail | `127.0.0.1:1143` | via the Proton Bridge app |
| Anything else | you type it | whatever your provider uses |

Every one of these still accepts password authentication over IMAP, so none of
it needs OAuth. Work and school Microsoft accounts are the exception worth
knowing about: an administrator can switch password sign-in off for the whole
tenant, and no app password will help if they have.

With more than one mailbox set up, a **✉︎ picker** appears in the toolbar for
scanning one of them or all of them at once, and the table grows a Mailbox
column. Each mailbox keeps its own password in the Keychain and gets its own
folders on its own server.

## Running on its own

Set an interval in the **Schedule** menu and Mail Manager scans on a timer. Turn
on *Keep scanning when the app is closed* and it registers a `launchd` agent so
scans continue after you quit and survive a reboot.

Background scans file only what the app would have pre-ticked. Everything else
waits for you.

A menu bar icon gives you a quick scan over the last 24 hours, 3 days or 7 days,
the sorter it will use, the schedule, and the result of the last background run,
without opening the window. The **Model** entry is named after whatever is
currently selected, and switching there is the same as switching in the window.

## What counts as job mail

Whether a message is part of your job search is asked before which part. Two
things it now recognises that it used to score at zero:

- **A call being proposed, with no role named.** "Please use this link to
  schedule a 20-minute Google Meet call". The fixed phrase "schedule a call"
  breaks the moment somebody says how long it will take.
- **A job description you saved or mailed to yourself.** It contains not one
  word a hiring process uses. It is all headings.

Both are checked against what the meeting is *for*. A booking link proves a
meeting and never that it is about a job, so a dentist's reminder, a
parent-teacher conference and a sales demo stay where they are.

## Reading shape, not just words

*"Seat 14C - FR7712 STN to DUB, Tuesday. Bags close 40 minutes before."* is a
flight, and contains no word that says so. So alongside the phrase tables the
sorter reads the shape of a message: which mailbox it came from (`offers@`,
`billing@`, `bookings@`), the structured things in it (a flight number beside
an airport pair, a tracking number, a direct debit, a table for four), and
whether it reads like two people talking rather than a company writing to a
customer.

It also carries what it knows about the world: 24,127 company brand names with
the sector each belongs to, 30,231 domains, and 4,570 airport codes. That is
350 KB of public data, bundled, with no network call. It is the difference between
`STN to DUB` and
`PDF to DOC`, and it is how the sorter knows ryanair.com is an airline and
argos.co.uk is a shop.

These rank; they never decide. Words always come first, so a security code
from a bank is a security notice rather than a bank statement. Anything
resting mainly on shape is capped below the filing threshold: good enough to
sort by, not good enough to move your mail unasked.

## Acknowledgements

"We got your application, we'll be in touch" is the commonest mail in a job
search and the one you least need to read. Every tracking system words it
differently, so instead of matching phrases the sorter counts what the message
*does*: says it arrived, promises to read it, promises to be in touch if it
fits. Two of those is enough.

A rejection does the same three things and then delivers a decision, so a
decision always wins. And "if your experience aligns, we will reach out to
discuss next steps" is a promise, not a task: it ends nearly every
acknowledgement, and reading it as an action item is how the dullest mail in
the inbox became a to-do list.

## Rules

**Settings → Auto Reply.** A rule is conditions plus actions, so it is whatever
you need rather than one of a fixed set.

Five ship, all switched off: acknowledging an interview invitation, answering a
request for documents or times, declining a recruiter, filing security notices,
and leaving colleagues alone. They are worked examples - open one and change it.

**Conditions** test job category, everyday topic, sender, domain, subject,
body, confidence, mailbox, age, who it was addressed to, the names of anything
attached, and whether it is bulk, has an attachment, is a reply, was sent to
you directly rather than to a list, or came from a machine. Operators are the
obvious ones (is, contains, starts with, matches a pattern, is at least...).
A rule matches on **all** conditions or **any**.

**Actions:**

| Action | What it does |
| --- | --- |
| Draft a reply from a template | Fills `{first_name}`, `{sender}`, `{subject}`, `{me}` |
| Draft a reply with the model | Hands the message to the model with your guidance |
| File it into a folder | Points the row at a folder; nothing moves until Apply |
| Tick it / Leave it unticked | Sets the checkbox, so Apply picks it up or skips it |
| Put it in the To Delete folder | Points the row at `To Delete` and ticks it. Nothing is deleted until you empty that folder |
| Mark it as read | `\Seen` on the server |
| Flag it | `\Flagged` on the server |
| Leave it where it is | Cancels any filing an earlier rule asked for |
| Stop | Skips every later rule for that message |

Rules run top to bottom and **↑ ↓** reorder them. A later rule adds to what an
earlier one decided until one says stop - which is how an exception at the top
works: *leave colleagues alone, and stop*, above a rule that files the rest.

**Try it on the last scan** runs your rules over the messages already on screen
and shows what would happen, touching neither the mailbox nor the model.

A half-written rule never runs; the editor lists what is missing rather than
refusing to save. The same check refuses a pattern that would hang the app  - 
`(a+)+` and `.*.*x` - because a regular expression cannot be interrupted once
started.

**A rule that writes has three limits of its own**, in a box that only
appears for rules that draft:

- **At most once in N days per person.** Without it, somebody who writes four
  times in a morning gets four identical drafts and a list you are on gets one
  per post. This is the setting that makes auto-reply usable.
- **Only between these hours, on these days.** A reply composed at three in
  the morning reads as three in the morning, even though you send it later.
  Filing is never held back this way - moving a message has not spoken to
  anybody.
- **Never to a machine, and this one cannot be switched off.** A no-reply
  address, a bounce, a mailer daemon, or anything carrying the headers an
  automatic reply sets (`Auto-Submitted`, `Precedence: bulk`,
  `X-Auto-Response-Suppress`). Two responders answering each other is how this
  feature goes wrong, and RFC 3834 exists so that one can recognise the other.

When a rule matches and then says nothing, the run says which of these held it
back, because "it matched and kept quiet" looks identical to "it did not
match" and they mean opposite things.

**Nothing is ever sent.** Drafts go to your Drafts mailbox, threaded with
`In-Reply-To` and `References`, and you press send yourself. The model is told
not to invent facts or commit you to anything; where it lacks something it
leaves `[a note in brackets]` and the draft lists those at the bottom. Bulk
mail is skipped by default, per rule.

## Running a model on this Mac

**Settings → Analysis.** If Ollama is missing and Homebrew is there, the panel
installs it. **Manage models…** lists what you have, with the size, the
parameter count and whether it is loaded, and removes what you do not want.

Be realistic about speed. Measured on an Intel Mac with a 3B model: twelve
seconds to load the weights and about a minute for one message. The Test
button says what it measured and what that means for a full inbox. The
built-in rule set is instant and needs nothing installed.

## Setting up

The first run opens a wizard. Link as many mailboxes as you like (iCloud,
Gmail, Outlook and seven others) and choose which folders to create:
job-search folders, everyday folders, or both. Each option says what it will
build before you pick it, and nothing is created until your first scan.

**Help → Add or Link Mailboxes…** reopens it, which is how you link another
account later.

## Which build is this

Bottom right: `1.1.0 · a1b2c3d`. A version number alone does not identify a
build between releases; the commit does. Hover for the full line, including
the Python version and whether it is running the Intel or the Apple silicon
slice, and click to copy it into a bug report.

## Attachments

The preview lists what is attached - name and kind - before you open
anything, because the server describes a message's parts without sending
them. **Attachments** opens them.

| | |
|---|---|
| **Images** | Any format Qt decodes. Fit, Fill, 100%, 200%, 400%, and Copy image |
| **Audio** | Play, seek, volume, cover art, tags |
| **PDF** | Fit width, whole page or actual size |
| **Text** | Wrap on or off, five font sizes |
| **Anything else** | Described, and saved if you want it |

Audio covers MP3, M4A/AAC, WAV, AIFF, FLAC, Ogg and Opus. The format is
decided by the file's own bytes, never its name.

**Info** shows what the file says about itself: type, size, SHA-256, image
dimensions, audio tags, PDF producer - and whether a photograph carries GPS
coordinates, which is worth knowing before you forward it.

Space plays and pauses, the arrow keys move between attachments and scrub
five seconds, Cmd-S saves, Cmd-I is Info. The window sits beside the main one
rather than on top of it, so the app can still be quit while it is open.

Sound files have something else in them. Press play and see.

**Only the part you open is downloaded.** Listing costs about a fifth of a
second; a six megabyte message used to cost six megabytes to see the first
thing in it.

**Nothing is ever run.** There is no "open with", and some refusals are
deliberate:

- A file's **bytes decide what it is**, not its name or declared type. A part
  claiming `image/png` that begins `MZ` is a Windows program and never
  reaches the image decoder.
- **Filenames are treated as hostile.** `../../.ssh/authorized_keys` saves as
  `authorized_keys`, and a right-to-left override - the trick that makes
  `photo<RLO>gnp.exe` read as `photo exe.png` - is stripped.
- **SVG and HTML are shown as text**, because rendering either runs a parser
  that can fetch remote content.
- **Archives are never expanded**, and a decompression bomb is refused before
  any pixels are allocated.
- **Cover art is an image from inside another file**, so it is sniffed and
  size-capped like any other attachment before it is decoded.

Saved files get the quarantine flag a download gets, and a name that exists
becomes `report (2).pdf` rather than overwriting anything.

## The briefing

**File → Briefing** (Ctrl-B). Forty rows across four mailboxes is four hundred
glances, and the three that matter are somewhere in the middle. The scan has
already read every message and decided what each one is; the briefing is that,
read back in the order somebody would want to be told it.

**Needs you** first: offers, interviews to arrange, next steps, security
notices, and anything the sorter could not place - ranked by what it costs to
miss rather than by when it arrived, so an interview invitation from Tuesday
outranks a rejection from an hour ago. Click one and it is selected in the
table, with any filter that was hiding it cleared.

Then the counts: what came in, by kind, each naming the folder it is bound for;
where it is all going, which is the question "what would Apply actually do";
who wrote more than once; and what is still waiting - unticked rows, anything
in Needs Review, and any mailbox that produced nothing at all.

**Copy** puts the whole thing on the clipboard as text. Nothing in it opens a
mailbox or calls a model: it is the rows already on screen, counted, so it
cannot disagree with the table and costs nothing to ask for.

## Clearing out a full mailbox

**File → Clear out mail…** Deleting four thousand messages a page at a time is
slow because each one is a round trip to the server. This asks the server for
the ones that match, then flags a hundred at a time and expunges. A mailbox of
five thousand is about a hundred commands rather than five thousand.

Pick a folder, then narrow it: senders (an address, or a domain for everyone at
it), words in the subject, older than so many days, only mail you have already
read, only mail sent to a list. Down the left are piles the last scan noticed -
the sender you have four hundred newsletters from - and ticking one fills the
filters in. They are suggestions and nothing more; job mail is never among
them, nor is anything from a sender you have corrected the app about, nor
receipts, bank mail or security notices.

Then **Count**. The number comes back from the server, against exactly the
filters the delete will use, and the Delete button is dead until it does -
and dead again the moment you change anything, because a count that was true
about a different question is worse than no count. The confirmation names the
folder, the filters and the number, and so does the button in it.

**File → Empty a folder…** does the whole folder in one go, and offers the
`To Delete` folder by default, because that is the folder that exists to be
emptied: a rule that bins something puts it there rather than deleting it, so a
mistyped rule costs you a folder to look through rather than your mail.

This talks to the server, and deletion on IMAP is permanent. There is no undo.

## Undo

⌘Z after an Apply moves everything back where it came from. The app moves real
mail, and knowing you can put it back is the difference between trying it and
being careful with it.

## Getting around

- **Scan:** which mailboxes the next scan reads. Any combination.
- **Show:** which mailboxes appear in the table, with Select all and Select
  none. A separate question from scanning: pull six in and read them one at a
  time. The Mailbox column shows each message's full address.
- **Columns:** turn any column off. Remembered between launches.
- **?** is a circled question mark in the corner. Switch it on and hovering
  anything explains it; switch it off and tooltips stay out of your way.

Settings can be **exported to a text file** and imported again, from Settings →
Appearance. It is plain JSON with a comment header, so it can be read and
edited anywhere. No passwords or keys are in it: those stay in the Keychain and
are entered again on the other Mac.

## Reading it comfortably

**Settings → Appearance** has three separate controls, because they solve
different problems and not everyone needs both:

- **Appearance** follows macOS, or pins light or dark.
- **Contrast** has a normal level, a high one that darkens every supporting
  colour until it passes against its own background, and a maximum that drops
  colour altogether - black on white or white on black, with nothing depending
  on hue.
- **Tune the layout for reading** changes spacing rather than colour: larger
  type with a little more tracking, taller rows, heavier column headings, a
  wider focus ring, and more room inside every control.

Changes apply as you make them rather than when you press OK.

## How good the offline sorter actually is

Two numbers, and the gap between them is the thing to understand before
trusting it with a mailbox.

Against **real collected mail** - 102 labelled messages from a live inbox - it
gets 99.0% right on job versus not-job, 87.3% on exact category, and 98.2% of
what it files lands in the right folder. That describes ordinary use:
transactional mail comes out of templates, and templates are what a rules
engine is good at.

Against **mail written to be awkward** - a held-out set that avoids every
phrase the engine knows - it gets 37.5%. That is not a bug being hidden; it is
what a phrase matcher does with prose it has never seen.

What holds in both: it filed **nothing** wrongly in either adversarial set,
because anything it cannot read clearly is held for review rather than guessed
at. Getting a message wrong and showing it to you costs a moment. Getting it
wrong and filing it costs you the message.

**None of the evaluation sets is published.** Two were built from a real
mailbox; every name was replaced, but four separate passes each found something
the one before had missed, so keeping them off the internet beats asserting
they are clean. The other three were written by hand and go with them, because
"the synthetic ones are safe" is a judgement and judgements here have been
wrong. What ships is the tooling and the guards that check a corpus  - 
`tests/private_fixtures.py` says where your own sets go.

```bash
./dev eval --file your-set.json  # any labelled set of your own
python tools/corpus.py --fetch   # six thousand real messages, once
python tools/corpus.py           # then run against them
```

That last is the SpamAssassin public corpus, 2002-2005, ham and spam labelled.
Its vocabulary is twenty years old, so recall there is a floor rather than a
description of a modern inbox. What carries over is structural, and it earned
its place by finding two real defects: a message beginning with seventy
underscores took **43 seconds** to classify, and work-from-home spam read as an
interview next step.

The number worth watching is not how much spam reaches Junk. It is how much
ordinary post does: **0.07%**, and **none** of it filed as job mail.

## Privacy

With the default sorter, no message text leaves your Mac. Credentials live in
the macOS Keychain, never in a file, and never appear in a log line, an error
message or a traceback - there is a test that goes looking for them. Message
bodies are never written to the log; a subject line can be, so the log and its
rotated backups are written `0600`, owner-only, like the settings file.

The two files that do describe your mail - the summaries kept between scans and
what the app learned from your corrections - are **encrypted on disk** with
AES-GCM under a key held in the Keychain. If the key cannot be reached, the
summaries are left out rather than written in the clear.

[Full details in SECURITY.md](SECURITY.md).

## Licence and legal

MIT ([LICENSE](LICENSE)), with **no warranty** - which matters for a program
that moves real mail. Nothing is deleted, nothing moves until you press Apply,
and undo goes ten deep; none of that replaces a backup.

The disk image also carries Qt under the **LGPL v3** as replaceable dynamic
libraries. Bundled components and the lexicon's data sources are listed in
[THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md).

Mail Manager is independent and **not affiliated with Apple, Google,
Anthropic, OpenAI, Microsoft or any mail provider**. [LEGAL.md](LEGAL.md) has
the warranty position, trademark notices, data handling, the encryption
notice, and contribution terms.

## Development

```bash
./dev demo      # the app with sample mail, no setup
./dev test      # 3,032 tests, about three minutes on four workers
./dev eval      # sorter accuracy (needs a labelled set of your own)
./dev fake      # the whole pipeline in the terminal, offline
```

`./dev` lists everything. The [handbook](docs/HANDBOOK.md) covers the
architecture, classification rules, IMAP behaviour and safety model.

## AI-assisted tools

During development and campaign preparation, the Mail Manager team used
AI-assisted tools in a limited supporting role, including coding assistance,
copy editing, and the preparation of some sample display content. The same
notice is in the app, under **Help → About Mail Manager**.
