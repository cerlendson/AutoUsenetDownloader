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
