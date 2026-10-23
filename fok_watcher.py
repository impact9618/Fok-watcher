#!/usr/bin/env python3
"""
Volgt de Fok-reeks "[Crowdfunding] #...": zoekt zelf het nieuwste topic en meldt
elke NIEUWE externe link (ook in spoilers) via ntfy en/of WhatsApp (CallMeBot).
Afbeeldingen, social media, nieuwssites en Fok zelf worden overgeslagen.
"""
import json
import os
import re
import sys
import urllib.parse

import requests

START_TOPIC_ID = "2768031"  # alleen het beginpunt; daarna loopt het script zelf mee
STATE_VERSION = 3  # bij een andere versie begint het script opnieuw, zonder meldingen
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# Hosts die nooit een melding geven (Fok zelf, advertenties, social, media, nieuws)
IGNORE_HOSTS = (
    "fok.nl", "fokstatic.nl", "fokzine.net", "trickyrock.com", "awin1.com", "bitvavo.com",
    "imgur.com", "giphy.com", "tenor.com", "gyazo.com", "prnt.sc", "postimg.cc",
    "youtube.com", "youtu.be", "twitter.com", "x.com", "facebook.com", "instagram.com",
    "linkedin.com", "reddit.com", "wikipedia.org", "google.com", "google.nl",
    "nu.nl", "nos.nl", "ad.nl", "telegraaf.nl", "rtl.nl", "nrc.nl", "volkskrant.nl",
    "fd.nl", "rtlnieuws.nl", "tweakers.net", "geenstijl.nl", "dumpert.nl",
)
IGNORE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".mp4")

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
CALLMEBOT_PHONE = os.environ.get("CALLMEBOT_PHONE")
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "nl-NL,nl;q=0.9",
    "Referer": "https://forum.fok.nl/",
}
LINK_RE = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")

# Tekst die Fok teruggeeft als een pagina (voor deze aanvraag) niet getoond wordt,
# bv. omdat Fok een geautomatiseerd verzoek weigert. Zo'n pagina komt gewoon als
# "succesvol" terug (HTTP 200), dus zonder deze check zou het script dit missen.
BLOCKED_MARKERS = ("error #52", "Kan dit topic niet weergeven")


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = "iso-8859-15"
    text = r.text
    if any(marker in text for marker in BLOCKED_MARKERS):
        raise RuntimeError(f"Fok gaf een foutpagina terug voor {url} (mogelijk geblokkeerd verzoek)")
    return text


def strip(html_text):
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", html_text)).strip()


def find_latest_topic(topic_html, current_id):
    """Zoek in de forumlijst het crowdfunding-topic met het hoogste #nummer."""
    m = re.search(r'href="((?:https://forum\.fok\.nl)?/forum/\d+[^"]*)"', topic_html)
    if not m:
        return current_id
    forum_url = urllib.parse.urljoin("https://forum.fok.nl/", m.group(1))
    try:
        listing = fetch(forum_url)
    except Exception:
        return current_id
    best = None  # (reeksnummer, topic_id)
    for t in re.finditer(r'/topic/(\d+)/\d+/\d+/-?(\d+)-[^"]*crowdfunding', listing, re.I):
        cand = (int(t.group(2)), t.group(1))
        if best is None or cand > best:
            best = cand
    return best[1] if best else current_id


PAGES_TO_CHECK = 5  # marge voor als een run een keer wat later draait dan gepland


def last_pages(topic_id, first_html):
    nums = [int(n) for n in re.findall(rf"/topic/{topic_id}/(\d+)/25", first_html)]
    last = max(nums) if nums else 1
    start = max(1, last - PAGES_TO_CHECK + 1)
    return list(range(start, last + 1))


def has_access_token(url):
    """Links met ?auth=... (of &auth=...) geven directe pre-investeringstoegang."""
    query = urllib.parse.urlparse(url).query.lower()
    params = urllib.parse.parse_qs(query)
    return any(len(v[0]) > 8 for k, v in params.items() if k in ("auth", "token", "key", "access"))


def find_links(page_html):
    found = {}
    for m in LINK_RE.finditer(page_html):
        href = m.group(1).replace("&amp;", "&")
        if not href.startswith("http"):
            continue
        parsed = urllib.parse.urlparse(href)
        host = parsed.netloc.lower()
        if any(host == h or host.endswith("." + h) for h in IGNORE_HOSTS):
            continue
        if parsed.path.lower().endswith(IGNORE_EXT):
            continue
        before = strip(page_html[max(0, m.start() - 600): m.start()])[-120:]
        found[href] = before
    return found


def notify(url, context):
    priority = "urgent" if has_access_token(url) else "default"
    title = "Fok: PRE-INVESTERINGSLINK" if priority == "urgent" else "Fok crowdfunding link"
    msg = f"Nieuwe link in Fok-topic:\n{url}"
    if context:
        msg += f"\n\nEr voor: ...{context}"
    if NTFY_TOPIC:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=msg.encode("utf-8"),
            headers={"Title": title, "Click": url, "Priority": priority},
            timeout=20,
        )
    if CALLMEBOT_PHONE and CALLMEBOT_APIKEY:
        requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": CALLMEBOT_PHONE, "text": msg, "apikey": CALLMEBOT_APIKEY},
            timeout=20,
        )
    print("Gemeld:", url)


def main():
    state = {"topic": START_TOPIC_ID, "seen": [], "version": 0}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state.update(json.load(f))

    # Nieuwe versie van het script: bestaande links onthouden zonder te melden
    first_run = state.get("version") != STATE_VERSION
    topic_id = state["topic"]
    first_html = fetch(f"https://forum.fok.nl/topic/{topic_id}/1/25/")

    latest = find_latest_topic(first_html, topic_id)
    if latest != topic_id:
        print("Nieuw topic gevonden:", latest)
        topic_id = latest
        first_html = fetch(f"https://forum.fok.nl/topic/{topic_id}/1/25/")

    seen = set(state["seen"])
    new = {}
    for p in last_pages(topic_id, first_html):
        for url, ctx in find_links(fetch(f"https://forum.fok.nl/topic/{topic_id}/{p}/25/")).items():
            if url not in seen:
                new[url] = ctx

    if first_run:
        print(f"Eerste run: {len(new)} bestaande links onthouden, geen meldingen.")
    else:
        for url, ctx in new.items():
            notify(url, ctx)

    seen.update(new)
    with open(STATE_FILE, "w") as f:
        json.dump({"topic": topic_id, "seen": sorted(seen), "version": STATE_VERSION}, f, indent=1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("Fout:", e, file=sys.stderr)
        sys.exit(1)
