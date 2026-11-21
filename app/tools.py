"""Internet tools.

General web search is the fragile one: DuckDuckGo's lite endpoint starts
answering HTTP 202 with a challenge page from a corporate IP once it has seen
a few requests, and Brave/Google cloak their result URLs behind JavaScript.
So the app does not pretend a scrape is a search API. It tries DuckDuckGo,
falls back to the Wikipedia search API, and if neither works it says so
plainly -- the assistant is instructed to repeat that sentence rather than
invent an excuse.

For a medicines assistant the two sources that matter most are also the two
that are stable and need no key: PubMed for literature and openFDA for
labelling. They are separate tools because their answers carry different
weight and the model must say which it used.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse

import httpx

from .config import settings

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

UNAVAILABLE = ("Web search is unavailable from this network right now. Tell the "
               "user that plainly if you needed it; do not guess at the answer.")

_RESULT = re.compile(
    r'href="(//duckduckgo\.com/l/\?uddg=[^"&]+)[^"]*"[^>]*class=[\'"]?result-link[\'"]?>(.*?)</a>',
    re.S,
)
_SNIPPET = re.compile(r"<td[^>]*class=[\'\"]?result-snippet[\'\"]?>(.*?)</td>", re.S)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _get(url: str, **kwargs) -> httpx.Response | None:
    try:
        with httpx.Client(timeout=25.0, trust_env=True, follow_redirects=True,
                          headers={"User-Agent": UA}) as c:
            return c.get(url, **kwargs)
    except httpx.HTTPError:
        return None


# --------------------------------------------------------------------------- #
# General web
# --------------------------------------------------------------------------- #

def _duckduckgo(query: str, limit: int) -> list[dict]:
    r = _get("https://lite.duckduckgo.com/lite/", params={"q": query})
    if r is None or r.status_code != 200:
        return []
    snippets = [_text(s) for s in _SNIPPET.findall(r.text)]
    out: list[dict] = []
    for i, (href, title) in enumerate(_RESULT.findall(r.text)):
        url = urllib.parse.unquote(href.split("uddg=", 1)[-1])
        if not url.startswith("http"):
            continue
        out.append({"title": _text(title)[:200] or url, "url": url,
                    "snippet": snippets[i][:400] if i < len(snippets) else "",
                    "source": "web"})
        if len(out) >= limit:
            break
    return out


def _wikipedia(query: str, limit: int) -> list[dict]:
    def run(term: str) -> list[dict]:
        r = _get("https://en.wikipedia.org/w/api.php", params={
            "action": "query", "list": "search", "srsearch": term,
            "format": "json", "srlimit": limit, "srprop": "snippet",
        })
        if r is None or r.status_code != 200:
            return []
        try:
            hits = r.json()["query"]["search"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return []
        return [{
            "title": h["title"],
            "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(h["title"].replace(" ", "_")),
            "snippet": _text(h.get("snippet", "")),
            "source": "wikipedia",
        } for h in hits[:limit]]

    # MediaWiki treats the query as AND, so a long question matches nothing.
    # Relax to the strongest terms OR'd together before giving up.
    hits = run(query)
    if hits:
        return hits
    terms = [t for t in re.findall(r"[A-Za-z]{3,}", query)][:6]
    relaxed = run(" OR ".join(terms)) if terms else []
    # The relaxed query is deliberately loose, so keep only results that
    # actually mention one of the things asked about.
    key = {t.lower() for t in terms if len(t) >= 5}
    return [h for h in relaxed
            if key & set(re.findall(r"[A-Za-z]{5,}", f"{h['title']} {h['snippet']}".lower()))]


def web_search(query: str, limit: int | None = None) -> list[dict]:
    limit = limit or settings.max_web_results
    if not settings.web_search:
        return []
    return _duckduckgo(query, limit) or _wikipedia(query, limit)


# --------------------------------------------------------------------------- #
# Medical sources
# --------------------------------------------------------------------------- #

def pubmed(query: str, limit: int = 4) -> list[dict]:
    """Recent literature. Titles and journals only - no abstracts, no claims."""
    search = _get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", params={
        "db": "pubmed", "term": query, "retmode": "json", "retmax": limit,
        "sort": "relevance",
    })
    if search is None or search.status_code != 200:
        return []
    try:
        ids = search.json()["esearchresult"]["idlist"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []
    if not ids:
        return []
    summary = _get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi", params={
        "db": "pubmed", "id": ",".join(ids), "retmode": "json",
    })
    if summary is None or summary.status_code != 200:
        return []
    try:
        result = summary.json()["result"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []
    out = []
    for uid in ids:
        item = result.get(uid) or {}
        if not item.get("title"):
            continue
        out.append({
            "title": item["title"].rstrip("."),
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
            "snippet": f"{item.get('source', '')} {item.get('pubdate', '')}".strip(),
            "source": "pubmed",
        })
    return out


def openfda(name: str, limit: int = 3) -> list[dict]:
    """US product labelling for a generic name. Different country: say so."""
    query = f'openfda.generic_name:"{name}"'
    r = _get("https://api.fda.gov/drug/label.json",
             params={"search": query, "limit": limit})
    if r is None or r.status_code != 200:
        return []
    try:
        results = r.json().get("results", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    out = []
    for item in results:
        fda = item.get("openfda", {})
        label = (fda.get("brand_name") or [name])[0]
        dose = _text(" ".join(item.get("dosage_and_administration", [])[:1]))[:500]
        if not dose:
            continue
        out.append({
            "title": f"US label: {label}",
            "url": f"https://dailymed.nlm.nih.gov/dailymed/search.cfm?labeltype=all&query={urllib.parse.quote(name)}",
            "snippet": dose,
            "source": "openfda",
        })
    return out


def fetch_page(url: str, max_chars: int = 6000) -> str:
    if not url.startswith(("http://", "https://")):
        return "refused: only http(s) URLs may be fetched"
    r = _get(url)
    if r is None:
        return "fetch failed: network error"
    if r.status_code != 200:
        return f"fetch failed: HTTP {r.status_code}"
    body = re.sub(r"<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", r.text,
                  flags=re.S | re.I)
    return _text(body)[:max_chars]


def format_results(results: list[dict], start: int = 1) -> str:
    """Number results consistently so the model can cite [W1], [W2]..."""
    if not results:
        return UNAVAILABLE
    lines = []
    for i, r in enumerate(results, start):
        tag = {"pubmed": "PubMed", "openfda": "US FDA label",
               "wikipedia": "Wikipedia", "web": "web"}.get(r.get("source", "web"), "web")
        lines.append(f"[W{i}] ({tag}) {r['title']}\n{r['url']}\n{r['snippet']}")
    return "\n\n".join(lines)
