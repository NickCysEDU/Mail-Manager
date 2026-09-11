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
        # sys.executable, not the checkout's virtualenv: CI installs into
        # the runner's own Python and there is no .venv there at all.
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:randomly",
             "-n", "0", "--collect-only", "-q"],
            cwd=ROOT, capture_output=True, text=True)
        if out.returncode != 0:
            pytest.skip("could not collect the suite")
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
        tracked = set(subprocess.run(["git", "ls-files"], cwd=ROOT,
                                     capture_output=True, text=True).stdout.split())
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
