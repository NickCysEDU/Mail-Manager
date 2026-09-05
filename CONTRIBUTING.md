# Contributing

Thanks for taking a look. The project is small and the setup is one command.

```bash
git clone https://github.com/NickCysEDU/Mail-Manager.git
cd Mail-Manager
./dev demo          # builds a virtualenv and opens the app with sample mail
```

`./dev` on its own lists every command.

## Before you open a pull request

```bash
./dev test          # the full suite, about 30 seconds
./dev eval          # accuracy of the offline sorter against the labelled set
```

Both must pass. If you change the sorter, `./dev eval` should not go down:
report the before and after numbers in the pull request.

## House rules

- **No credentials or real mail in the repository.** The labelled fixture in
  `tests/fixtures/` is anonymised; keep it that way.
- **Tests do not touch the network or the Keychain.** `tests/conftest.py` has a
  fake IMAP server and a fake API client to build on.
- **Behaviour changes come with a test.** Especially anything that decides
  where a message is filed.
- Write comments that explain why, not what. Skip the ones that restate the
  code.

## Adding sorter signals

`rules_engine.py` holds the shared hiring language; `rulesets.py` holds the
field-specific overlays. Add a phrase with a weight between 0.5 and 3.0, run
`./dev eval`, and keep the change if accuracy improves and precision does not
fall.

Precision matters more than recall here. A message the sorter is unsure about
goes to Needs Review, which costs a few seconds. A message it is confidently
wrong about ends up in a folder nobody checks.

## The icon

The icon is drawn in code, not stored as art. `tools/make_icon.py` renders
every size the app and the website need:

```bash
python tools/make_icon.py
```

That writes `assets/icon.png` and `assets/icon.icns` for the bundle, and the
favicon, logo and social card under `docs/assets/`. Commit the results; the
build does not regenerate them.

Two things are worth knowing before changing it:

- The outline is not a rounded rectangle and not a superellipse. macOS keeps a
  straight edge out to about 60% of the half-width and then turns through a
  corner fitting an exponent of 1.8. Those numbers were measured off
  `Mail.app`, and `tests/test_icons.py` holds them, because a shape that is
  even slightly rounder reads as "not a Mac app" next to the system icons.
- Below 44 points the drawing changes rather than shrinking: the shadow goes,
  the gradient flattens, and the envelope grows to fill the space a margin
  would otherwise waste. Check `icon_16x16` after any edit.

The GitHub repository card is `docs/assets/social-preview.png`. GitHub has no
file convention for it, so it has to be uploaded by hand once, under
**Settings -> General -> Social preview**.
