# Security

## Reporting a vulnerability

Open a [security advisory](https://github.com/NickCysEDU/Mail-Manager/security/advisories/new),
not a public issue: what you did, what happened, and what you expected. Please
leave real credentials and real mail out of it. You will get a reply within a
few days.

## Where your data goes

| Data | Where it lives |
|---|---|
| Mail passwords and API keys | macOS Keychain |
| Settings | `~/Library/Application Support/Mail Manager/settings.json`, `0600` |
| Verdicts kept between scans | `verdicts.json`, encrypted, `0600` |
| What it learned from corrections | `corrections.json`, encrypted, `0600` |
| Logs | `~/Library/Logs/Mail Manager/`, `0600`, no message bodies |
| Your mail | Read over TLS, kept in memory for a scan, never written to disk |

With the default sorter, or a model running on this Mac, no message text
leaves it. With a model elsewhere, each message a scan reads goes to that
provider: who sent it and to whom, its subject and date, the names of its
attachments, where up to twelve of its links go, and its text without quoted
history, signatures and footers, trimmed when long. Codes, passwords and keys
in its words, and the queries, logins and keys in its links, are taken out
first. While "Reuse verdicts from earlier scans" is on, as it is to begin
with, a message already sorted with the same settings is not sent again. A
rule that drafts a reply with a model sends the message it answers, cut down
the same way, your name and what the rule asks for. Attachments themselves
are never uploaded.

The first time a model elsewhere is chosen, in Settings, from the model menu
or while setting up, the app says what it will be sent and goes ahead only if
you say so.

What a provider keeps, for how long and what for is up to its own terms.
On Gemini's free tier, Google may use what it is sent to improve its
products and have people read it; with billing on the key's project, it
does not. Claude's and OpenAI's APIs do not train on what they are sent
unless you opt in, and keep it for a limited time to check for abuse.

A message's pictures are fetched from wherever the sender keeps them only
when you press Load Images over it, or for every message if Settings says
to: fetching them tells the sender it was opened, and from where. The
one-pixel images mail uses only for that are never fetched, and nothing else
a message refers to is.

Once a day, unless turned off in Settings, the app asks GitHub for the latest
version number. Nothing else is sent.

## Protections

- **TLS everywhere,** with certificates and hostnames verified. A custom
  endpoint on plain `http://` is refused unless it is on this machine or your
  local network.
- **Encryption at rest:** the two files that describe your mail use AES-GCM
  with a key in the Keychain. If the key cannot be reached, summaries are
  left out rather than written in the clear.
- **Nothing sensitive is logged,** and a test searches errors and logs for
  credentials.
- **Mail is untrusted input.** It is escaped before it reaches a model, which
  is told never to follow instructions inside an email.
- **Secrets stay here.** A model is never sent the codes, passwords and keys
  a message carries, nor the keys in its links: it needs to know one came,
  not what it was.
- **Pictures wait to be asked for,** message by message, so opening mail does
  not report it to the sender.
- **Links show where they go** before anything opens, and only web and mail
  addresses open at all.
- **Nothing moves without you.** A copy is confirmed before the original is
  removed, and only the app's own messages are expunged.
- **Updates are checked** against the size and SHA-256 GitHub reports, and
  must be signed with the same certificate as the copy that is running.

## What it cannot protect you from

- **Gatekeeper.** The app is not signed with a paid Apple Developer ID, so
  macOS warns on first launch. Build it yourself if that matters to you.
- **Your model provider,** whose own terms cover what you send it.
- **Someone with your unlocked Mac and Keychain.**

## A pinned dependency

`cryptography` is pinned to 48.x, which has three open advisories
(CVE-2026-69247, -69248 and -69249), in PKCS#7 decryption and in X.509
certificate checking. None is reachable: the app uses only its AES (AES-GCM
for its own files, and plain AES to open an Office document protected with a
password, as Office's protection is built), and TLS goes through Python's own
`ssl`. Later versions ship only for Apple silicon. `tests/test_abuse.py` fails
if the app starts using anything else from `cryptography`.

## Supported versions

The latest release.

See [LEGAL.md](LEGAL.md) for the warranty, trademark and encryption notices.
