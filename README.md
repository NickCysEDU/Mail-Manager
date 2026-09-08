<div align="center">

<img src="docs/assets/icon-128.png" width="112" height="112" alt="Mail Manager">

# Mail Manager

**Sort your job-search email in iCloud. On your Mac, in seconds, without an API key.**

[Download](https://github.com/NickCysEDU/Mail-Manager/releases/latest) ·
[Handbook](docs/HANDBOOK.md) ·
[Security](SECURITY.md) ·
[Contributing](CONTRIBUTING.md)

![macOS 13+](https://img.shields.io/badge/macOS-13%2B-black)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)
![Tests](https://img.shields.io/badge/tests-1%2C334%20passing-brightgreen)
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

- **Nothing moves without you.** Every message gets a tick box. Only ticked ones
  are filed.
- **Uncertainty goes to a folder, not a guess.** Anything below the confidence
  bar lands in `Needs Review` instead of a category folder.
- **It creates its own folders.** You do not touch Mail.
- **Read-only until you say otherwise.** Messages are fetched with `BODY.PEEK`,
  so nothing is marked as read.

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

The built-in sorter is a rule set, not a model: about 530 weighted signals
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
belongs to, and which of twelve everyday topics it is. Only the first got
folders. Under **⚙︎ → What to sort** you choose which distinctions are worth a
folder of their own:

| Profile | Job folders | Everything else |
| --- | --- | --- |
| **Job search** *(default)* | seven | left in your inbox |
| **Job search and everyday mail** | seven | filed under `Sorted Mail/` |
| **Everyday mail** | one | filed under `Sorted Mail/` |
| **Essentials only** | one | six folders: security, finance, receipts, travel, newsletters, promotions |

The everyday topics are Security, Finance, Receipts, Shipping, Travel, Events,
Work, Personal, Social, Newsletters, Promotions and Junk. All of it runs on the
same offline rules, so none of it needs an API key.

Changing profile refiles the rows already on screen. Nothing moves until you
press Apply, as ever.

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
  schedule a 20-minute Google Meet call" — the fixed phrase "schedule a call"
  breaks the moment somebody says how long it will take.
- **A job description you saved or mailed to yourself.** It contains not one
  word a hiring process uses. It is all headings.

Both are checked against what the meeting is *for*. A booking link proves a
meeting and never that it is about a job, so a dentist's reminder, a
parent-teacher conference and a sales demo stay where they are.

## Rules

**Settings → Auto Reply.** A rule is a list of conditions and a list of things
to do, so it is whatever you need it to be rather than one of a fixed set.

Five rules ship, all switched off: acknowledging an interview invitation,
answering a request for documents or times, declining a recruiter politely,
filing security notices, and leaving anything from a colleague alone. They are
worked examples as much as defaults — open one, see how it is built, change it.

**Conditions** test the job category, the everyday topic, the sender, the
sender's domain, the subject, the message text, either of those, the
confidence, which mailbox it arrived in, how old it is, and whether it is bulk
mail, carries an attachment or is a reply. Operators are the obvious ones —
is, is not, contains, does not contain, starts with, ends with, is exactly,
matches a pattern, is at least, is at most. A rule matches on **all** of its
conditions or on **any** of them.

**Actions** are what happens next:

| Action | What it does |
| --- | --- |
| Draft a reply from a template | Fills `{first_name}`, `{sender}`, `{subject}` and `{me}` |
| Draft a reply with the model | Hands the message to the model with your guidance |
| File it into a folder | Points the row at a folder. Nothing moves until you press Apply |
| Tick it / Leave it unticked | Sets the checkbox, so Apply picks it up or skips it |
| Mark it as read | `\Seen` on the server |
| Flag it | `\Flagged` on the server |
| Leave it where it is | Cancels any filing an earlier rule asked for |
| Stop | Skips every later rule for that message |

Rules run top to bottom, and **↑ ↓** reorder them. A later rule adds to what an
earlier one decided until one of them says stop, which is what makes an
exception at the top of the list work: *leave anything from a colleague alone,
and stop* above a rule that files everything else.

**Try it on the last scan** runs every finished, switched-on rule over the
messages already on screen and tells you what would happen, without touching
the mailbox or the model. Reading a rule and knowing what it does are different
things.

A rule that is half-written is never run — the editor lists what is missing
rather than refusing to save it, because half-written is a normal state to
leave a rule in overnight. The same list catches a pattern that would hang the
app: `(a+)+` and `.*.*x` are refused before they run, because a regular
expression cannot be interrupted once it has started.

**Nothing is ever sent.** A drafted reply is written into your Drafts mailbox,
threaded correctly with `In-Reply-To` and `References`, and you press send
yourself in Mail. An app that answers a stranger's post on your behalf, with a
model, without you reading it first, is not a feature anybody asks for twice.

The model is told not to invent a fact and not to commit you to anything; where
it needs something it does not have, it leaves `[a note in brackets]` and the
draft lists those at the bottom. Bulk mail is skipped by default, per rule.

## Undo

⌘Z after an Apply moves everything back where it came from. The app moves real
mail, and knowing you can put it back is the difference between trying it and
being careful with it.

## Getting around

- **Scan:** — which mailboxes the next scan reads. Any combination.
- **Show:** — which mailboxes appear in the table, with Select all and Select
  none. A separate question from scanning: pull six in and read them one at a
  time. The Mailbox column shows each message's full address.
- **Columns** — turn any column off. Remembered between launches.
- **?** — a circled question mark in the corner. Switch it on and hovering
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

Two numbers, and the gap between them is worth understanding before trusting
it with a mailbox.

Against **real collected mail** - 102 labelled messages from a live inbox - it
gets 98.0% right on job versus not-job, 83.3% on the exact category, and 98.1%
of what it files goes to the right folder. That is the number that describes
ordinary use, because ordinary transactional mail comes out of templates and
templates are what a rules engine is good at.

Against **mail written by hand to be awkward** - a held-out set that
deliberately avoids every phrase the engine knows - it gets 16.7%. That is not
a bug being hidden; it is what a phrase-and-structure matcher does with prose
it has never seen.

What holds in both cases is the part that matters: it filed **nothing** wrongly
in either adversarial set, because anything it cannot read clearly is held for
review instead of guessed at. Getting a message wrong and showing it to you
costs you a moment. Getting it wrong and filing it costs you the message.

```bash
./dev eval                       # against real collected mail
python tools/adversarial.py      # against the two hand-written sets
python tools/corpus.py --fetch   # six thousand real messages, once
python tools/corpus.py           # then run against them
```

The third is the SpamAssassin public corpus: six thousand real messages from
2002-2005, ham and spam labelled. Its vocabulary is twenty years old, so the
recall figure is a floor rather than a description of a modern inbox. What does
carry over is everything structural, and it earned its place by finding two
real defects: a message beginning with seventy underscores took **43 seconds**
to classify, and work-from-home spam was being read as an interview next step.

The number worth watching there is not how much spam reaches Junk. It is how
much ordinary post does: **0.1%**, and none of it filed as job mail.

If you want the harder cases sorted rather than queued, that is what the model
backends are for. Point it at Gemini or Claude and the same messages get read
properly.

## Privacy

With the default sorter, no message text leaves your Mac. Credentials live in
the macOS Keychain, never in a file. Message bodies are never written to the
log. [Full details in SECURITY.md](SECURITY.md).

## Development

```bash
./dev demo      # the app with sample mail, no setup
./dev test      # 1,334 tests, about two and a half minutes
./dev eval      # sorter accuracy against the labelled fixture
./dev fake      # the whole pipeline in the terminal, offline
```

`./dev` lists everything. The [handbook](docs/HANDBOOK.md) covers the
architecture, the classification rules, IMAP behaviour and the safety model in
detail.

## License

MIT. See [LICENSE](LICENSE).
