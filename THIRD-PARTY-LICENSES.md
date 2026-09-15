# Third-party software in Mail Manager

The app is MIT licensed. The disk image also contains the libraries below,
which are not, and this is where they are named.

## Qt, via PySide6

**Qt and PySide6 are used under the LGPL version 3.** They are bundled as
separate dynamic libraries inside `Mail Manager.app/Contents/Frameworks`,
not linked into the executable, so they can be replaced with another build
of the same version - which is the freedom the LGPL is there to preserve.

- Licence: <https://www.gnu.org/licenses/lgpl-3.0.html>
- Qt source: <https://download.qt.io/official_releases/qt/>
- PySide6 source: <https://code.qt.io/cgit/pyside/pyside-setup.git/>

No Qt source is modified by this project.

## Everything else in the bundle

| Package | Version | Licence |
|---|---|---|
| PySide6 | 6.11.2 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| anthropic | 1.4.0 | MIT |
| certifi | 2026.7.22 | MPL-2.0 |
| cryptography | 48.0.1 | Apache-2.0 OR BSD-3-Clause |
| httpcore2 | 2.12.0 | BSD-3-Clause |
| httpx2 | 2.12.0 | BSD-3-Clause |
| importlib_metadata | 9.0.1 | Apache-2.0 |
| keyring | 25.7.0 | MIT |
| pydantic | 2.13.5 | MIT |
| shiboken6 | 6.11.2 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |

## Data

The offline sorter ships a lexicon of company and airport names built from
**OurAirports** (public domain) and **Wikidata** (CC0). Both allow
redistribution; `data/lexicon.json.gz` records the provenance in its own
`sources` field.

The CA bundle is **certifi**, under MPL-2.0, shipped unmodified.
