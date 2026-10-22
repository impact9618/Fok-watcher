#!/usr/bin/env python3
"""
Volgt Marktplaats-zoekopdrachten "iPhone" en "iPad" en meldt een nieuwe advertentie
met "Direct kopen" die opvallend goedkoop is t.o.v. vergelijkbare advertenties
(zelfde model + opslaggrootte).

LET OP: dit script is geschreven zonder de live Marktplaats-pagina te kunnen
bekijken (alleen op basis van bekende opbouw van zulke sites). Het kan zijn dat
de eerste run meteen een fout geeft omdat Marktplaats zijn pagina anders heeft
opgebouwd dan verwacht. Bekijk in dat geval de foutmelding in de Actions-log.
"""
import json
import os
import re
import statistics
import sys

import requests

SEARCH_QUERIES = ["iphone", "ipad"]
DROP_THRESHOLD = float(os.environ.get("PRICE_THRESHOLD_PCT", "30")) / 100  # 30% onder mediaan
MIN_SAMPLES = 5          # pas vergelijken als een model al minstens dit vaak is gezien
MAX_HISTORY_PER_GROUP = 60  # oude prijzen per model laten vervallen

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "marktplaats_state.json")

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
CALLMEBOT_PHONE = os.environ.get("CALLMEBOT_PHONE")
CALLMEBOT_APIKEY = os.environ.get("CALLMEBOT_APIKEY")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "nl-NL,nl;q=0.9",
}

MODEL_RE = re.compile(
    r"i(?:phone|pad)\s*"
    r"(\d+|pro\s*max|pro|air|mini|se)?\s*"
    r"(pro\s*max|pro|air|mini)?\s*"
    r"(\d{2,4})\s*gb",
    re.I,
)

# Woorden die op schade/defect wijzen: zo'n advertentie telt niet mee en wordt nooit gemeld.
DAMAGE_RE = re.compile(
    r"\b("
    r"schade|kapot|defect|gebarsten|barst|niet werk|werkt niet|beschadig|"
    r"valschade|waterschade|scheur|deuk|sporen van val|voor onderdelen|"
    r"accu ?probleem|batterij ?probleem|icloud ?lock|geen beeld|geen lading|"
    r"scherm ?kapot|scherm ?stuk|gebroken"
    r")\b",
    re.I,
)

# Ontkenningen vlak voor het woord ("geen schade", "zonder schade") maken het ongedaan,
# net als een woord dat direct aan "schade"/"gebreken" vastzit ("schadevrij", "schadeloos").
NEGATION_BEFORE_RE = re.compile(r"(geen|zonder|nooit|nimmer)\s+\w*\s*$", re.I)
NEGATION_SUFFIX_RE = re.compile(r"^(vrij|loos|vrije)\b", re.I)

# Woorden die op "nieuw/verzegeld" wijzen; alles anders telt als "gebruikt".
NEW_RE = re.compile(r"\b(nieuw in (doos|verpakking)|gesealed|sealed|ongeopend|nieuwstaat|splinternieuw)\b", re.I)


def listing_text(item):
    """Titel + (korte) beschrijving uit de zoekresultaten, voor het herkennen van schade/conditie."""
    return f"{item.get('title', '')} {item.get('description', '')}"


def has_damage(text):
    for m in DAMAGE_RE.finditer(text):
        before = text[max(0, m.start() - 20): m.start()]
        after = text[m.end(): m.end() + 6]
        if NEGATION_BEFORE_RE.search(before):
            continue  # "geen schade", "zonder defect" e.d.
        if NEGATION_SUFFIX_RE.match(after):
            continue  # "schadevrij", "schadeloos" (voor de zekerheid, ook los geschreven)
        return True
    return False


def condition_bucket(text, item):
    # Als Marktplaats een expliciet conditieveld meegeeft, gebruik dat als leidend.
    attrs = item.get("attributes") or item.get("extendedAttributes") or []
    for a in attrs:
        if isinstance(a, dict):
            key = str(a.get("key", "")).lower()
            val = str(a.get("value", "")).lower()
            if "condition" in key or "conditie" in key:
                return "nieuw" if "nieuw" in val else "gebruikt"
    return "nieuw" if NEW_RE.search(text) else "gebruikt"


def normalize_group(title, condition):
    """Herleidt een advertentietitel naar een vergelijkingsgroep, bv. 'iphone 14 pro 256gb gebruikt'."""
    t = title.lower()
    is_ipad = "ipad" in t
    m = MODEL_RE.search(t)
    if not m:
        return None
    parts = [p for p in m.groups() if p]
    base = "ipad" if is_ipad else "iphone"
    return f"{base} {' '.join(parts)} {condition}".strip()


def fetch_search_json(query):
    """Haalt de zoekresultatenpagina op en pakt het ingesloten data-blok (__NEXT_DATA__)."""
    url = f"https://www.marktplaats.nl/q/{query}/#sortBy:SORT_INDEX|sortOrder:DECREASING"
    # De hash-URL werkt niet server-side; gebruik de query-param variant die Marktplaats ook accepteert
    url = f"https://www.marktplaats.nl/l/telecommunicatie/q/{query}/"
    r = requests.get(url, headers=HEADERS, timeout=25)
    r.raise_for_status()
    m = re.search(
        r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S
    )
    if not m:
        raise RuntimeError(
            f"Kon __NEXT_DATA__ niet vinden op de zoekpagina voor '{query}'. "
            "Marktplaats heeft zijn pagina-opbouw mogelijk gewijzigd."
        )
    return json.loads(m.group(1))


def extract_listings(data):
    """Zoekt in de JSON-structuur naar de lijst met advertenties.
    De exacte locatie kan per Marktplaats-versie verschillen, dus we zoeken
    breed naar de eerste lijst van dicts die 'itemId' en 'priceInfo' bevat."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            if "itemId" in node and "title" in node:
                found.append(node)
            else:
                for v in node.values():
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return found


def listing_price_cents(item):
    price_info = item.get("priceInfo") or {}
    price_type = (price_info.get("priceType") or "").upper()
    cents = price_info.get("priceCents")
    return price_type, cents


def is_direct_kopen(price_type):
    # "FIXED" = vaste prijs / Direct kopen. Veilingen/bieden hebben een ander priceType.
    return price_type == "FIXED"


def listing_url(item):
    url = item.get("vipUrl") or item.get("url") or ""
    if url.startswith("/"):
        url = "https://www.marktplaats.nl" + url
    return url


def notify(item, group, price, median):
    pct = round((1 - price / median) * 100)
    title = item.get("title", "Advertentie")
    url = listing_url(item)
    msg = (
        f"Goedkope {group}!\n"
        f"{title}\n"
        f"€{price:.0f}  ({pct}% onder mediaan €{median:.0f})\n"
        f"{url}"
    )
    if NTFY_TOPIC:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=msg.encode("utf-8"),
            headers={"Title": "Marktplaats deal", "Click": url, "Priority": "high"},
            timeout=20,
        )
    if CALLMEBOT_PHONE and CALLMEBOT_APIKEY:
        requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": CALLMEBOT_PHONE, "text": msg, "apikey": CALLMEBOT_APIKEY},
            timeout=20,
        )
    print("Gemeld:", title, url)


def main():
    state = {"seen_ids": [], "price_history": {}}
    first_run = not os.path.exists(STATE_FILE)
    if not first_run:
        with open(STATE_FILE) as f:
            state.update(json.load(f))

    seen_ids = set(state["seen_ids"])
    history = {k: v for k, v in state["price_history"].items()}

    for query in SEARCH_QUERIES:
        try:
            data = fetch_search_json(query)
        except Exception as e:
            print(f"Fout bij ophalen '{query}':", e, file=sys.stderr)
            continue

        for item in extract_listings(data):
            item_id = str(item.get("itemId"))
            if not item_id or item_id in seen_ids:
                continue
            seen_ids.add(item_id)

            price_type, cents = listing_price_cents(item)
            title = item.get("title", "")
            text = listing_text(item)

            if has_damage(text):
                continue  # schade/defect: nooit meetellen of melden

            condition = condition_bucket(text, item)
            group = normalize_group(title, condition)
            if not group or cents is None:
                continue
            price = cents / 100

            group_prices = history.setdefault(group, [])

            if not first_run and is_direct_kopen(price_type) and len(group_prices) >= MIN_SAMPLES:
                median = statistics.median(group_prices)
                if price <= median * (1 - DROP_THRESHOLD):
                    notify(item, group, price, median)

            group_prices.append(price)
            history[group] = group_prices[-MAX_HISTORY_PER_GROUP:]

    if first_run:
        print(f"Eerste run: {len(seen_ids)} bestaande advertenties onthouden, geen meldingen.")

    with open(STATE_FILE, "w") as f:
        json.dump({"seen_ids": sorted(seen_ids), "price_history": history}, f)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("Fout:", e, file=sys.stderr)
        sys.exit(1)
