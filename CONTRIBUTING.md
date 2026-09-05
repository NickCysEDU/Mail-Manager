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
