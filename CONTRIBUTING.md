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
./dev eval          # accuracy, if you have a labelled set (see below)
```

Both must pass. If you change the sorter, `./dev eval` should not go down:
report the before and after numbers in the pull request.

## House rules

- **No credentials or real mail in the repository.** The two sets built from
  a real inbox - `labelled.json` and `acknowledgements.json` - are not
  committed; `tests/fixtures/private/` is ignored. The suite skips what needs
  them and says why. If you build your own, put it there: the privacy guards
  in `tests/test_privacy.py` run over anything found in that directory, which
  is the point of them.

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

## Tests that are meant to fail

Three suites exist to be hostile rather than reassuring, and they are the ones
worth reading before changing the sorter or the settings layer.

`tests/test_abuse.py` feeds the app things a user or a server can actually
produce: ports that are not numbers, settings files that are JSON arrays,
mailbox names of `..`, subjects made of zero-width characters, 200 KB bodies,
prompt injection in the body. It has found real defects - a mailbox called `..`
used to reach `CREATE`, which matters on the many IMAP servers that still store
one mailbox per directory. Every case that found something stays as a guard.

`tests/test_sorter_safety.py` pins the property the design rests on - nothing
confidently filed into the wrong folder - and deliberately does not pin
accuracy. An accuracy assertion turns every fixture into something to fit to.

`tools/adversarial.py` scores two written sets. The first guided the structural
work and now flatters it; the second was written afterwards and is scored once.
**Do not add a signal because a message in the held-out set failed.** If you do,
it stops measuring anything and the next person needs a third set. One of the
safety tests fails if the held-out set ever starts scoring like the dev set,
which is what fitting to it looks like from the outside.
