#!/usr/bin/env python3
"""
Volgt de Fok-reeks "[Crowdfunding] #...": zoekt zelf het nieuwste topic en meldt
nieuwe pre-investeringslinks (ook in spoilers) via ntfy en/of WhatsApp (CallMeBot).
"""
import json
import os
import re
import sys
import urllib.parse

import requests

START_TOPIC_ID = "2768031"  # alleen het beginpunt; daarna loopt het script zelf mee
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# Een link telt als pre-investeringslink als de URL, de linktekst of de ~250
# tekens ervoor dit patroon bevatten.
PATTERN = re.compile(r"pre[-\s]?invest|pre[-\s]?inschrijv|voor[-\s]?inschrijv", re.I)

IGNORE_HOSTS = ("fok.nl", "fokstatic.nl", "fokzine.net", "trickyrock.com", "awin1.com", "bitvavo.com")

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
CALLMEBOT_PHONE = os.environ.get("CALLMEBOT_PHONE")
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY")

HEADERS = {"User-Agent": "Mozilla/5.0 (persoonlijke topic-watcher)"}
LINK_RE = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = "iso-8859-15"
    return r.text


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


def last_pages(topic_id, first_html):
    nums = [int(n) for n in re.findall(rf"/topic/{topic_id}/(\d+)/25", first_html)]
    last = max(nums) if nums else 1
    return [last] if last == 1 else [last - 1, last]


def find_links(page_html):
    found = {}
    for m in LINK_RE.finditer(page_html):
        href, anchor_html = m.group(1), m.group(2)
        href = href.replace("&amp;", "&")
        if not href.startswith("http"):
            continue
        host = urllib.parse.urlparse(href).netloc.lower()
        if any(host == h or host.endswith("." + h) for h in IGNORE_HOSTS):
            continue
        before = strip(page_html[max(0, m.start() - 600): m.start()])[-250:]
        if PATTERN.search(f"{href} {strip(anchor_html)} {before}"):
            found[href] = before
    return found


def notify(url):
    msg = f"Nieuwe pre-investeringslink in Fok-topic:\n{url}"
    if NTFY_TOPIC:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=msg.encode("utf-8"),
            headers={"Title": "Fok pre-investering", "Click": url, "Priority": "high"},
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
    first_run = not os.path.exists(STATE_FILE)
    state = {"topic": START_TOPIC_ID, "seen": []}
    if not first_run:
        with open(STATE_FILE) as f:
            state.update(json.load(f))

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
        for url in new:
            notify(url)

    seen.update(new)
    with open(STATE_FILE, "w") as f:
        json.dump({"topic": topic_id, "seen": sorted(seen)}, f, indent=1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("Fout:", e, file=sys.stderr)
        sys.exit(1)
