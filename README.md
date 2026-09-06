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
![Tests](https://img.shields.io/badge/tests-1%2C109%20passing-brightgreen)
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

## Privacy

With the default sorter, no message text leaves your Mac. Credentials live in
the macOS Keychain, never in a file. Message bodies are never written to the
log. [Full details in SECURITY.md](SECURITY.md).

## Development

```bash
./dev demo      # the app with sample mail, no setup
./dev test      # 1,109 tests, about 35 seconds
./dev eval      # sorter accuracy against the labelled fixture
./dev fake      # the whole pipeline in the terminal, offline
```

`./dev` lists everything. The [handbook](docs/HANDBOOK.md) covers the
architecture, the classification rules, IMAP behaviour and the safety model in
detail.

## License

MIT. See [LICENSE](LICENSE).
