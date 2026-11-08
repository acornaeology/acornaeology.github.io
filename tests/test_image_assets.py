"""Tests for serving the repo images that docs and analyses show."""

from generator.build import _localise_images, _publish_repo_file


def _repo(tmp_path):
    repo_dirpath = tmp_path / "repo"
    (repo_dirpath / "docs" / "sprites").mkdir(parents=True)
    (repo_dirpath / "docs" / "sprites" / "sprite_00.png").write_bytes(b"png")
    (repo_dirpath / "art.png").write_bytes(b"art")
    (tmp_path / "outside.png").write_bytes(b"no")
    return repo_dirpath


class TestLocaliseImages:
    def test_relative_image_is_copied_and_repointed(self, tmp_path):
        repo_dirpath = _repo(tmp_path)
        output_dirpath = tmp_path / "out"
        html = _localise_images(
            '<p><img alt="s" src="sprites/sprite_00.png" /></p>',
            repo_dirpath / "docs", repo_dirpath, output_dirpath)
        assert html == '<p><img alt="s" src="docs/sprites/sprite_00.png" /></p>'
        assert (output_dirpath / "docs/sprites/sprite_00.png").read_bytes() == (
            b"png")

    def test_parent_relative_image_within_the_repo(self, tmp_path):
        repo_dirpath = _repo(tmp_path)
        html = _localise_images('<img src="../art.png">', repo_dirpath / "docs",
                                repo_dirpath, tmp_path / "out")
        assert html == '<img src="art.png">'

    def test_untouched_srcs(self, tmp_path):
        repo_dirpath = _repo(tmp_path)
        output_dirpath = tmp_path / "out"
        for src in ("https://example.com/a.png", "/images/logo.png",
                    "data:image/png;base64,AAAA", "../../outside.png",
                    "missing.png"):
            html = f'<img src="{src}">'
            assert _localise_images(html, repo_dirpath / "docs", repo_dirpath,
                                    output_dirpath) == html
        assert not output_dirpath.exists()

    def test_escaped_path(self, tmp_path):
        repo_dirpath = _repo(tmp_path)
        (repo_dirpath / "docs" / "a b.png").write_bytes(b"x")
        html = _localise_images('<img src="a%20b.png">', repo_dirpath / "docs",
                                repo_dirpath, tmp_path / "out")
        assert html == '<img src="docs/a%20b.png">'


class TestPublishRepoFile:
    def test_returns_the_repo_relative_path(self, tmp_path):
        repo_dirpath = _repo(tmp_path)
        assert _publish_repo_file("docs/../art.png", repo_dirpath, repo_dirpath,
                                  tmp_path / "out") == "art.png"
