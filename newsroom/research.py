"""Read the source behind an inbox item.

- A YouTube link goes to Video Studio as is (it fetches the transcript itself).
- Any other link: the article text is extracted here (paragraphs of the main content) and sent as the job's source text,
  with the headline as title and the site as credit. Video Studio's script writer (DeepSeek) then writes the TLDR with the
  "summary first, punch-up second" rule. Without readable article text, the job is not started (a script from a headline
  alone would invent facts)."""
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0 Safari/537.36")
MIN_WORDS = 120


def is_youtube(url):
    h = (urlparse(url).hostname or "").lower()
    return h in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be")


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "figure", "button"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth_skip = 0; self.in_p = 0; self.in_article = 0
        self.paras, self.article_paras, self.buf = [], [], []
        self.meta, self.title, self._in_title = {}, "", False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            k = (a.get("property") or a.get("name") or "").lower()
            if k in ("og:site_name", "og:title", "og:description", "author", "article:published_time", "description"):
                self.meta[k] = a.get("content") or ""
        if tag == "title": self._in_title = True
        if tag in self.SKIP: self.depth_skip += 1
        if tag == "article": self.in_article += 1
        if tag in ("p", "li", "h2", "h3", "blockquote") and not self.depth_skip:
            self.in_p += 1; self.buf = []

    def handle_endtag(self, tag):
        if tag == "title": self._in_title = False
        if tag in self.SKIP and self.depth_skip: self.depth_skip -= 1
        if tag == "article" and self.in_article: self.in_article -= 1
        if tag in ("p", "li", "h2", "h3", "blockquote") and self.in_p:
            self.in_p -= 1
            t = re.sub(r"\s+", " ", "".join(self.buf)).strip()
            if len(t.split()) >= 8:
                (self.article_paras if self.in_article else self.paras).append(t)

    def handle_data(self, data):
        if self._in_title: self.title += data
        if self.in_p and not self.depth_skip: self.buf.append(data)


def extract(html):
    """-> {"title", "site", "text", "words"} from an article's HTML."""
    p = _Text()
    try: p.feed(html)
    except Exception: pass
    paras = p.article_paras if len(" ".join(p.article_paras).split()) >= MIN_WORDS else p.article_paras + p.paras
    seen, out = set(), []
    for t in paras:
        if t not in seen and not re.search(r"(cookie|subscribe|newsletter|sign up|all rights reserved|advertisement)", t, re.I):
            seen.add(t); out.append(t)
    text = "\n\n".join(out)[:20000]
    return {"title": (p.meta.get("og:title") or p.title).strip(), "site": (p.meta.get("og:site_name") or "").strip(),
            "text": text, "words": len(text.split())}


def fetch(url, cfg=None, log=print):
    """-> {"kind": "youtube"|"article", "url", "title", "site", "text", "words"}. Raises RuntimeError."""
    if is_youtube(url):
        return {"kind": "youtube", "url": url, "title": "", "site": "YouTube", "text": "", "words": 0}
    html, err = "", ""
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=30)
        if r.ok and "html" in r.headers.get("content-type", "html"): html = r.text
        else: err = f"HTTP {r.status_code}"
    except requests.RequestException as e:
        err = str(e)[:150]
    art = extract(html) if html else {"words": 0}
    if art["words"] < MIN_WORDS and cfg is not None:   # JavaScript-built or bot-blocked page: read it in the real browser
        try:
            from . import browser
            with browser.session(cfg) as (context, page):
                page.goto(url, wait_until="domcontentloaded", timeout=60000); page.wait_for_timeout(4000)
                art2 = extract(page.content())
                if art2["words"] > art["words"]: art = art2
        except Exception as e:
            err = (err + "; " if err else "") + f"browser: {str(e).splitlines()[0][:120]}"
    if art.get("words", 0) < MIN_WORDS:
        raise RuntimeError(f"couldn't read enough article text from {urlparse(url).hostname} "
                           f"({art.get('words', 0)} words{'; ' + err if err else ''})")
    host = (urlparse(url).hostname or "").replace("www.", "")
    return {"kind": "article", "url": url, "title": art["title"], "site": art["site"] or host, "text": art["text"], "words": art["words"]}


# ---- the same story from another outlet, when the original blocks robots or is paywalled (Reuters 401, Forbes 403, ...)
_WORD = re.compile(r"[a-z0-9]+")
_STOP = set("the a an and or of to in on for with at by from is are was were be as its it this that new says said how why what will".split())


def _words(t):
    return {w for w in _WORD.findall((t or "").lower()) if len(w) > 2 and w not in _STOP}


def same_story(headline, title, need=0.5):
    """True when at least half of the headline's meaningful words are in the other title."""
    h = _words(headline)
    return bool(h) and len(h & _words(title)) / len(h) >= need


def alternates(headline, skip_host="", limit=5, get=None):
    """-> [(url, title, source)] for the same story elsewhere, from Bing News' RSS (no key, no sign-in)."""
    import html as _html
    from urllib.parse import parse_qs, quote_plus
    get = get or (lambda u: requests.get(u, headers={"User-Agent": UA}, timeout=20).text)
    try: xml = get(f"https://www.bing.com/news/search?q={quote_plus(headline)}&format=rss")
    except Exception: return []
    skip = (skip_host or "").replace("www.", "")
    out = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        title = _html.unescape(re.sub(r"<.*?>", "", (re.search(r"<title>(.*?)</title>", item, re.S) or [None, ""])[1]))
        link = _html.unescape((re.search(r"<link>(.*?)</link>", item, re.S) or [None, ""])[1]).strip()
        q = parse_qs(urlparse(link).query)
        url = (q.get("url") or [link])[0]
        src = _html.unescape((re.search(r"<News:Source>(.*?)</News:Source>", item, re.S) or [None, ""])[1])
        host = (urlparse(url).hostname or "").replace("www.", "")
        if not url.startswith("http") or not host or host == skip or host.endswith("bing.com"): continue
        if not same_story(headline, title): continue
        out.append((url, title, src)); 
        if len(out) >= limit: break
    return out
