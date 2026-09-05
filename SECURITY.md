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
| Logs | `~/Library/Logs/Mail Manager/`, rotated at 2 MB |
| Your mail | Read over TLS, held in memory for the length of a scan, never written to disk |

With the default sorter, **no message text leaves your Mac at all**. If you
choose a cloud model backend, the subject, sender and a trimmed body are sent to
that provider so it can classify them. Attachments are never uploaded; only
their filenames are mentioned.

## What the app does to protect you

- **TLS everywhere.** IMAP uses `ssl.create_default_context()`, which verifies
  certificates and hostnames. So does every HTTP backend.
- **No plaintext to a remote host.** A custom endpoint on `http://` is refused
  unless it is on this machine or your local network, because every request
  carries the text of an email.
- **Nothing sensitive is logged.** Message bodies, subjects, passwords and keys
  never reach the log file, and the HTTP libraries are pinned to `WARNING` so
  they cannot log URLs.
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
- Anyone with access to your unlocked Mac and Keychain.

## Supported versions

The latest release on `main` is the supported version.
