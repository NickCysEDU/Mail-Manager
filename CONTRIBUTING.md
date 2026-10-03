# Contributing

```bash
git clone https://github.com/NickCysEDU/Mail-Manager.git
cd Mail-Manager
./dev demo     # builds a virtualenv and opens the app with sample mail
```

`./dev` lists every command.

## Before a pull request

```bash
./dev test     # the full suite
./dev eval     # sorter accuracy, if you have a labelled set
```

Both must pass. If you change the sorter, give the before and after numbers.

## House rules

- **No credentials or real mail in the repository.** Labelled sets built from
  a real inbox go in `tests/fixtures/private/`, which is ignored; the privacy
  tests run over it.
- **Precision before recall.** A message the sorter is unsure of waits in
  Needs Review; a confident mistake lands in a folder nobody checks.
- **Do not add a signal because a held-out message failed.** The held-out set
  then stops measuring anything. A safety test fails if it starts scoring like
  the development set.

## Adding sorter signals

Shared hiring language is in `rules_engine.py`; field overlays are in
`rulesets.py`. Add a phrase with a weight between 0.5 and 3.0, run `./dev
eval`, and keep it if accuracy rises and precision does not fall.

## The icon

`tools/make_icon.py` draws every size of the icon and the website's images.
Commit what it writes; the build does not regenerate them. `tests/test_icons.py`
holds the outline's measurements.

## Licence of contributions

Contributions are accepted under the project's MIT licence. Opening a pull
request confirms that you wrote it or may submit it, and that you licence it on
those terms. There is no CLA.
