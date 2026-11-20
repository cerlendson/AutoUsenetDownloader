"""Fake Newznab indexer and NZBGet server built on httpx.MockTransport."""
import base64
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.parse import parse_qs, urlparse

import httpx

NZB = """<?xml version="1.0"?><nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
<file subject="{title}"><segments><segment bytes="1" number="1">{seg}@news</segment></segments></file></nzb>"""


def rss(items):
    out = ['<?xml version="1.0"?><rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel>']
    for it in items:
        cats = "".join(f'<newznab:attr name="category" value="{c}"/>' for c in it["cats"])
        out.append(
            f"<item><title>{it['title']}</title><guid isPermaLink=\"true\">https://idx.example/details/{it['id']}</guid>"
            f"<link>https://idx.example/getnzb/{it['id']}.nzb?r=KEY</link>"
            f"<pubDate>{format_datetime(it['date'])}</pubDate>"
            f"<enclosure url=\"https://idx.example/getnzb/{it['id']}.nzb?r=KEY\" length=\"1000\" type=\"application/x-nzb\"/>"
            f"{cats}<newznab:attr name=\"size\" value=\"123456789\"/></item>")
    out.append("</channel></rss>")
    return "".join(out)


class FakeIndexer:
    def __init__(self, items, segment_prefix="seg"):
        self.items = items
        self.segment_prefix = segment_prefix
        self.queries = []
        self.grabs = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if "/getnzb/" in url.path:
            nid = url.path.rsplit("/", 1)[-1].removesuffix(".nzb")
            it = next(i for i in self.items if i["id"] == nid)
            self.grabs.append(nid)
            return httpx.Response(200, text=NZB.format(title=it["title"], seg=it.get("seg", f"{self.segment_prefix}-{nid}")))
        q = parse_qs(urlparse(str(url)).query)
        if q.get("apikey") != ["good"]:
            return httpx.Response(200, text='<error code="100" description="Incorrect user credentials"/>')
        if q["t"] == ["caps"]:
            return httpx.Response(200, text='<caps><categories><category id="3000" name="Audio"><subcat id="3040" name="Lossless"/></category></categories></caps>')
        self.queries.append(q)
        words = (q.get("q") or [""])[0].lower().split()
        res = [i for i in self.items if all(w in i["title"].lower() for w in words)]
        return httpx.Response(200, text=rss(res))


class FakeNZBGet:
    def __init__(self):
        self.next_id = 100
        self.queue = {}
        self.history = {}
        self.appended = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Basic " + base64.b64encode(b"nzbget:secret").decode():
            return httpx.Response(401)
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        if method == "version":
            result = "24.3"
        elif method == "status":
            result = {"DownloadPaused": False}
        elif method == "append":
            self.next_id += 1
            self.appended.append(params[0])
            self.queue[self.next_id] = {"NZBID": self.next_id, "NZBName": params[0], "Status": "QUEUED"}
            result = self.next_id
        elif method == "listgroups":
            result = list(self.queue.values())
        elif method == "history":
            result = list(self.history.values())
        else:
            return httpx.Response(200, json={"error": {"message": "unknown method"}})
        return httpx.Response(200, json={"result": result})

    def finish(self, nzb_id, status="SUCCESS/ALL", dest="/downloads/completed/x"):
        self.queue.pop(nzb_id)
        self.history[nzb_id] = {"NZBID": nzb_id, "Status": status, "DestDir": dest, "FinalDir": "", "Kind": "NZB"}


def ago(hours):
    return datetime.now(timezone.utc) - timedelta(hours=hours)
