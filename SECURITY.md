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

With the default sorter, no message text leaves your Mac. With a model
backend, the subject, sender and a trimmed body go to that provider.
Attachments are never uploaded.

The pictures in a message are fetched from wherever the sender keeps them
when the preview shows it, which tells the sender it was opened. Settings
turns that off; the one-pixel images mail uses only for that are never
fetched either way, and nothing else a message refers to is.

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
(CVE-2026-69247, -69248 and -69249). None is reachable: the app uses only its
AES-GCM, and TLS goes through Python's own `ssl`. Later versions ship only for
Apple silicon. `tests/test_abuse.py` fails if the app starts using anything
else from `cryptography`.

## Supported versions

The latest release.

See [LEGAL.md](LEGAL.md) for the warranty, trademark and encryption notices.
