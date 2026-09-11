# Security

Mail Manager reads your mail. That deserves a straight account of what it does
with it, and a way to tell us when something is wrong.

## Reporting a vulnerability

Open a [security advisory](https://github.com/NickCysEDU/Mail-Manager/security/advisories/new)
rather than a public issue. Include what you did, what happened, and what you
expected. You will get a reply within a few days.

Please do not include real credentials or real message content in a report.

## Where your data goes

| Data | Where it lives |
|---|---|
| iCloud app-specific password | macOS Keychain, service `iCloud Job Triage` |
| Model API keys | macOS Keychain, one entry per backend |
| Settings | `~/Library/Application Support/Mail Manager/settings.json`, mode `0600` |
| What it learned from your corrections | `corrections.json`, **encrypted**, mode `0600` |
| Verdicts kept between scans | `verdicts.json`, **encrypted**, mode `0600` |
| Logs | `~/Library/Logs/Mail Manager/`, mode `0600`, rotated at 2 MB |
| Your mail | Read over TLS, held in memory for the length of a scan, never written to disk |

With the default sorter, **no message text leaves your Mac at all**. If you
choose a cloud model backend, the subject, sender and a trimmed body are sent to
that provider so it can classify them. Attachments are never uploaded; only
their filenames are mentioned.

## What the app does to protect you

- **TLS everywhere.** IMAP uses `ssl.create_default_context()`, which verifies
  certificates and hostnames. So does every HTTP backend.
- **The two files that describe your mail are encrypted.** `verdicts.json`
  holds a summary and a line of reasoning for every message classified, and
  `corrections.json` holds every sender you have filed by hand. Both are
  AES-GCM with a key kept in the Keychain, so another program running as you
  can read neither, and neither ends up readable in a Time Machine snapshot.
  `--self-test` proves the round trip rather than asserting it.
  When it cannot encrypt, it leaves the summaries out rather than writing
  them in the clear.
- **A link goes where it says it goes.** The links panel builds each anchor
  from a URL the sender chose, and Qt's rich text parser does not expand
  entities inside an attribute. So the URL is percent-encoded for the few
  characters that would end the attribute early rather than escaped as text -
  otherwise a crafted link could close the `href`, append a second one, and
  send the click somewhere the displayed text never mentioned.
- **No plaintext to a remote host.** A custom endpoint on `http://` is refused
  unless it is on this machine or your local network, because every request
  carries the text of an email. The host is parsed as an address rather than
  matched as a string, so `127.0.0.1.evil.com` is a remote host and is
  refused.
- **Nothing sensitive is logged.** Message bodies, subjects, passwords and keys
  never reach the log file, and the HTTP libraries are pinned to `WARNING` so
  they cannot log URLs. There is a test for the credentials half of that: it
  fails a login and a provider call on purpose and searches the error text,
  the traceback and every log line for the password and the key that caused
  them. The log is written `0600` regardless, because it does name your
  mailboxes.
- **Errors are trimmed.** A server response quoted in an error message is
  shortened to a single short line, so an endpoint cannot echo your mail back
  into a dialog.
- **Message text is untrusted input.** It is escaped before it reaches a model
  prompt, and the system prompt tells the model never to follow instructions
  found inside an email. The offline sorter treats such attempts as spam.
- **Nothing moves without confirmation.** A message is only filed if you tick
  it, or if you explicitly turn on automatic filing for background scans, and
  even then only for messages the app would have pre-ticked.
- **A copy is confirmed before the original is touched.** `UID COPY` must
  return `OK` before anything is flagged for deletion, and `UID EXPUNGE` is
  used so only the app's own messages are removed.

## What the app cannot protect you from

- An unsigned build. This project is not signed with a paid Apple Developer
  certificate, so macOS shows a Gatekeeper prompt on first launch. Build it
  yourself from source if that matters to you.
- Whatever your chosen model provider does with the text you send it. Read
  their policy, or use the default sorter and send nothing.
- Anyone with access to your unlocked Mac and Keychain. Encryption at rest
  keeps the two sensitive files away from *other programs* running as you; it
  cannot protect anything from somebody who can run this one.

## Supported versions

The latest release on `main` is the supported version.
