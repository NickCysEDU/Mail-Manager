# Legal notices

Mail Manager is MIT-licensed free software ([LICENSE](LICENSE)). This file
covers what the licence does not. It is not legal advice.

## No warranty

The software is provided as is, with no warranty, and the authors are not
liable for damages arising from it. It moves real mail, so a bug or an unusual
server can put a message somewhere you did not intend. Nothing moves until
you press Apply, a copy is confirmed before the original is removed, and undo
goes ten filings deep. Filing never deletes; only Clear out mail and Empty a
folder do, after a count and your confirmation. None of that replaces a
backup.

## Your data

The app has no server, no account, no analytics and no telemetry. It connects
to your mail provider; to a model backend only if you choose one; and once a
day to GitHub for the latest version number, unless you turn that off.
Credentials live in the Keychain, and the two files that describe your mail are
encrypted. [SECURITY.md](SECURITY.md) has the detail.

## Trademarks and affiliation

**Mail Manager is independent and not affiliated with, endorsed by or
connected to any company named here.** Names are used only to say what the
software works with and belong to their owners: Apple, macOS, iCloud and
Keychain (Apple Inc.); Google, Gmail and Gemini (Google LLC); Anthropic and
Claude (Anthropic, PBC); OpenAI (OpenAI, Inc.); Microsoft and Outlook
(Microsoft Corporation); Yahoo (Yahoo Inc.); Fastmail (Fastmail Pty Ltd);
Proton Mail (Proton AG); Qt (The Qt Company Ltd); Ollama (Ollama Inc.);
LinkedIn (LinkedIn Corporation). Other names belong to their owners.

## Third-party software

The app includes Qt under the LGPL v3, among others. Every bundled component
and the lexicon's data sources are listed in
[THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md), which also ships inside the
app.

## Encryption

The app encrypts two local files with AES-GCM via the `cryptography` package
and uses TLS for connections; it implements no cryptography itself.
Encryption software is regulated in some countries, including under the US
Export Administration Regulations. If you redistribute it where such rules
apply, compliance is yours to determine.

## Contributions

Contributions are accepted under the MIT licence. Opening a pull request
confirms you wrote it or may submit it, and that you licence it on those terms.

## Code signature

Releases are signed with a local certificate, not an Apple Developer ID, and
are not notarised. macOS will say the developer cannot be verified;
right-click and choose Open the first time. The release page lists each
download's SHA-256.

## AI tools

During development and campaign preparation, the Mail Manager team used
AI-assisted tools in a limited supporting role, including coding assistance,
copy editing, and the preparation of some sample display content.
