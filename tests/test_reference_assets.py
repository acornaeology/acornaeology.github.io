"""Tests for serving repo-local reference files from the site.

`_localise_reference_assets` copies a reference whose `url` is a
repo-relative path to a real file (a PDF datasheet, an SSD disc image)
into the version's output directory and rewrites the reference `url` to
the absolute site URL, so an otherwise-dead link resolves from every
page. External, site-absolute, fragment/mailto, and non-existent-file
URLs are left untouched.
"""

from __future__ import annotations

from generator.build import BASE_URL, _localise_reference_assets


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_local_file_is_copied_and_url_rewritten(tmp_path):
    repo = tmp_path / "repo"
    _write(repo / "disc" / "foo.ssd", b"SSD-BYTES")
    out = tmp_path / "out" / "myslug"
    refs = [{"label": "Disc", "url": "disc/foo.ssd"}]

    _localise_reference_assets(refs, repo, "myslug", out)

    copied = out / "disc" / "foo.ssd"
    assert copied.read_bytes() == b"SSD-BYTES"
    assert refs[0]["url"] == f"{BASE_URL}myslug/disc/foo.ssd"


def test_external_and_absolute_urls_are_untouched(tmp_path):
    repo = tmp_path / "repo"
    out = tmp_path / "out" / "s"
    refs = [
        {"url": "https://example.com/x.pdf"},
        {"url": "/already/absolute"},
        {"url": "#anchor"},
        {"url": "mailto:a@b.c"},
    ]
    originals = [r["url"] for r in refs]

    _localise_reference_assets(refs, repo, "s", out)

    assert [r["url"] for r in refs] == originals
    assert not out.exists()


def test_missing_file_is_left_as_a_relative_url(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "out" / "s"
    refs = [{"url": "docs/nope.pdf"}]

    _localise_reference_assets(refs, repo, "s", out)

    assert refs[0]["url"] == "docs/nope.pdf"


def test_path_escaping_the_repo_is_refused(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(tmp_path / "outside.bin", b"secret")
    out = tmp_path / "out" / "s"
    refs = [{"url": "../outside.bin"}]

    _localise_reference_assets(refs, repo, "s", out)

    # Not copied, URL unchanged.
    assert refs[0]["url"] == "../outside.bin"
    assert not out.exists()
