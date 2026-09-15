# Legal notices

Mail Manager is MIT-licensed free software ([LICENSE](LICENSE)). This file
covers what the licence does not, in plainer words. It describes the project;
it is not legal advice.

## No warranty

The MIT licence provides the software "as is", with no warranty, and the
authors are not liable for damages arising from it.

That matters more than usual here, because this app moves real mail. A bug, a
bad network or an unusual server can put a message somewhere you did not
intend. The design limits the damage:

- Nothing moves until you press **Apply**.
- Messages are copied, then removed from the source using the identifier the
  server returns, so a failed move leaves the original in place.
- **Nothing is ever deleted.** Filing moves messages between folders.
- **Undo** goes ten filings deep.
- Drafts are written to your Drafts mailbox and never sent.

None of that is a guarantee, and none of it replaces a backup.

## Your data

No server, no account, no analytics, no telemetry, no update check. The app
connects to your mail provider, and to a model backend only if you supply an
API key.

- **Credentials** live in the macOS Keychain, never in a file.
- **Two files describe your mail** - decisions and your corrections. Both are
  AES-GCM encrypted under a Keychain key. If that key is unreachable,
  summaries are omitted rather than written in the clear.
- **The built-in sorter sends nothing anywhere.** A cloud backend receives
  message text under that provider's own terms, which are between you and them.

[SECURITY.md](SECURITY.md) has the detail and how to report a vulnerability.

## Trademarks and affiliation

**Mail Manager is independent. It is not affiliated with, endorsed by or
connected to any company named here**, and it is not an Apple product.

Names are used only to say what the software works with. They belong to their
owners: **Apple**, **macOS**, **iCloud**, **Keychain** (Apple Inc.);
**Google**, **Gmail**, **Gemini** (Google LLC); **Anthropic**, **Claude**
(Anthropic, PBC); **OpenAI** (OpenAI, Inc.); **Microsoft**, **Outlook**
(Microsoft Corporation); **Yahoo** (Yahoo Inc.); **Fastmail** (Fastmail Pty
Ltd); **Proton Mail** (Proton AG); **Qt** (The Qt Company Ltd); **Ollama**
(Ollama Inc.); **LinkedIn** (LinkedIn Corporation). Other provider, ATS and
model names belong to their respective owners.

## Third-party software

The disk image includes Qt under the **LGPL v3**, among others. Every bundled
component, its licence, and the lexicon's data sources are listed in
[THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md). Both files also ship
inside the app bundle.

## Encryption

The app encrypts two local files with AES-GCM via the `cryptography` package
and uses TLS for network connections. It implements no cryptography itself.

This is publicly available open-source software. Encryption software is
regulated in some countries, including under the US Export Administration
Regulations. If you redistribute it or use it where such rules apply,
compliance is yours to determine.

## Contributions

Contributions are accepted under the project's MIT licence. Opening a pull
request confirms you wrote it, or may submit it, and that you licence it on
those terms. There is no CLA.

## Code signature

Releases are signed with a local certificate, not an Apple Developer ID, and
are **not notarised**. macOS will say the developer cannot be verified;
right-click and **Open** the first time. A local signature identifies the
build, not the publisher - verify the SHA-256 on the release page if that
matters to you.

## AI tools

During development and campaign preparation, the Mail Manager team used
AI-assisted tools in a limited supporting role, including coding assistance,
copy editing, and the preparation of some sample display content.
