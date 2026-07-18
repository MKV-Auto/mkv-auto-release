import datetime
import pytest
import uuid
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api import models, database, crud
from parsing.disc_parser import hydrate_disc_payload


RAW_INFO_LOG = """
MSG:3307,0,0,"File 00800.mpls was added as title #1"
TINFO:1,9,0,0,"01:59:00"
TINFO:1,11,0,0,"123456789"
TINFO:1,16,0,0,"00800.mpls"
TINFO:1,27,0,0,"Main Feature"
SINFO:1,0,1,0,"Video"
SINFO:1,0,19,0,"3840x2160"
CINFO:2,0,"Sample Movie"
"""


@pytest.mark.xfail(reason="staging baseline fail; tracked in #398", strict=True)
def test_hydrate_disc_payload_parses_info_log_and_infers_fields():
    payload = hydrate_disc_payload(
        "1",
        "/mnt/disc",
        {
            "disc_hash": "abc123",
            "raw_info_log": RAW_INFO_LOG,
            "release_year": 2024,
        },
    )

    assert payload["disc_num"] == "1"
    assert payload["mount_point"] == "/mnt/disc"
    # inferred metadata
    assert payload["info_title"] == "Sample Movie"
    assert payload["disc_format"] == "UHD"
    assert payload["resolution"] == "2160p"
    # parsed tracks
    scan_tracks = payload.get("scan_tracks") or []
    assert scan_tracks, "expected scan_tracks to be populated from info_log"
    assert scan_tracks[0]["track_id"] == "00800.mpls"
    # titles map preserved as strings
    assert payload.get("titles", {}).get("1", {}).get("file") == "00800.mpls"
    # slug suggestions honor year+format
    assert payload.get("suggested_release_slug") == "2024-4k"
    assert payload.get("disc_name") == "UHD - Sample Movie"
    assert payload.get("disc_slug") == "uhd_-_sample_movie"


def test_hydrate_disc_payload_merges_discdb_into_scan_tracks_preserves_comment():
    """DiscDB hit + tracks should enrich scan_tracks from log without touching MakeMKV comment."""
    payload = hydrate_disc_payload(
        "1",
        "/mnt/disc",
        {
            "disc_hash": "abc123",
            "raw_info_log": RAW_INFO_LOG,
            "discdb_hit": True,
            "tracks": {
                "00800.mpls": {
                    "type": "MainMovie",
                    "title": "DiscDB Name",
                    "description": "Synopsis from DiscDB",
                }
            },
        },
    )
    scan_tracks = payload.get("scan_tracks") or []
    assert scan_tracks
    t0 = scan_tracks[0]
    assert t0.get("track_id") == "00800.mpls" or t0.get("source_file") == "00800.mpls"
    assert t0.get("comment") == "Main Feature"
    assert t0.get("type") == "MainMovie"
    assert t0.get("title") == "DiscDB Name"
    assert t0.get("description") == "Synopsis from DiscDB"


def test_label_flags_normalized():
    payload = hydrate_disc_payload("2", "/mnt/disc2", {"label_required": True})
    assert payload["label_required"] is True
    assert payload["label_ready"] is False  # auto-set when required

    payload2 = hydrate_disc_payload("3", "/mnt/disc3", {})
    assert payload2["label_required"] is False
    assert payload2["label_ready"] is True


def test_apply_scan_tracks_persists_titles_and_streams(tmp_path):
    # Ensure a clean SQLite schema for this test.
    engine = create_engine(f"sqlite:///{tmp_path}/scan.db", future=True)
    from sqlalchemy import event
    @event.listens_for(engine, "connect")
    def _conn(dbapi_conn, connection_record):
        dbapi_conn.create_function("now", 0, lambda: datetime.datetime.now(datetime.UTC).isoformat())
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    database.engine = engine
    database.SessionLocal = SessionLocal
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)

    session = SessionLocal()
    disc = models.Disc(id=str(uuid.uuid4()), content_hash="HASH1", format="UHD")
    session.add(disc)
    session.commit()
    session.refresh(disc)

    scan_tracks = [
        {
            "title_id": "00100.mpls",
            "track_id": "00100.mpls",
            "index": 1,
            "comment": "Main Feature",
            "duration": "00:01:30",
            "size": 1234,
            "display_size": "1.2 MB",
            "segment_map": "1,2,3",
            "chapters_info": "2 chapters",
            "streams": [
                {
                    "type": "Video",
                    "codec_short": "V_MPEGH/ISO/HEVC",
                    "resolution": "3840x2160",
                    "aspect_ratio": "16:9",
                    "duration_seconds": 90,
                },
                {
                    "type": "Audio",
                    "audio_type": "Surround 7.1",
                    "language_code": "eng",
                    "language": "English",
                    "codec_short": "A_DTS",
                    "codec_hint": "DTS-HD MA",
                    "channels": 8,
                    "sample_rate": "48000",
                    "bit_depth": "24",
                    "duration_seconds": 90,
                    "default": True,
                    "layout": "7.1",
                },
            ],
        }
    ]

    crud._apply_scan_tracks(disc, scan_tracks)
    session.commit()

    titles = session.query(models.DiscTitle).all()
    tracks = session.query(models.TitleStream).order_by(models.TitleStream.stream_index).all()

    assert len(titles) == 1
    title = titles[0]
    assert not hasattr(title, "title_id")
    assert title.duration == 90
    assert title.order_index == 0
    assert title.segment_map == "1,2,3"
    assert len(title.streams or []) == 2

    assert len(tracks) == 2
    video, audio = tracks
    assert video.title_id == title.id
    assert video.stream_index == 0
    assert video.stream_type == "Video"
    assert video.codec_short == "V_MPEGH/ISO/HEVC"
    assert video.resolution == "3840x2160"
    assert video.aspect_ratio == "16:9"
    assert video.duration_seconds == 90

    assert audio.title_id == title.id
    assert audio.stream_index == 1
    assert audio.audio_type == "Surround 7.1"
    assert audio.language_code == "eng"
    assert audio.codec_hint == "DTS-HD MA"
    assert audio.channels == 8
    assert audio.sample_rate == "48000"
    assert audio.default is True
    assert audio.layout == "7.1"


def test_apply_scan_tracks_empty_season_episode_strings_become_null(tmp_path):
    """MakeMKV/JSON often sends '' for unknown season/episode; DB columns are Integer — must store NULL."""
    engine = create_engine(f"sqlite:///{tmp_path}/scan_se.db", future=True)
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _conn(dbapi_conn, connection_record):
        dbapi_conn.create_function("now", 0, lambda: datetime.datetime.now(datetime.UTC).isoformat())

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    database.engine = engine
    database.SessionLocal = SessionLocal
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)

    session = SessionLocal()
    disc = models.Disc(id=str(uuid.uuid4()), content_hash="HASH_SE", format="UHD")
    session.add(disc)
    session.commit()
    session.refresh(disc)

    scan_tracks = [
        {
            "track_id": "00800.mpls",
            "source_file": "00800.mpls",
            "index": 1,
            "comment": "Episode",
            "season": "",
            "episode": "",
        }
    ]
    crud._apply_scan_tracks(disc, scan_tracks)
    session.commit()

    title = session.query(models.DiscTitle).one()
    assert title.season is None
    assert title.episode is None


def test_apply_scan_tracks_appends_new_source_files_when_titles_already_exist(tmp_path):
    """Later scan with extra playlists should insert rows for new source_file only."""
    engine = create_engine(f"sqlite:///{tmp_path}/scan_merge.db", future=True)
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _conn(dbapi_conn, connection_record):
        dbapi_conn.create_function("now", 0, lambda: datetime.datetime.now(datetime.UTC).isoformat())

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    database.engine = engine
    database.SessionLocal = SessionLocal
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)

    session = SessionLocal()
    disc = models.Disc(id=str(uuid.uuid4()), content_hash="HASHM", format="UHD")
    session.add(disc)
    session.commit()
    session.refresh(disc)

    first = [
        {
            "source_file": "00100.mpls",
            "index": 0,
            "comment": "A",
            "streams": [{"type": "Video", "codec_short": "V_MPEGH/ISO/HEVC"}],
        }
    ]
    crud._apply_scan_tracks(disc, first)
    session.commit()
    assert session.query(models.DiscTitle).count() == 1

    second = [
        {
            "source_file": "00100.mpls",
            "index": 0,
            "comment": "A",
            "streams": [{"type": "Video", "codec_short": "V_MPEGH/ISO/HEVC"}],
        },
        {
            "source_file": "00200.mpls",
            "index": 1,
            "comment": "B",
            "streams": [{"type": "Video", "codec_short": "V_MPEGH/ISO/HEVC"}],
        },
    ]
    crud._apply_scan_tracks(disc, second)
    session.commit()

    titles = session.query(models.DiscTitle).order_by(models.DiscTitle.order_index).all()
    assert len(titles) == 2
    assert titles[0].source_file == "00100.mpls"
    assert titles[0].comment == "A"
    assert titles[1].source_file == "00200.mpls"
    assert titles[1].comment == "B"
    assert titles[1].order_index == 1


def test_apply_scan_tracks_same_source_file_reconciles_one_row_last_scan_wins(tmp_path):
    """UNIQUE(disc_id, source_file): duplicate scan lines for one file update a single row (last wins)."""
    engine = create_engine(f"sqlite:///{tmp_path}/scan_dup.db", future=True)
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _conn(dbapi_conn, connection_record):
        dbapi_conn.create_function("now", 0, lambda: datetime.datetime.now(datetime.UTC).isoformat())

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    database.engine = engine
    database.SessionLocal = SessionLocal
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)

    session = SessionLocal()
    disc = models.Disc(id=str(uuid.uuid4()), content_hash="HASHDUP", format="BDMV")
    session.add(disc)
    session.commit()
    session.refresh(disc)

    scan_tracks = [
        {
            "track_id": "00928.m2ts",
            "source_file": "00928.m2ts",
            "index": 302,
            "duration": 4652.0,
            "segment_map": "928",
            "streams": [{"type": "Video", "codec_short": "V_MPEG4/ISO/AVC"}],
        },
        {
            "track_id": "00928.m2ts",
            "source_file": "00928.m2ts",
            "index": 303,
            "duration": None,
            "segment_map": None,
            "streams": [],
        },
    ]
    crud._apply_scan_tracks(disc, scan_tracks)
    session.commit()

    titles = session.query(models.DiscTitle).order_by(models.DiscTitle.order_index).all()
    assert len(titles) == 1
    assert titles[0].source_file == "00928.m2ts"
    assert titles[0].index == 303


def test_apply_scan_tracks_reconcile_stale_index_plus_new_title_no_integrity_error(tmp_path):
    """Rescan can move MakeMKV index for an existing file; another title may take the old index."""
    engine = create_engine(f"sqlite:///{tmp_path}/scan_stale.db", future=True)
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _conn(dbapi_conn, connection_record):
        dbapi_conn.create_function("now", 0, lambda: datetime.datetime.now(datetime.UTC).isoformat())

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    database.engine = engine
    database.SessionLocal = SessionLocal
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)

    session = SessionLocal()
    disc = models.Disc(id=str(uuid.uuid4()), content_hash="HASHSTALE", format="UHD")
    session.add(disc)
    session.commit()
    session.refresh(disc)

    crud._apply_scan_tracks(
        disc,
        [
            {
                "source_file": "A.mpls",
                "index": 5,
                "comment": "old",
                "streams": [{"type": "Video", "codec_short": "V_MPEGH/ISO/HEVC"}],
            },
        ],
    )
    session.commit()

    crud._apply_scan_tracks(
        disc,
        [
            {
                "source_file": "A.mpls",
                "index": 10,
                "comment": "renumbered",
                "streams": [{"type": "Video", "codec_short": "V_MPEGH/ISO/HEVC"}],
            },
            {
                "source_file": "B.mpls",
                "index": 5,
                "comment": "new",
                "streams": [{"type": "Video", "codec_short": "V_MPEGH/ISO/HEVC"}],
            },
        ],
    )
    session.commit()

    by_sf = {t.source_file: t for t in session.query(models.DiscTitle).all()}
    assert len(by_sf) == 2
    assert by_sf["A.mpls"].index == 10
    assert by_sf["B.mpls"].index == 5


def test_parse_title_metadata_accepts_three_field_tinfo():
    alt_log = """
MSG:3307,0,0,"File 00101.mpls was added as title #0"
TINFO:0,9,0,"01:30:00"
TINFO:0,11,0,"987654321"
TINFO:0,16,0,"00101.mpls"
TINFO:0,27,0,"SampleTitle.mkv"
TINFO:0,27,0,"Main Feature"
TINFO:0,26,0,"5"
SINFO:0,0,1,"Video"
SINFO:0,0,19,"3840x2160"
SINFO:0,0,21,"23.976 (24000/1001)"
"""
    payload = hydrate_disc_payload("1", "/mnt/disc", {"raw_info_log": alt_log, "disc_hash": "abc"})
    scan_tracks = payload.get("scan_tracks") or []
    assert scan_tracks, "scan_tracks should be parsed from three-field TINFO logs"
    t = scan_tracks[0]
    assert t.get("duration") == pytest.approx(5400.0)
    assert t.get("duration_raw") == "01:30:00"
    assert t.get("size") == 987654321
    assert t.get("comment") == "Main Feature"
    assert t.get("segment_map") == "5"
    assert t.get("streams")[0].get("frame_rate") == "23.976 (24000/1001)"
