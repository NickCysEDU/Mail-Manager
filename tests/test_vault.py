"""Encryption at rest for the two files that describe somebody's mail.

The threat this closes is not somebody holding the disk: FileVault and mode
0600 already cover that. It is any other program running as the same user,
which can read a file in your home directory and cannot read a Keychain item
belonging to this app without the system asking first.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import vault
from config import InMemoryCredentialStore


@pytest.fixture
def sealed(tmp_path, monkeypatch):
    """A vault with its own in-memory Keychain, touching nothing real."""
    monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
    box = vault.Vault(store=InMemoryCredentialStore())
    monkeypatch.setattr(vault, "_SHARED", box)
    return box


@pytest.fixture
def keyless(tmp_path, monkeypatch):
    """A vault that cannot reach a key, however it failed."""
    monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
    box = vault.Vault(store=None)
    box._looked = True
    box._key = None
    monkeypatch.setattr(vault, "_SHARED", box)
    return box


class TestItActuallyEncrypts:
    def test_the_plaintext_is_not_in_the_file(self, sealed, tmp_path):
        path = tmp_path / "verdicts.json"
        sealed.write(path, {"summary": "A recruiter turned you down",
                            "sender": "someone@a-company.example"})
        raw = path.read_bytes()
        for secret in (b"recruiter", b"turned you down", b"a-company"):
            assert secret.lower() not in raw.lower(), secret

    def test_it_is_marked_as_sealed(self, sealed, tmp_path):
        path = tmp_path / "verdicts.json"
        sealed.write(path, {"a": 1})
        assert path.read_bytes().startswith(vault.MAGIC)

    def test_it_reads_back_exactly(self, sealed, tmp_path):
        path = tmp_path / "verdicts.json"
        document = {"version": 1, "rows": [{"x": "é 日本", "n": 4.5}]}
        sealed.write(path, document)
        assert sealed.read(path) == document

    def test_every_write_uses_a_fresh_nonce(self, sealed, tmp_path):
        """Reusing one with the same key is how AES-GCM is broken."""
        path = tmp_path / "verdicts.json"
        seen = set()
        for _ in range(12):
            sealed.write(path, {"same": "document"})
            raw = path.read_bytes()
            seen.add(raw[len(vault.MAGIC):len(vault.MAGIC) + vault.NONCE_BYTES])
        assert len(seen) == 12

    def test_the_file_is_owner_only(self, sealed, tmp_path):
        path = tmp_path / "verdicts.json"
        sealed.write(path, {"a": 1})
        assert oct(path.stat().st_mode)[-3:] == "600"

    def test_the_key_is_kept_in_the_keychain(self, sealed):
        assert sealed.key() is not None
        assert len(sealed.key()) == vault.KEY_BYTES
        stored = sealed._credential_store().get(vault.KEY_ACCOUNT)
        assert stored, "the key should be in the Keychain, not on disk"

    def test_the_same_key_is_reused(self, sealed):
        assert sealed.key() == sealed.key()


class TestTampering:
    def test_a_flipped_byte_is_refused(self, sealed, tmp_path):
        path = tmp_path / "verdicts.json"
        sealed.write(path, {"secret": "value"})
        raw = bytearray(path.read_bytes())
        raw[-1] ^= 0x01
        path.write_bytes(bytes(raw))
        assert sealed.read(path) is None

    def test_a_file_renamed_over_another_is_refused(self, sealed, tmp_path):
        """The filename is bound in, so one cannot be swapped for the other."""
        verdicts = tmp_path / "verdicts.json"
        corrections = tmp_path / "corrections.json"
        sealed.write(verdicts, {"which": "verdicts"})
        corrections.write_bytes(verdicts.read_bytes())
        assert sealed.read(corrections) is None

    def test_a_truncated_file_is_refused(self, sealed, tmp_path):
        path = tmp_path / "verdicts.json"
        sealed.write(path, {"a": 1})
        raw = path.read_bytes()
        path.write_bytes(raw[:len(raw) // 2])
        assert sealed.read(path) is None

    def test_a_wrong_key_cannot_read_it(self, sealed, tmp_path):
        path = tmp_path / "verdicts.json"
        sealed.write(path, {"secret": "value"})
        other = vault.Vault(store=InMemoryCredentialStore())
        assert other.key() != sealed.key()
        assert other.read(path) is None

    def test_nothing_here_raises(self, sealed, tmp_path):
        for content in (b"", b"not json", vault.MAGIC, vault.MAGIC + b"short",
                        b"\xff\xfe\x00binary"):
            path = tmp_path / "verdicts.json"
            path.write_bytes(content)
            assert sealed.read(path) is None


class TestWhenItCannotEncrypt:
    def test_it_says_so(self, keyless):
        assert keyless.sealing is False
        assert "Not encrypted" in keyless.describe()

    def test_it_still_writes_something(self, keyless, tmp_path):
        """A cache that cannot be written is a feature that stops working."""
        path = tmp_path / "verdicts.json"
        assert keyless.write(path, {"a": 1}) is True
        assert keyless.read(path) == {"a": 1}

    def test_the_summary_is_left_out_instead(self, keyless, tmp_path):
        """The promise holds even when the cipher does not."""
        import verdict_cache
        from models import Category, Classification, EmailMessage, OtherCategory

        message = EmailMessage(uid="1", account_id="a", source_folder="INBOX")
        verdict = Classification(
            summary="A recruiter turned you down for the Platform role",
            is_job_related=True, category=Category.NOT_INTERESTED,
            other_category=OtherCategory.NOT_APPLICABLE,
            confidence_score=0.97,
            reasoning="It says they moved forward with other candidates",
            model="test")
        cache = verdict_cache.VerdictCache(path=tmp_path / "verdicts.json")
        cache.put(message, "recipe", verdict)
        cache.save()

        raw = (tmp_path / "verdicts.json").read_bytes().lower()
        assert b"recruiter" not in raw
        assert b"other candidates" not in raw
        # The useful half survives, so the scan is still skipped.
        assert b"not_interested" in raw

    def test_the_verdict_still_comes_back(self, keyless, tmp_path):
        import verdict_cache
        from models import Category, Classification, EmailMessage, OtherCategory

        message = EmailMessage(uid="1", account_id="a", source_folder="INBOX")
        verdict = Classification(
            summary="private", is_job_related=True,
            category=Category.INTERVIEW,
            other_category=OtherCategory.NOT_APPLICABLE,
            confidence_score=0.97, reasoning="private", model="test")
        path = tmp_path / "verdicts.json"
        cache = verdict_cache.VerdictCache(path=path)
        cache.put(message, "recipe", verdict)
        cache.save()

        again = verdict_cache.VerdictCache.load(path)
        hit = again.get(message, "recipe")
        assert hit is not None
        assert hit.category is Category.INTERVIEW
        assert hit.confidence_score == pytest.approx(0.97)


class TestMigration:
    def test_a_plaintext_file_from_an_older_build_still_opens(self, sealed, tmp_path):
        path = tmp_path / "verdicts.json"
        path.write_text(json.dumps({"version": 1, "verdicts": []}))
        assert sealed.read(path) == {"version": 1, "verdicts": []}

    def test_and_is_sealed_the_next_time_it_is_written(self, sealed, tmp_path):
        path = tmp_path / "corrections.json"
        path.write_text(json.dumps({"version": 1, "corrections": []}))
        assert not path.read_bytes().startswith(vault.MAGIC)
        sealed.write(path, sealed.read(path))
        assert path.read_bytes().startswith(vault.MAGIC)

    def test_the_real_corrections_file_round_trips(self, sealed, tmp_path):
        import corrections
        path = tmp_path / "corrections.json"
        memory = corrections.Memory(path=path)
        memory.remember_move("jane@acme.example", "Job Search/Interviews")
        memory.save()
        assert path.read_bytes().startswith(vault.MAGIC)
        assert len(corrections.Memory.load(path)) == 1
