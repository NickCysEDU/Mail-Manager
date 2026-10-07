<div align="center">

<img src="docs/assets/icon-128.png" width="112" height="112" alt="Mail Manager">

# Mail Manager

**Sorts your mail on your Mac. No account, no API key, and nothing moves until you say.**

[Download](https://github.com/NickCysEDU/Mail-Manager/releases/latest) ·
[Handbook](docs/HANDBOOK.md) ·
[Security](SECURITY.md) ·
[Contributing](CONTRIBUTING.md)

<img src="docs/screenshot.png" width="900" alt="The approval table, with a summary, category, destination folder and confidence score for every message">

</div>

## What it does

Mail Manager reads a window of your inbox over IMAP, works out what each
message is, and shows a table: a summary, a category, a folder and a
confidence score for every row. Tick what you want filed and press Apply.

- Job-search mail goes into folders such as Interview, Next Steps, Offers and
  Not Interested.
- Everyday mail can be sorted too, into as many of thirteen topics as you
  like, or left where it is.
- Anything it is unsure of goes to Needs Review rather than a guess.
- Nothing is marked as read, and ⌘Z undoes a filing.

## Install

Download the `.dmg` from the
[latest release](https://github.com/NickCysEDU/Mail-Manager/releases/latest),
open it, and drag Mail Manager to Applications. It runs on Apple silicon and
Intel, on macOS 13 or later.

The app is not signed with an Apple Developer certificate, so the first time,
right-click it in Applications and choose **Open**. After that, new versions
are offered in the app and install in one click.

<details>
<summary>Build it yourself</summary>

```bash
git clone https://github.com/NickCysEDU/Mail-Manager.git
cd Mail-Manager
./dev build      # virtualenv, dependencies, tests, then the .app
./dev install    # copies it to /Applications
```

Needs Python 3.11 or later.
</details>

## Setting up

The first run asks for a mailbox and its app password, and which folders to
create. iCloud, Gmail, Outlook, Yahoo, Fastmail and other IMAP servers work;
Proton works through its Bridge. **Help → Add or Link Mailboxes** adds more.

## Sorting

The built-in sorter is a rule set that runs on your Mac: instant, free, and no
message text leaves the machine. It reads a message by what it does, not
only by the phrases in it: an offer by its terms, a rejection by its
consolations, a second interview by the days offered. On 102 real messages
it agreed with a language model on job versus not job 99% of the time, and
on the exact category 96%. On a set it had never seen, 79% exact, and every
message it was sure enough to file, it filed right.

You can use a model instead: Ollama on your Mac, or Gemini, Claude or an
OpenAI-compatible service with your own key, in **Settings → Analysis**.

## Also

- **Read and write** like any mail app: double-click a message for its own
  window, then reply, reply all, forward, flag, archive, delete or move it.
  Replies and new messages are written with formatting, pictures and
  attachments under a sign-off of your own, go out through your own mailbox,
  and a copy lands in Sent.
- **Rules** file, tick, flag, mark as read, or draft a reply into Drafts.
  A rule never sends anything; only you do.
- **Schedule** scans on a timer, and can keep scanning after you quit.
- **Briefing** reads the last scan back, most urgent first.
- **Clear out mail** deletes in bulk by sender, subject or age, after a count
  from the server.
- **Attachments** open in a viewer that never runs anything. Audio files come
  with a music visualiser; its strobe is off by default, and at its fastest
  it can trigger photosensitive epilepsy.
- **Links** in a message are listed by where they really go, and the address
  is shown before anything opens.
- **Touch Bar**: each window's controls are on it, on a MacBook Pro that has
  one; a button that opens onto a slider can be held and dragged instead.

## Privacy

With the built-in sorter, no message text leaves your Mac. Passwords and keys
are kept in the Keychain, and what the app keeps about your mail is encrypted
on disk. Once a day it asks GitHub for the latest version number; Settings
turns that off. See [SECURITY.md](SECURITY.md).

## Development

```bash
./dev demo     # the app with sample mail, no setup
./dev test     # 4,915 tests, about five minutes on four workers
./dev eval     # sorter accuracy, on a labelled set of your own
```

The [handbook](docs/HANDBOOK.md) covers how it works.

## Licence

MIT ([LICENSE](LICENSE)), with no warranty. The app carries Qt under the
LGPL v3; see [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md). Mail Manager
is not affiliated with Apple, Google, Anthropic, OpenAI, Microsoft or any mail
provider; see [LEGAL.md](LEGAL.md).

## AI-assisted tools

During development and campaign preparation, the Mail Manager team used
AI-assisted tools in a limited supporting role, including coding assistance,
copy editing, and the preparation of some sample display content. The same
notice is in the app, under **Help → About Mail Manager**.
