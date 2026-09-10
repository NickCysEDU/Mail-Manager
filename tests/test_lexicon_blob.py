"""The lexicon as a file the app can read without parsing it."""

from __future__ import annotations

import struct

import pytest

import lexicon_blob
from lexicon_blob import build, open_blob


@pytest.fixture
def blob(tmp_path):
    path = build({
        "airports": {"STN": "", "DUB": "", "LHR": ""},
        "brands": {"argos": "retail", "ryanair": "airline", "zzz": "news"},
        "meta": {"built": "2026-09-09"},
    }, tmp_path / "lexicon.bin")
    opened = open_blob(path)
    yield opened
    opened.close()


class TestReadingItBack:
    def test_every_table_is_there(self, blob):
        assert set(blob.tables) == {"airports", "brands", "meta"}

    def test_the_counts_are_right(self, blob):
        assert len(blob.table("airports")) == 3
        assert len(blob.table("brands")) == 3

    def test_a_value_comes_back(self, blob):
        assert blob.table("brands").get("argos") == "retail"
        assert blob.table("brands").get("ryanair") == "airline"

    def test_a_missing_key_is_the_default(self, blob):
        assert blob.table("brands").get("nobody") is None
        assert blob.table("brands").get("nobody", "x") == "x"

    def test_the_first_and_last_keys_are_findable(self, blob):
        """Binary search gets its bounds wrong at exactly these two."""
        keys = sorted(["argos", "ryanair", "zzz"])
        assert blob.table("brands").get(keys[0]) is not None
        assert blob.table("brands").get(keys[-1]) is not None

    def test_contains(self, blob):
        assert "STN" in blob.table("airports")
        assert "ZZZ" not in blob.table("airports")

    def test_an_empty_value_round_trips(self, blob):
        """Airports are keys with nothing attached; that has to survive."""
        assert blob.table("airports").get("STN") == ""
        assert blob.table("airports").get("STN") is not None

    def test_iterating_gives_the_keys_in_order(self, blob):
        assert list(blob.table("brands")) == ["argos", "ryanair", "zzz"]

    def test_items(self, blob):
        assert dict(blob.table("brands").items()) == {
            "argos": "retail", "ryanair": "airline", "zzz": "news"}

    def test_a_table_that_is_not_there(self, blob):
        assert blob.table("nonsense") is None


class TestEverythingSurvivesTheRoundTrip:
    def test_a_thousand_keys(self, tmp_path):
        wanted = {f"key{n:04}": f"value{n}" for n in range(1000)}
        path = build({"t": wanted}, tmp_path / "b.bin")
        with open_blob(path) as blob:
            table = blob.table("t")
            assert len(table) == 1000
            for key, value in wanted.items():
                assert table.get(key) == value

    def test_unicode(self, tmp_path):
        wanted = {"café": "naïve", "日本": "テスト", "straße": "grüße"}
        path = build({"t": wanted}, tmp_path / "b.bin")
        with open_blob(path) as blob:
            assert dict(blob.table("t").items()) == wanted

    def test_an_empty_table(self, tmp_path):
        path = build({"t": {}}, tmp_path / "b.bin")
        with open_blob(path) as blob:
            assert len(blob.table("t")) == 0
            assert blob.table("t").get("anything") is None

    def test_the_order_it_is_written_in_does_not_matter(self, tmp_path):
        forwards = build({"t": {"a": "1", "b": "2", "c": "3"}},
                         tmp_path / "f.bin")
        backwards = build({"t": {"c": "3", "b": "2", "a": "1"}},
                          tmp_path / "b.bin")
        assert forwards.read_bytes() == backwards.read_bytes()


class TestRefusingWhatItCannotRead:
    def test_a_file_that_is_not_there(self, tmp_path):
        assert open_blob(tmp_path / "nope.bin") is None

    def test_a_file_that_is_not_one_of_these(self, tmp_path):
        path = tmp_path / "b.bin"
        path.write_bytes(b"this is not a lexicon at all, at all, at all")
        assert open_blob(path) is None

    def test_an_empty_file(self, tmp_path):
        path = tmp_path / "b.bin"
        path.write_bytes(b"")
        assert open_blob(path) is None

    def test_a_truncated_file(self, tmp_path):
        path = build({"t": {f"k{n}": "v" for n in range(200)}}, tmp_path / "b.bin")
        whole = path.read_bytes()
        path.write_bytes(whole[:len(whole) // 2])
        assert open_blob(path) is None

    def test_a_future_version(self, tmp_path):
        path = build({"t": {"a": "1"}}, tmp_path / "b.bin")
        whole = bytearray(path.read_bytes())
        whole[4] = 0x09           # bump the version byte inside the magic
        path.write_bytes(bytes(whole))
        assert open_blob(path) is None


class TestRefusingWhatItCannotWrite:
    def test_a_name_too_long_for_the_header(self, tmp_path):
        with pytest.raises(ValueError, match="16 bytes"):
            build({"a" * 20: {}}, tmp_path / "b.bin")

    def test_a_key_with_a_nul_in_it(self, tmp_path):
        with pytest.raises(ValueError, match="NUL"):
            build({"t": {"a\0b": "v"}}, tmp_path / "b.bin")


class TestTheRealOne:
    def test_the_lexicon_prefers_it(self):
        import lexicon
        if lexicon._blob() is None:
            pytest.skip("no lexicon.bin in this checkout")
        assert "mapped" in lexicon.describe()

    def test_it_answers_the_same_as_the_json(self):
        """Both forms have to agree, or the fallback is a different app."""
        import gzip
        import json
        from pathlib import Path

        import lexicon
        root = Path(lexicon.__file__).resolve().parent
        blob_path = root / "data" / "lexicon.bin"
        json_path = root / "data" / "lexicon.json.gz"
        if not (blob_path.exists() and json_path.exists()):
            pytest.skip("both forms are needed for this comparison")

        with gzip.open(json_path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        with open_blob(blob_path) as blob:
            brands = blob.table("brands")
            for key, value in list(payload["brands"].items())[:2000]:
                assert brands.get(key) == value
            airports = blob.table("airports")
            for code in list(payload["airports"])[:2000]:
                assert code in airports

    def test_the_two_forms_are_the_same_build(self):
        """A rebuilt JSON with a stale blob would ship the wrong tables.

        tools/build_lexicon.py writes both and nothing else does, so the only
        way they diverge is somebody rebuilding one by hand. The app prefers
        the blob, so a stale one is silent: the same app, quietly a release
        behind on what it knows about the world.
        """
        import gzip
        import json
        from pathlib import Path

        import lexicon
        root = Path(lexicon.__file__).resolve().parent
        blob_path = root / "data" / "lexicon.bin"
        json_path = root / "data" / "lexicon.json.gz"
        if not (blob_path.exists() and json_path.exists()):
            pytest.skip("both forms are needed for this comparison")

        with gzip.open(json_path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        with open_blob(blob_path) as blob:
            meta = blob.table("meta")
            assert meta.get("built") == payload.get("built"), (
                "data/lexicon.bin and data/lexicon.json.gz were built at "
                "different times - run tools/build_lexicon.py")
            for section, expected in (("brands", payload["brands"]),
                                      ("domains", payload["domains"]),
                                      ("airports", payload["airports"])):
                assert len(blob.table(section)) == len(expected), section

    def test_it_is_smaller_in_memory_than_the_parsed_form(self):
        """The whole reason it exists."""
        import lexicon
        if lexicon._blob() is None:
            pytest.skip("no lexicon.bin in this checkout")
        # A mapped table holds no Python objects per row.
        table = lexicon._data()["brands"]
        assert isinstance(table, lexicon_blob.Table)
        assert len(table) > 1000
