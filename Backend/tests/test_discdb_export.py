"""TheDiscDB submission export (#741).

Two things are load-bearing here. The zip must match upstream's directory layout
or a contributor has to reorganise it by hand — which is the whole point of the
change. And the MakeMKV log must be redacted, because it names the drive model,
its serial, and the device path, and the export ends up in a public pull request.
"""
import io
import json
import time
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.discdb_export import (
    build_discdb_zip,
    redact_makemkv_log,
    upstream_dir,
)


class TestRedaction:
    """A raw MakeMKV log identifies the drive. A public PR must not."""

    def test_drive_name_serial_and_device_path_are_stripped(self):
        # The serial is part of the hardware name MakeMKV reports, so redacting
        # the field is the only way to remove it.
        line = 'DRV:2,2,999,12,"BD-RE PIONEER BD-RW  BDR-XD06U 1.11 RDDL073394UC","HARRY_POTTER","/dev/sr1"'
        out = redact_makemkv_log(line)
        assert out.strip() == 'DRV:2,2,999,12,"***","***","***"'
        for leaked in ("PIONEER", "RDDL073394UC", "/dev/sr1", "HARRY_POTTER"):
            assert leaked not in out

    def test_empty_drive_slots_are_left_byte_identical(self):
        """Nothing to leak, and rewriting them would differ from upstream's logs."""
        line = 'DRV:5,256,999,0,"","",""'
        assert redact_makemkv_log(line).strip() == line

    def test_log_path_is_stripped(self):
        line = (
            'MSG:1004,131072,1,"Debug logging enabled, log will be saved as '
            '/home/brandon/.MakeMKV/log.txt","Debug logging enabled, log will be saved as %1",'
            '"/home/brandon/.MakeMKV/log.txt"'
        )
        out = redact_makemkv_log(line)
        assert "brandon" not in out
        assert "***" in out

    def test_libredrive_id_is_stripped(self):
        """A drive identifier. Upstream keeps it; we do not — it costs nothing."""
        line = 'MSG:1011,0,1,"Using LibreDrive mode (v02.1 id=DFE22909F92F)","%1","x"'
        assert "DFE22909F92F" not in redact_makemkv_log(line)

    def test_registration_key_is_stripped(self):
        """Belt-and-braces: MakeMKV should never echo it, but if it does, not upstream."""
        line = 'MSG:5021,0,0,"key T-yJKmL0pQrStUvWxYz1234567890abcdef accepted"'
        out = redact_makemkv_log(line)
        assert "yJKmL0pQrStUvWxYz1234567890abcdef" not in out

    def test_home_directory_names_are_stripped(self):
        """Container paths are /data/..., but outside Docker MKVAUTO_ROOT is ~/."""
        line = 'MSG:5014,0,2,"Saving to /home/brandon/MakeMKV-Auto/jobs/x","%1","/home/brandon/x"'
        out = redact_makemkv_log(line)
        assert "brandon" not in out
        # The rest of the path is not sensitive and is useful context.
        assert "MakeMKV-Auto/jobs/x" in out

    @pytest.mark.parametrize("path", [
        "/home/alice/x", "/Users/alice/x", "C:\\Users\\alice\\x",
    ])
    def test_home_directory_stripping_covers_every_platform(self, path):
        assert "alice" not in redact_makemkv_log(f'MSG:1002,0,0,"{path}"')

    def test_content_lines_are_untouched(self):
        """Redaction must not damage the data upstream actually consumes."""
        line = 'MSG:3307,0,2,"File 00129.mpls was added as title #0","File %1 was added as title #%2","00129.mpls","0"'
        assert redact_makemkv_log(line).strip() == line

    def test_trailing_newline_is_preserved(self):
        assert redact_makemkv_log("MSG:1005,0,1,\"x\"\n").endswith("\n")
        assert not redact_makemkv_log("MSG:1005,0,1,\"x\"").endswith("\n")

    def test_multiline_log_redacts_every_drive_line(self):
        raw = "\n".join([
            'MSG:1005,0,1,"MakeMKV v1.18.2 started","%1","MakeMKV v1.18.2"',
            'DRV:0,2,999,12,"BD-RE ASUS BW-16D1HT 3.10 KLAO9CB5258","LABEL","/dev/sr2"',
            'DRV:1,256,999,0,"","",""',
            'MSG:3307,0,2,"File 00129.mpls was added as title #0","%1","00129.mpls"',
        ])
        out = redact_makemkv_log(raw)
        assert "KLAO9CB5258" not in out and "/dev/sr2" not in out
        assert "00129.mpls" in out          # content survives
        assert 'DRV:1,256,999,0,"","",""' in out


class TestUpstreamDir:
    """`data/movie/Cinderella Man (2005)/2025-4k` — placement must be obvious."""

    def test_movie_layout_matches_upstream(self):
        assert upstream_dir("Cinderella Man", 2005, "movie", "2025-4k") == (
            "data/movie/Cinderella Man (2005)/2025-4k"
        )

    @pytest.mark.parametrize("rel_type,expected", [
        ("movie", "movie"), ("series", "series"), ("tv", "series"),
        ("boxset", "sets"), ("collection", "sets"), (None, "movie"),
    ])
    def test_release_type_picks_the_top_level_directory(self, rel_type, expected):
        assert upstream_dir("X", None, rel_type, "s").startswith(f"data/{expected}/")

    def test_year_is_omitted_when_unknown(self):
        assert upstream_dir("Some Show", None, "series", "s1") == "data/series/Some Show/s1"

    def test_path_separators_in_a_title_cannot_escape_the_directory(self):
        """A title is user data and becomes a zip entry name — it must stay one segment."""
        out = upstream_dir("../../etc/passwd", None, "movie", "s")
        assert out.split("/") == ["data", "movie", "..-..-etc-passwd", "s"]


def _bundle(**over):
    base = {
        "schema": "thediscdb-bundle/v1",
        "disc_id": "d1",
        "content_hash": "ABC",
        "disc_number": 1,
        "release_slug": "2025-4k",
        "release_type": "movie",
        "film_title": "Cinderella Man",
        "film_year": 2005,
        "release": {"Slug": "2025-4k", "Title": "Cinderella Man 4K", "Year": 2025,
                    "ImageUrl": None, "BackImageUrl": None,
                    "Contributors": [], "Groups": []},
        "disc": {"Index": 1, "Slug": "4k", "Name": "4K", "ContentHash": "ABC", "Titles": []},
        "summary": "Disc 1 summary text",
        "info_log_included": False,
    }
    base.update(over)
    return base


def _names(zip_bytes):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return zf.namelist()


def _read(zip_bytes, name):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return zf.read(name).decode("utf-8")


class TestZipLayout:
    """The zip must drop into a fork of TheDiscDb/data with no reorganising."""

    def test_emits_upstream_filenames_at_the_upstream_path(self):
        with patch("core.discdb_finalize.generate_discdb_bundle", return_value=_bundle()), \
             patch("core.discdb_export._find_info_log", return_value=None), \
             patch("core.discdb_export._fetch_image", return_value=None):
            name, data = build_discdb_zip("job1", db=None)

        target = "data/movie/Cinderella Man (2005)/2025-4k"
        assert set(_names(data)) == {
            f"{target}/release.json",
            f"{target}/disc01.json",
            f"{target}/disc01-summary.txt",
            "README.txt",
        }
        assert name.endswith(".zip")

    def test_disc_number_drives_the_file_stem(self):
        with patch("core.discdb_finalize.generate_discdb_bundle",
                   return_value=_bundle(disc_number=3)), \
             patch("core.discdb_export._find_info_log", return_value=None), \
             patch("core.discdb_export._fetch_image", return_value=None):
            _, data = build_discdb_zip("job1", db=None)
        assert any(n.endswith("disc03.json") for n in _names(data))
        assert any(n.endswith("disc03-summary.txt") for n in _names(data))

    def test_json_files_are_valid_json(self):
        with patch("core.discdb_finalize.generate_discdb_bundle", return_value=_bundle()), \
             patch("core.discdb_export._find_info_log", return_value=None), \
             patch("core.discdb_export._fetch_image", return_value=None):
            _, data = build_discdb_zip("job1", db=None)
        target = "data/movie/Cinderella Man (2005)/2025-4k"
        assert json.loads(_read(data, f"{target}/release.json"))["Slug"] == "2025-4k"
        assert json.loads(_read(data, f"{target}/disc01.json"))["ContentHash"] == "ABC"

    def test_the_log_is_included_and_redacted(self, tmp_path):
        log = tmp_path / "makemkv_info.log"
        log.write_text('DRV:0,2,999,12,"BD-RE ASUS KLAO9CB5258","L","/dev/sr2"\n')

        with patch("core.discdb_finalize.generate_discdb_bundle", return_value=_bundle()), \
             patch("core.discdb_export._find_info_log", return_value=log), \
             patch("core.discdb_export._fetch_image", return_value=None):
            _, data = build_discdb_zip("job1", db=None)

        target = "data/movie/Cinderella Man (2005)/2025-4k"
        body = _read(data, f"{target}/disc01.txt")
        assert "KLAO9CB5258" not in body and "/dev/sr2" not in body
        assert '"***"' in body

    def test_falls_back_to_the_log_the_scan_persisted(self):
        """The job artifact is written only when a rip had no cached title map,
        and job artifacts are cleaned up — so the disc row is the reliable copy."""
        with patch("core.discdb_finalize.generate_discdb_bundle", return_value=_bundle()), \
             patch("core.discdb_export._find_info_log", return_value=None), \
             patch("core.discdb_export._stored_info_log",
                   return_value='DRV:0,2,999,12,"ASUS KLAO9CB5258","L","/dev/sr2"'), \
             patch("core.discdb_export._fetch_image", return_value=None):
            _, data = build_discdb_zip("job1", db=None)

        body = _read(data, "data/movie/Cinderella Man (2005)/2025-4k/disc01.txt")
        # Still redacted on this path — the stored copy is just as raw.
        assert "KLAO9CB5258" not in body and '"***"' in body

    def test_the_job_artifact_wins_when_both_exist(self, tmp_path):
        """It is contemporaneous with this rip; the disc row is the newest scan."""
        log = tmp_path / "makemkv_info.log"
        log.write_text("MSG:1005,0,1,\"from-artifact\"\n")

        with patch("core.discdb_finalize.generate_discdb_bundle", return_value=_bundle()), \
             patch("core.discdb_export._find_info_log", return_value=log), \
             patch("core.discdb_export._stored_info_log", return_value="from-disc-row"), \
             patch("core.discdb_export._fetch_image", return_value=None):
            _, data = build_discdb_zip("job1", db=None)

        body = _read(data, "data/movie/Cinderella Man (2005)/2025-4k/disc01.txt")
        assert "from-artifact" in body and "from-disc-row" not in body

    def test_an_unreadable_artifact_falls_through_to_the_stored_copy(self, tmp_path):
        missing = tmp_path / "gone.log"   # _find_info_log returned it; it is not readable
        with patch("core.discdb_finalize.generate_discdb_bundle", return_value=_bundle()), \
             patch("core.discdb_export._find_info_log", return_value=missing), \
             patch("core.discdb_export._stored_info_log", return_value="from-disc-row"), \
             patch("core.discdb_export._fetch_image", return_value=None):
            _, data = build_discdb_zip("job1", db=None)

        body = _read(data, "data/movie/Cinderella Man (2005)/2025-4k/disc01.txt")
        assert "from-disc-row" in body

    def test_cover_art_is_included_when_we_have_it(self):
        with patch("core.discdb_finalize.generate_discdb_bundle",
                   return_value=_bundle(release={**_bundle()["release"],
                                                 "ImageUrl": "https://x/f.jpg",
                                                 "BackImageUrl": "https://x/b.jpg"})), \
             patch("core.discdb_export._find_info_log", return_value=None), \
             patch("core.discdb_export._fetch_image", return_value=b"\xff\xd8jpeg"):
            _, data = build_discdb_zip("job1", db=None)
        # Upstream names them exactly front.jpg / back.jpg.
        assert any(n.endswith("front.jpg") for n in _names(data))
        assert any(n.endswith("back.jpg") for n in _names(data))

    def test_a_failed_download_hands_over_the_url(self):
        """Creating a release requires a front cover URL, so we always have one —
        telling someone to go find cover art they already supplied is useless."""
        with patch("core.discdb_finalize.generate_discdb_bundle",
                   return_value=_bundle(release={**_bundle()["release"],
                                                 "ImageUrl": "https://img.example/front.jpg"})), \
             patch("core.discdb_export._find_info_log", return_value=None), \
             patch("core.discdb_export._fetch_image", return_value=None):
            _, data = build_discdb_zip("job1", db=None)
        readme = _read(data, "README.txt")
        assert "https://img.example/front.jpg" in readme
        assert "re-run the export" in readme

    def test_a_release_with_no_cover_url_is_flagged_as_unusual(self):
        with patch("core.discdb_finalize.generate_discdb_bundle", return_value=_bundle()), \
             patch("core.discdb_export._find_info_log", return_value=None), \
             patch("core.discdb_export._fetch_image", return_value=None):
            _, data = build_discdb_zip("job1", db=None)
        assert "which is unusual" in _read(data, "README.txt")

    def test_a_missing_log_is_called_out_rather_than_silently_dropped(self):
        """Otherwise a contributor submits an incomplete entry without knowing."""
        with patch("core.discdb_finalize.generate_discdb_bundle", return_value=_bundle()), \
             patch("core.discdb_export._find_info_log", return_value=None), \
             patch("core.discdb_export._stored_info_log", return_value=None), \
             patch("core.discdb_export._fetch_image", return_value=None):
            _, data = build_discdb_zip("job1", db=None)
        readme = _read(data, "README.txt")
        assert "disc01.txt is missing" in readme
        assert "front.jpg is not included" in readme

    def test_readme_names_the_target_directory(self):
        with patch("core.discdb_finalize.generate_discdb_bundle", return_value=_bundle()), \
             patch("core.discdb_export._find_info_log", return_value=None), \
             patch("core.discdb_export._fetch_image", return_value=None):
            _, data = build_discdb_zip("job1", db=None)
        assert "data/movie/Cinderella Man (2005)/2025-4k" in _read(data, "README.txt")

    def test_a_failed_image_fetch_does_not_fail_the_export(self):
        with patch("core.discdb_finalize.generate_discdb_bundle",
                   return_value=_bundle(release={**_bundle()["release"],
                                                 "ImageUrl": "https://example.com/a.jpg"})), \
             patch("core.discdb_export._find_info_log", return_value=None), \
             patch("requests.get", side_effect=OSError("no network")):
            _, data = build_discdb_zip("job1", db=None)
        assert "front.jpg could not be downloaded" in _read(data, "README.txt")


class TestCoverFetch:
    """A front cover URL is required to create a release, so the image is known
    to exist — the fetch is worth more than one shot, and worth validating."""

    def _resp(self, content=b"\xff\xd8jpeg", ctype="image/jpeg"):
        from unittest.mock import Mock

        r = Mock()
        r.content = content
        r.headers = {"Content-Type": ctype}
        r.raise_for_status = Mock()
        return r

    def test_retries_a_transient_failure(self):
        from core.discdb_export import _fetch_image

        with patch("requests.get", side_effect=[OSError("timeout"), self._resp()]) as get:
            assert _fetch_image("https://x/f.jpg") == b"\xff\xd8jpeg"
        assert get.call_count == 2

    def test_gives_up_after_the_attempt_budget(self):
        from core.discdb_export import _fetch_image

        with patch("requests.get", side_effect=OSError("down")) as get:
            assert _fetch_image("https://x/f.jpg", attempts=3) is None
        assert get.call_count == 3

    def test_a_non_image_200_is_rejected(self):
        """An error page or login redirect must not ship as cover art."""
        from core.discdb_export import _fetch_image

        with patch("requests.get", return_value=self._resp(b"<html>nope</html>", "text/html")):
            assert _fetch_image("https://x/f.jpg") is None

    def test_a_non_image_response_is_not_retried(self):
        """It is a definite answer, not a transient failure."""
        from core.discdb_export import _fetch_image

        with patch("requests.get",
                   return_value=self._resp(b"<html/>", "text/html")) as get:
            _fetch_image("https://x/f.jpg", attempts=3)
        assert get.call_count == 1

    def test_an_empty_body_is_not_written(self):
        from core.discdb_export import _fetch_image

        with patch("requests.get", return_value=self._resp(b"", "image/jpeg")):
            assert _fetch_image("https://x/f.jpg") is None

    @pytest.mark.parametrize("url", [None, "", "ftp://x/f.jpg", "/local/f.jpg"])
    def test_non_http_urls_are_refused_without_a_request(self, url):
        from core.discdb_export import _fetch_image

        with patch("requests.get") as get:
            assert _fetch_image(url) is None
        get.assert_not_called()


class TestUpstreamFields:
    """Fields the old bundle omitted, checked against a real upstream entry."""

    def test_release_json_carries_the_three_missing_fields(self):
        from core.discdb_finalize import _to_release_json

        out = _to_release_json({"release_name": "X", "cover_back_url": "https://b/back.jpg"}, "s")
        assert out["BackImageUrl"] == "https://b/back.jpg"
        assert out["Contributors"] == [] and out["Groups"] == []

    def test_cover_front_url_populates_image_url(self):
        """It was read from a key the disc-backed payload never sets, so it was always null."""
        from core.discdb_finalize import _to_release_json

        out = _to_release_json({"cover_front_url": "https://b/front.jpg"}, "s")
        assert out["ImageUrl"] == "https://b/front.jpg"

    def test_global_disc_id_is_emitted_only_when_known(self):
        """Upstream's field is add-only and immutable — a wrong value is worse than none."""
        from core.discdb_finalize import _to_disc_json

        without = _to_disc_json({}, "hash", 1, "disc01", None)
        assert "GlobalDiscId" not in without

        with_id = _to_disc_json(
            {"global_disc_id": "d2924b73d929f45f2cdff7174688d128cdec3e29"}, "hash", 1, "disc01", None
        )
        assert with_id["GlobalDiscId"] == "D2924B73D929F45F2CDFF7174688D128CDEC3E29"


class TestStoredInfoLog:
    """The DB path itself, not a mock of it — this is where the log really lives."""

    def _disc(self, session, **over):
        from api import models

        disc = models.Disc(content_hash=f"h-{over.pop('tag', 'x')}", **over)
        session.add(disc)
        session.commit()
        session.refresh(disc)
        return disc

    def test_reads_raw_info_log_off_the_disc_row(self, test_db):
        from core.discdb_export import _stored_info_log

        with test_db() as session:
            disc = self._disc(session, tag="raw", disc_info={"raw_info_log": "MSG:1005,0,1,\"x\""})
            assert _stored_info_log(disc.id, session) == 'MSG:1005,0,1,"x"'

    def test_falls_back_to_the_info_log_key(self, test_db):
        """Both keys are written by the scan path; accept either."""
        from core.discdb_export import _stored_info_log

        with test_db() as session:
            disc = self._disc(session, tag="alt", disc_info={"info_log": "DRV:0,2,999,12"})
            assert _stored_info_log(disc.id, session) == "DRV:0,2,999,12"

    def test_joins_a_list_valued_log(self, test_db):
        from core.discdb_export import _stored_info_log

        with test_db() as session:
            disc = self._disc(session, tag="list", disc_info={"raw_info_log": ["a", "b"]})
            assert _stored_info_log(disc.id, session) == "a\nb"

    @pytest.mark.parametrize("info", [None, {}, {"raw_info_log": ""}, {"raw_info_log": "   "}])
    def test_absent_or_blank_log_reads_as_none(self, test_db, info):
        """An empty string must not become a zero-byte disc01.txt in a submission."""
        from core.discdb_export import _stored_info_log

        with test_db() as session:
            disc = self._disc(session, tag="blank", disc_info=info)
            assert _stored_info_log(disc.id, session) is None

    def test_unknown_disc_reads_as_none(self, test_db):
        from core.discdb_export import _stored_info_log

        with test_db() as session:
            assert _stored_info_log("no-such-disc", session) is None


class TestFilmIdentity:
    """Upstream names a directory `{Film Title (Year)}` — the *film's* year, not
    the release's. "Cinderella Man (2005)" sits under release slug "2025-4k"."""

    def _release(self, session, **kw):
        from api import models

        movie = models.Movie(name=kw.pop("movie_name", "Cinderella Man"),
                             production_year=kw.pop("production_year", 2005))
        session.add(movie)
        session.flush()
        boxset = None
        if kw.pop("with_boxset", False):
            boxset = models.Boxset(slug="avp", name="AVP Double Feature", year=2014)
            session.add(boxset)
            session.flush()
        release = models.Release(
            slug="2025-4k", type=kw.pop("type", "movie"), name="Cinderella Man 4K UHD",
            movie_id=movie.id, release_year=2025,
            boxset_id=boxset.id if boxset else None,
        )
        session.add(release)
        session.commit()
        session.refresh(release)
        return release

    def test_uses_the_films_year_not_the_release_year(self, test_db):
        """Release has no production_year column at all, so this used to read
        None and silently fall back to 2025 — the wrong directory upstream."""
        from core.discdb_finalize import _film_identity

        with test_db() as session:
            out = _film_identity(self._release(session), {})
        assert out == {"film_title": "Cinderella Man", "film_year": 2005}

    def test_a_boxset_wins_over_its_movie(self, test_db):
        """Release.movie_id is non-nullable, so a boxset release has a movie too —
        but upstream files it under data/sets/{Set Name (Year)}."""
        from core.discdb_finalize import _film_identity

        with test_db() as session:
            out = _film_identity(self._release(session, type="boxset", with_boxset=True), {})
        assert out == {"film_title": "AVP Double Feature", "film_year": 2014}

    def test_the_edition_name_never_becomes_the_directory(self, test_db):
        """"Cinderella Man 4K UHD" is the release name; upstream keeps edition
        wording in the slug, not the title directory."""
        from core.discdb_finalize import _film_identity

        with test_db() as session:
            assert _film_identity(self._release(session), {})["film_title"] == "Cinderella Man"

    def test_the_full_path_reflects_the_film_year(self, test_db):
        from core.discdb_export import upstream_dir
        from core.discdb_finalize import _film_identity

        with test_db() as session:
            ident = _film_identity(self._release(session), {})
        assert upstream_dir(ident["film_title"], ident["film_year"], "movie", "2025-4k") == (
            "data/movie/Cinderella Man (2005)/2025-4k"
        )


class TestLabelPayloadYear:
    """The UI has always shown the right year; only the export lost it.

    api/routers/discs.py resolves it as "production_year from movie,
    release_year from boxset or release". build_label_payload_from_disc read it
    off Release, which has no such column, so it was None on every release.
    """

    def test_production_year_comes_from_the_movie(self, test_db):
        from api import models
        from core.discdb_finalize import build_label_payload_from_disc

        with test_db() as session:
            movie = models.Movie(name="Cinderella Man", production_year=2005)
            session.add(movie)
            session.flush()
            release = models.Release(slug="2025-4k", type="movie", name="4K UHD",
                                     movie_id=movie.id, release_year=2025)
            session.add(release)
            session.flush()
            disc = models.Disc(content_hash="h-year", release_id=release.id)
            session.add(disc)
            session.commit()
            session.refresh(disc)
            session.refresh(release)

            payload = build_label_payload_from_disc(disc, release)

        assert payload["production_year"] == 2005
        # The release year is a separate thing and must not stand in for it.
        assert payload["release_year"] == 2025


class TestSharedReleaseDirectory:
    """Discs of one release share a directory — that is upstream's layout — so
    the bulk export must not refetch and rewrite the same cover art per disc."""

    def _write(self, zf, disc_number, written):
        from core.discdb_export import _write_disc_entry

        bundle = _bundle(disc_number=disc_number,
                         release={**_bundle()["release"], "ImageUrl": "https://x/f.jpg"})
        return _write_disc_entry(zf, bundle, "job1", None, written)

    def test_cover_art_is_fetched_once_across_sibling_discs(self):
        written = set()
        buf = io.BytesIO()
        with patch("core.discdb_export._find_info_log", return_value=None), \
             patch("core.discdb_export._fetch_image", return_value=b"\xff\xd8") as fetch, \
             zipfile.ZipFile(buf, "w") as zf:
            self._write(zf, 1, written)
            self._write(zf, 2, written)

        # Once for front — the second disc reuses it. (Back has no URL here.)
        assert fetch.call_count == 1

    def test_each_disc_still_gets_its_own_numbered_files(self):
        written = set()
        buf = io.BytesIO()
        with patch("core.discdb_export._find_info_log", return_value=None), \
             patch("core.discdb_export._fetch_image", return_value=None), \
             zipfile.ZipFile(buf, "w") as zf:
            self._write(zf, 1, written)
            self._write(zf, 2, written)

        names = _names(buf.getvalue())
        assert any(n.endswith("disc01.json") for n in names)
        assert any(n.endswith("disc02.json") for n in names)
        # release.json is shared, written once.
        assert len([n for n in names if n.endswith("release.json")]) == 1


class TestExportJobRegistry:
    """Properties that only exist because the export runs in the background."""

    def setup_method(self):
        from core import discdb_export_jobs as jobs

        jobs._jobs.clear()

    def test_a_second_start_joins_the_running_export(self):
        """Two concurrent exports would duplicate every cover-art fetch and race
        each other stamping the same discs as exported."""
        import threading

        from core import discdb_export_jobs as jobs

        release = threading.Event()

        def slow(*a, **kw):
            release.wait(5)
            return "f.zip", None, {"included": 0, "skipped": 0, "disc_ids": [],
                                   "total": 0, "cancelled": False}

        with patch("core.discdb_export.build_discdb_bulk_zip", slow), \
             patch("api.database.SessionLocal"):
            first = jobs.start_export_job()
            second = jobs.start_export_job()
            assert second.id == first.id
            release.set()

    def test_cancelling_marks_the_job_and_is_reported(self):
        import threading

        from core import discdb_export_jobs as jobs

        seen = threading.Event()
        release = threading.Event()

        def slow(db, dest=None, progress=None, should_cancel=None):
            seen.set()
            release.wait(5)
            # The builder checks between discs; report what it would have done.
            return "f.zip", None, {"included": 0, "skipped": 1, "disc_ids": [],
                                   "total": 1, "cancelled": bool(should_cancel())}

        with patch("core.discdb_export.build_discdb_bulk_zip", slow), \
             patch("api.database.SessionLocal"):
            job = jobs.start_export_job()
            seen.wait(5)
            assert jobs.cancel_job(job.id) is True
            release.set()

    def test_cancelling_an_unknown_job_reports_false(self):
        from core import discdb_export_jobs as jobs

        assert jobs.cancel_job("no-such-job") is False

    def test_an_empty_export_fails_rather_than_completing(self):
        """A "completed" job whose archive holds only a README reads as success."""
        from core import discdb_export_jobs as jobs

        with patch("core.discdb_export.build_discdb_bulk_zip",
                   return_value=("f.zip", None, {"included": 0, "skipped": 0,
                                                 "disc_ids": [], "total": 0,
                                                 "cancelled": False})), \
             patch("api.database.SessionLocal"):
            job = jobs.start_export_job()
            for _ in range(200):
                if job.status in ("completed", "failed"):
                    break
                time.sleep(0.01)

        assert job.status == "failed"
        assert "finished job" in (job.error or "")
