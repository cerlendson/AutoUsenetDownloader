import json

import httpx

from app import db, monitor, searcher
from app.newznab import NewznabClient
from app.nzbget import NZBGetClient
from tests.fakes import FakeIndexer, FakeNZBGet, ago


def setup(fresh_db, items_by_indexer, keywords="Beatles\n\"Michael Jackson\"", cats=("audio",)):
    fresh_db.set_settings({"nzbget_host": "nzbget", "nzbget_username": "nzbget", "nzbget_password": "secret"})
    fakes = {}
    with db.connect() as conn:
        for prio, (name, items) in enumerate(items_by_indexer.items(), start=1):
            cur = conn.execute("INSERT INTO indexers(name, api_url, api_key, priority, created_at) VALUES (?,?,?,?,?)",
                               (name, f"https://{name}.example", "good", prio, db.iso(db.utcnow())))
            fakes[cur.lastrowid] = FakeIndexer(items)
        conn.execute("INSERT INTO profiles(name, categories, keywords, created_at) VALUES (?,?,?,?)",
                     ("Music", json.dumps(list(cats)), keywords, db.iso(db.utcnow())))
    nz = FakeNZBGet()

    def ix_factory(ix):
        return NewznabClient(ix["api_url"], ix["api_key"], transport=httpx.MockTransport(fakes[ix["id"]].handler))

    def nz_factory(s):
        return NZBGetClient.from_settings(s, transport=httpx.MockTransport(nz.handler))

    def scan():
        return searcher.run_scan(indexer_factory=ix_factory, nzbget_factory=nz_factory)

    return fakes, nz, scan, nz_factory


ITEMS = [
    {"id": "a1", "title": "The Beatles - Abbey Road FLAC", "cats": [3000, 3040], "date": ago(2)},
    {"id": "a2", "title": "Rare Beatles Collection", "cats": [3010], "date": ago(5)},
    {"id": "a3", "title": "Beatles Discography", "cats": [3040], "date": ago(30)},       # outside 24h window
    {"id": "a4", "title": "Beatles Documentary 1080p", "cats": [2040], "date": ago(1)},  # wrong category
    {"id": "a5", "title": "Michael Jackson - Thriller", "cats": [3040], "date": ago(3)},
    {"id": "a6", "title": "Jackson Family Collection", "cats": [3040], "date": ago(3)},  # phrase mismatch
]


def test_scan_queues_matches_and_never_twice(fresh_db):
    fakes, nz, scan, _ = setup(fresh_db, {"geek": ITEMS})
    stats = scan()
    assert stats.queued == 3, stats
    assert sorted(nz.appended) == sorted(["The Beatles - Abbey Road FLAC.nzb", "Rare Beatles Collection.nzb",
                                          "Michael Jackson - Thriller.nzb"])
    # categories sent to indexer
    ix = next(iter(fakes.values()))
    assert ix.queries[0]["cat"] == ["3000"]
    # second scan: nothing new
    stats2 = scan()
    assert stats2.queued == 0 and stats2.duplicates == 3
    assert len(nz.appended) == 3


def test_same_release_on_two_indexers_grabbed_once_from_highest_priority(fresh_db):
    item = {"id": "z1", "title": "The.Beatles.Abbey.Road.FLAC", "cats": [3040], "date": ago(1), "seg": "same"}
    other = dict(item, id="q9", title="Beatles Abbey Road Remaster FLAC")  # different title + id, same NZB contents
    fakes, nz, scan, _ = setup(fresh_db, {"geek": [item], "finder": [other]}, keywords="Beatles")
    stats = scan()
    assert stats.queued == 1 and stats.duplicates == 1
    with db.connect() as conn:
        rows = conn.execute("SELECT indexer_name FROM downloads").fetchall()
    assert [r[0] for r in rows] == ["geek"]


def test_monitor_tracks_only_own_downloads_and_moves(fresh_db, tmp_path):
    fakes, nz, scan, nz_factory = setup(fresh_db, {"geek": ITEMS[:1]})
    completed_root = tmp_path / "nzbget-completed"
    (completed_root / "The Beatles - Abbey Road FLAC").mkdir(parents=True)
    (completed_root / "The Beatles - Abbey Road FLAC" / "01.flac").write_text("x")
    target = tmp_path / "library"
    fresh_db.set_settings({"move_completed": True, "target_dir": str(target),
                           "nzbget_path_prefix": "/downloads/completed", "local_path_prefix": str(completed_root)})
    scan()
    # a download added to NZBGet by someone else
    nz.queue[999] = {"NZBID": 999, "NZBName": "other", "Status": "DOWNLOADING"}

    with db.connect() as conn:
        row = dict(conn.execute("SELECT * FROM downloads").fetchone())
    nz.queue[row["nzbget_id"]]["Status"] = "DOWNLOADING"
    monitor.run_monitor(nz_factory(db.get_settings()))
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM downloads").fetchone()[0] == "downloading"
        assert conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 1  # 999 never imported

    nz.finish(row["nzbget_id"], dest="/downloads/completed/The Beatles - Abbey Road FLAC")
    monitor.run_monitor(nz_factory(db.get_settings()))
    with db.connect() as conn:
        r = dict(conn.execute("SELECT * FROM downloads").fetchone())
    assert r["status"] == "completed" and r["move_state"] == "done", r
    assert (target / "Music" / "The Beatles - Abbey Road FLAC" / "01.flac").exists()
    assert not (completed_root / "The Beatles - Abbey Road FLAC").exists()


def test_failed_download(fresh_db):
    fakes, nz, scan, nz_factory = setup(fresh_db, {"geek": ITEMS[:1]})
    scan()
    nid = next(iter(nz.queue))
    nz.finish(nid, status="FAILURE/PAR")
    monitor.run_monitor(nz_factory(db.get_settings()))
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM downloads").fetchone()[0] == "failed"
    # a failed release is still "processed" and is not grabbed again
    assert scan().queued == 0


def test_indexer_and_nzbget_test_connection():
    ix = FakeIndexer(ITEMS)
    good = NewznabClient("https://geek.example", "good", transport=httpx.MockTransport(ix.handler))
    assert good.test().startswith("Connected")
    bad = NewznabClient("https://geek.example", "bad", transport=httpx.MockTransport(ix.handler))
    try:
        bad.test()
        raise AssertionError("expected failure")
    except Exception as exc:
        assert "Incorrect user credentials" in str(exc)
    nz = FakeNZBGet()
    ok = NZBGetClient("nzbget", 6789, "nzbget", "secret", transport=httpx.MockTransport(nz.handler))
    assert "24.3" in ok.test()


def test_custom_indexer_category_only_used_on_that_indexer(fresh_db):
    item = {"id": "c1", "title": "Beatles Bootleg", "cats": [100500], "date": ago(1)}
    fakes, nz, scan, _ = setup(fresh_db, {"geek": [item], "finder": [item]}, keywords="Beatles",
                               cats=("ix:1:100500",))
    stats = scan()
    assert stats.queued == 1
    geek, finder = fakes[1], fakes[2]
    assert geek.queries[0]["cat"] == ["100500"]
    assert finder.queries == []  # never searched without a category filter


def test_variant_searches_are_merged_and_queued_once(fresh_db):
    items = [
        {"id": "r1", "title": "TheRollingStones-Sticky.Fingers.FLAC", "cats": [3040], "date": ago(1)},
        {"id": "r2", "title": "The.Rolling.Stones.Hackney.Diamonds", "cats": [3040], "date": ago(1)},
    ]
    fakes, nz, scan, _ = setup(fresh_db, {"geek": items}, keywords='"The Rolling Stones"')
    stats = scan()
    ix = fakes[1]
    assert [q["q"][0] for q in ix.queries] == ["the rolling stones", "therollingstones", "the.rolling.stones"]
    assert stats.queued == 2 and stats.duplicates == 0
    assert len(nz.appended) == 2
