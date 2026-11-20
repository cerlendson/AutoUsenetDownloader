from fastapi.testclient import TestClient


def test_create_profile_and_indexer(fresh_db):
    from app.web import app
    with TestClient(app) as c:
        r = c.post("/indexers/save", data={"name": "Geek", "api_url": "https://api.nzbgeek.info", "api_key": "k",
                                           "priority": "1", "enabled": "1", "cat_audio.lossless": "3040, 3045"})
        assert r.status_code == 200 and "saved" in r.text
        r = c.post("/profiles/save", data={"name": "Music", "enabled": "1", "categories": ["audio", "audio.lossless"],
                                           "keywords": 'Beatles\n"Michael Jackson"', "action": "save"})
        assert "Profile &#39;Music&#39; saved" in r.text or "Music" in r.text
        r = c.post("/profiles/save", data={"name": "Music", "keywords": '"Michael Jackson"', "action": "test",
                                           "test_title": "Jackson Family Collection"})
        assert "Doesn" in r.text
        r = c.get("/")
        assert r.status_code == 200 and "Search profiles" in r.text
        r = c.post("/settings", data={"scan_interval_minutes": "15", "search_window_hours": "48"})
        assert fresh_db.get_setting("scan_interval_minutes") == 15
        assert fresh_db.get_setting("search_window_hours") == 48


def test_all_standard_categories_offered_including_xxx_and_custom(fresh_db):
    import json
    from app import db
    from app.web import app
    with db.connect() as conn:
        conn.execute("INSERT INTO indexers(name, api_url, caps_json, created_at) VALUES (?,?,?,?)",
                     ("Geek", "https://x", json.dumps([{"id": "6000", "name": "XXX", "subcats": []},
                                                        {"id": "100500", "name": "Special", "subcats": [
                                                            {"id": "100501", "name": "Bootlegs"}]}]), db.iso(db.utcnow())))
    with TestClient(app) as c:
        html = c.get("/profiles/new").text
        for text in ["XXX &gt; x264", "Books &gt; Foreign", "Movies &gt; WEB-DL", "Special &gt; Bootlegs", 'value="ix:1:100501"']:
            assert text in html, text
        c.post("/profiles/save", data={"name": "P", "enabled": "1", "categories": ["xxx", "ix:1:100501"],
                                       "keywords": "foo", "action": "save"})
        page = c.get("/profiles").text
        assert "XXX" in page and "Geek: Special &gt; Bootlegs (100501)" in page
