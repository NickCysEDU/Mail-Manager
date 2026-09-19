"""The documentation has to agree with the code, and with itself.

Every number in a README goes stale, and a stale number in a security
document is worse than no number. These check the claims that can be checked
mechanically: counts, the default backend, links that resolve, and the two
statements that used to contradict each other.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import git_lines

import profiles
import providers
import rules_engine
import rulesets
from models import OtherCategory

ROOT = Path(__file__).resolve().parent.parent
DOCS = ("README.md", "SECURITY.md", "CONTRIBUTING.md",
        "docs/HANDBOOK.md", "docs/HANDOFF.md")


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def everything() -> str:
    return "\n".join(read(name) for name in DOCS)


def signal_count() -> int:
    total = sum(len(t) for _c, t in rules_engine._CATEGORY_TABLES)
    total += sum(len(t) for t in rules_engine.TOPIC_SIGNALS.values())
    total += len(rules_engine.JOB_CONTEXT_SIGNALS)
    total += len(rules_engine.NON_JOB_SIGNALS)
    total += sum(len(t) for t in rules_engine.CONDITIONAL_SIGNALS.values())
    return total


class TestTheNumbersAreCurrent:
    def test_the_signal_count(self):
        stated = {int(n.replace(",", "")) for n in
                  re.findall(r"(\d[\d,]*) weighted", everything())}
        assert stated, "no signal count is quoted anywhere"
        assert stated == {signal_count()}, (stated, signal_count())

    def test_the_overlay_count(self):
        overlay = sum(rulesets.get(n).signal_count
                      for n, _l, _b in rulesets.choices())
        assert f"{overlay} extra signals" in everything()

    def test_the_lexicon_counts(self):
        import gzip
        import json
        with gzip.open(ROOT / "data" / "lexicon.json.gz", "rt") as handle:
            data = json.load(handle)
        text = everything()
        for count in (len(data["brands"]), len(data["domains"]),
                      len(data["airports"])):
            assert f"{count:,}" in text, count

    def test_the_topic_count(self):
        words = {12: "twelve", 13: "thirteen", 14: "fourteen"}
        assert words[len(profiles.ALL_TOPICS)] in everything().lower()

    def test_the_test_count(self):
        """The quoted figure is for a checkout that has the private sets.

        Without them a few files collect nothing, so a public clone counts
        fewer and the number in the README would look wrong. It is not: it
        describes the full suite, and the docs say so.
        """
        import private_fixtures
        if any(private_fixtures.path(n) is None for n in private_fixtures.PRIVATE):
            pytest.skip("counts differ without the evaluation sets; "
                        "the quoted figure is for a full checkout")
        # sys.executable, not the checkout's virtualenv: CI installs into
        # the runner's own Python and there is no .venv there at all.
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:randomly",
             "-n", "0", "--collect-only", "-q"],
            cwd=ROOT, capture_output=True, text=True)
        assert out.returncode == 0, (
            "could not collect the suite, so the quoted count is unchecked:\n"
            + out.stderr[-2000:])
        total = sum(int(line.rsplit(": ", 1)[1])
                    for line in out.stdout.splitlines()
                    if re.match(r"^tests/.*: \d+$", line))
        stated = {int(n.replace(",", "")) for n in
                  re.findall(r"(\d[\d,]*) tests\b", everything())}
        assert stated, "no test count is quoted anywhere"
        # Within a handful, so adding one test does not fail the suite.
        assert all(abs(n - total) <= 25 for n in stated), (stated, total)


class TestTheDocsDoNotContradictTheCode:
    def test_the_default_backend_is_the_offline_one(self):
        """The docs promise the default sends nothing anywhere."""
        spec = providers.provider_class(providers.DEFAULT_PROVIDER)
        assert spec.on_device and not spec.needs_api_key

    def test_no_doc_claims_a_cloud_model_is_the_default(self):
        for name in DOCS:
            text = read(name)
            for match in re.finditer(r"[^.\n]*\bdefault\b[^.\n]*", text, re.I):
                sentence = match.group(0)
                if re.search(r"\b(claude|anthropic|gemini|openai)\b", sentence, re.I):
                    # Allowed only when it is naming the default *model* for
                    # a backend somebody has already chosen.
                    assert "default is **Haiku" in sentence or \
                           "Gemini default" in sentence or \
                           "default model" in sentence.lower(), (name, sentence)

    def test_every_category_and_topic_is_documented(self):
        text = everything()
        for topic in profiles.ALL_TOPICS:
            assert topic.label.split(" &")[0] in text, topic

    def test_church_is_documented(self):
        assert OtherCategory.CHURCH in profiles.ALL_TOPICS
        assert "Church" in everything()


class TestEveryLinkResolves:
    def _refs(self, name: str):
        text = read(name)
        out = [r for r in re.findall(r'!\[[^\]]*\]\(([^)]+)\)', text)]
        out += re.findall(r'<img[^>]+src="([^"]+)"', text)
        out += re.findall(r'(?<!!)\[[^\]]*\]\(([^)]+)\)', text)
        return out

    @pytest.mark.parametrize("name", DOCS)
    def test_local_files_exist_and_are_committed(self, name):
        # Via the helper, because a git that refuses to run used to leave
        # this set empty and make every link look uncommitted.
        tracked = set(git_lines("ls-files"))
        for ref in self._refs(name):
            ref = ref.split()[0].strip()
            if ref.startswith(("http", "mailto:", "#")):
                continue
            target = ((ROOT / name).parent / ref.split("#")[0]).resolve()
            rel = str(target.relative_to(ROOT))
            assert target.exists(), f"{name} -> {ref} does not exist"
            assert rel in tracked, f"{name} -> {ref} is not committed"

    @pytest.mark.parametrize("name", DOCS)
    def test_anchors_point_at_real_headings(self, name):
        def slugs(text):
            out = set()
            for line in text.split("\n"):
                found = re.match(r"^#{1,6}\s+(.*?)\s*$", line)
                if found:
                    title = re.sub(r"[`*_]", "", found.group(1))
                    slug = re.sub(r"[^\w\s-]", "", title).strip().lower()
                    out.add(re.sub(r"\s+", "-", slug))
            return out

        here = slugs(read(name))
        for ref in self._refs(name):
            if not ref.startswith("#"):
                continue
            assert ref[1:] in here, f"{name} -> {ref}"


class TestItDoesNotReadLikeAMachineWroteIt:
    def test_no_em_dashes_in_the_prose(self):
        """They are the tell. Sample mail quoted in the docs may keep one."""
        for name in DOCS:
            for number, line in enumerate(read(name).split("\n"), start=1):
                if "—" not in line:
                    continue
                assert "Seat 14C" in line, f"{name}:{number}: {line.strip()[:70]}"


class TestTheLicencesTravelWithTheBinary:
    """Qt is LGPL v3 and the disk image carries twenty of its libraries.

    Clause 4 wants the notice to reach whoever received the program. A file
    on a web page is not that, so both licence files are bundled next to the
    executable, and the app says which licence Qt is under.
    """

    def test_the_spec_bundles_both_licence_files(self):
        spec = (ROOT / "MailManager.spec").read_text(encoding="utf-8")
        assert "THIRD-PARTY-LICENSES.md" in spec
        assert '"LICENSE"' in spec or "'LICENSE'" in spec

    def test_the_notice_names_qt_and_the_lgpl(self):
        notice = (ROOT / "THIRD-PARTY-LICENSES.md").read_text(encoding="utf-8")
        assert "LGPL" in notice
        assert "Qt" in notice
        assert "gnu.org/licenses/lgpl-3.0" in notice
        # Where to get the source of the thing you were given.
        assert "download.qt.io" in notice or "code.qt.io" in notice

    def test_about_says_it_too(self):
        source = (ROOT / "about.py").read_text(encoding="utf-8")
        assert "LGPL" in source, "the window claims MIT and stops there"
        assert "THIRD_PARTY_URL" in source

    def test_the_lexicon_provenance_is_recorded(self):
        notice = (ROOT / "THIRD-PARTY-LICENSES.md").read_text(encoding="utf-8")
        assert "OurAirports" in notice and "Wikidata" in notice


class TestTheWordingStaysPlain:
    """"Audit all help tips and text in the application. Make it less
    verbose, not AI sounding in structure. No em dashes. No 'this isn't
    this, it's this'. No 'x, because y' structured sentences. Users don't
    need every little feature explained with logic behind it."

    Thirty-seven tooltips, labels and dialogs explained themselves at
    length. A tooltip says what a control does; why it works that way
    belongs in the code, where this file's own comments live.

    Only text that reaches a person is checked. The sample inbox is
    pretend mail and the prompts are written for a model, so both keep
    their own voice.
    """

    #: The calls that put words in front of somebody.
    SHOWN = {"setToolTip", "setStatusTip", "setText", "setPlaceholderText",
             "setWindowTitle", "setTitle", "addItem", "setInformativeText",
             "setLabelText"}

    @classmethod
    def _shown_strings(cls):
        """Every literal handed to one of those calls, with where it is."""
        import ast

        found = []
        for path in sorted(ROOT.glob("*.py")):
            if path.name.startswith("test"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = (node.func.attr if isinstance(node.func, ast.Attribute)
                        else getattr(node.func, "id", ""))
                if name not in cls.SHOWN:
                    continue
                parts = [sub.value for sub in ast.walk(node)
                         if isinstance(sub, ast.Constant)
                         and isinstance(sub.value, str)]
                if parts:
                    found.append((path.name, node.lineno, " ".join(parts)))
        return found

    def test_there_is_text_to_check(self):
        """So that a change to how this reads the source cannot quietly
        turn the rest of the class into a test of nothing."""
        assert len(self._shown_strings()) > 200

    def test_nothing_shown_has_an_em_dash_in_it(self):
        bad = [(f, line) for f, line, text in self._shown_strings()
               if "—" in text or "–" in text]
        assert not bad, "em dashes at " + ", ".join(
            f"{f}:{line}" for f, line in bad)

    def test_nothing_shown_explains_itself_with_because(self):
        """"No 'x, because y' structured sentences." """
        import re

        bad = [(f, line, text) for f, line, text in self._shown_strings()
               if re.search(r",\s+(because|so that|since|which is why|so )",
                            text)]
        assert not bad, "\n".join(
            f"{f}:{line}: {text[:90]}" for f, line, text in bad)

    def test_nothing_shown_runs_on(self):
        """A tooltip is a few words. Twenty-five is already generous."""
        bad = [(f, line, len(text.split()))
               for f, line, text in self._shown_strings()
               if len(text.split()) > 25]
        assert not bad, "\n".join(
            f"{f}:{line} is {count} words" for f, line, count in bad)
