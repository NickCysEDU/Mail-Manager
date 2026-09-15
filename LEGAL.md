# Legal notices

Mail Manager is free software published under the MIT licence in
[LICENSE](LICENSE). This file collects the notices that licence does not
cover, in plainer language than a licence is written in. It is a description
of the project, not legal advice.

## No warranty, and what that means for your mail

The MIT licence says the software is provided "as is", without warranty of
any kind, and that the authors are not liable for any claim, damage or other
liability arising from it. That is the legal wording; here is what it means
in practice for an application whose whole job is moving your email.

**This app files real messages in a real mailbox.** It connects over IMAP and
issues copy and delete commands against your account. A bug, a mis-sorted
message, an interrupted network or a server that behaves unexpectedly can put
a message somewhere you did not intend.

What the design does about that:

- **Nothing moves until you press Apply.** Sorting is a suggestion until then.
- **Messages are copied, then removed from the source** using the identifiers
  the server reports back, so a failed move leaves the original in place.
- **Nothing is ever deleted.** Filing moves a message between folders. The app
  has no feature that empties, expunges or permanently removes anything.
- **Undo goes ten filings deep**, using the same mechanism in reverse.
- **Drafts are never sent.** The reply feature writes to your Drafts mailbox
  and stops there.

None of that is a guarantee, and none of it replaces a backup. If the mail in
your account matters, keep a copy somewhere the app cannot reach.

## Your data

The app runs on your Mac. It has no server, no account, no analytics and no
telemetry, and it makes no network connection except to your mail provider
and — only if you supply an API key — the model backend you chose.

- **Credentials** live in the macOS Keychain, never in a file.
- **Two files describe your mail** (what was decided about each message, and
  what you corrected). Both are encrypted with AES-GCM under a Keychain key.
  When that key cannot be reached, the summaries are left out rather than
  written in the clear.
- **With the built-in sorter, no message text leaves the machine.** With a
  cloud model backend, message text is sent to the provider you chose, under
  that provider's terms and privacy policy, which are between you and them.
- **Nothing is sent anywhere else, ever.** There is no crash reporter, no
  usage statistics and no update check.

[SECURITY.md](SECURITY.md) has the detail, including how to report a
vulnerability.

## Trademarks and affiliation

**Mail Manager is an independent project. It is not affiliated with,
endorsed by, sponsored by or otherwise connected to any of the companies
named below.**

Product and company names are used only to say what the software works with,
which is nominative use — naming a thing in order to refer to it. They remain
the property of their owners:

- **Apple**, **macOS**, **iCloud** and **Keychain** are trademarks of Apple Inc.
- **Google**, **Gmail** and **Gemini** are trademarks of Google LLC.
- **Anthropic** and **Claude** are trademarks of Anthropic, PBC.
- **OpenAI** is a trademark of OpenAI, Inc.
- **Microsoft** and **Outlook** are trademarks of Microsoft Corporation.
- **Yahoo** is a trademark of Yahoo Inc. **Fastmail** is a trademark of
  Fastmail Pty Ltd. **Proton Mail** is a trademark of Proton AG.
- **Qt** is a trademark of The Qt Company Ltd. **Ollama** is a trademark of
  Ollama Inc. **LinkedIn** is a trademark of LinkedIn Corporation.
- Any other name used to describe a mail provider, applicant-tracking system
  or model backend belongs to its respective owner.

The application is not produced by Apple and is not an Apple Mail product.

## Third-party software

The disk image contains software under licences other than MIT, including Qt
under the **LGPL v3**. Every bundled component is named with its licence, and
the offline lexicon's data sources are recorded, in
[THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md). Those files are also
inside the application bundle, so they travel with the program.

## Encryption

The application contains cryptographic functionality: it encrypts two local
files with AES-GCM, using the `cryptography` package, and it uses TLS for
every network connection. It implements no cryptography of its own.

This is publicly available open-source software. Distributing or using
encryption software is regulated in some countries, including under the US
Export Administration Regulations and equivalent rules elsewhere. Anyone who
redistributes this software, or uses it somewhere with such rules, is
responsible for their own compliance; nothing here is a determination about
your situation.

## Contributions

Contributions are accepted under the same MIT licence the project is
published under. By opening a pull request you confirm that you wrote the
contribution or otherwise have the right to submit it, and that you licence
it to the project and its users under those terms. There is no separate
contributor licence agreement to sign.

## The code signature

Releases are signed with a locally generated certificate, not an Apple
Developer ID, and are **not notarised by Apple**. macOS will say the
developer cannot be verified; opening it the first time takes a right-click
and **Open**. A local signature identifies the build, not the publisher, and
you should treat it accordingly — verify the SHA-256 on the release page if
that matters to you.

## Use of AI tools

During development and campaign preparation, the Mail Manager team used
AI-assisted tools in a limited supporting role, including coding assistance,
copy editing, and the preparation of some sample display content.
