#!/usr/bin/env python3  
"""  
poke_placeholder_creator  
Reads a CSV export of your card spreadsheet, fetches card images from the  
TCGdex API (https://api.tcgdex.net) in the card's own language, and renders  
a 3x3-per-page print-ready PDF of card-sized placeholders (63mm x 88mm)  
with cut lines. PDF rendering is done with Playwright/Chromium.  
Writes match_report.csv so you can audit which TCGdex card each row used.  
"""  
  
import argparse  
import csv  
import json  
import re  
import time  
import unicodedata  
import urllib.parse  
import urllib.request  
from pathlib import Path  
  
from jinja2 import Template  
from playwright.sync_api import sync_playwright  
  
# Sheet Lang code -> TCGdex language code  
TCGDEX_LANGS = {  
    "ENG": "en", "JPN": "ja", "KOR": "ko",  
    "GER": "de", "FRE": "fr", "ITA": "it",  
    "SPA": "es", "LAT": "es", "POR": "pt",  
    "TCHI": "zh-tw", "SCHI": "zh-cn",  
    "THA": "th", "IND": "id",  
    # DUT has no TCGdex language -> NO IMAGE FOUND  
}  
  
_set_cache = {}   # lang -> list of sets  
_card_cache = {}  # (lang, set_id) -> list of cards  
  
  
def norm(s):  
    """Normalize a string for fuzzy matching."""  
    if not s:  
        return ""  
    s = unicodedata.normalize("NFKC", str(s)).lower()  
    return re.sub(r"[^a-z0-9一-鿿가-힯぀-ヿ]+", " ", s).strip()  
  
  
def norm_num(s):  
    """Normalize a card number: '004' -> '4', 'TG04' -> 'tg4'."""  
    if not s:  
        return ""  
    s = str(s).strip().lower()  
    m = re.match(r"([a-z]*)\s*0*(\d+)(.*)", s)  
    return f"{m.group(1)}{m.group(2)}{m.group(3)}".strip() if m else s  
  
  
def get(row, *names):  
    lowered = {k.strip().lower(): v for k, v in row.items() if k}  
    for n in names:  
        for k, v in lowered.items():  
            if k == n.lower() and v:  
                return v.strip()  
    return ""  
  
  
def fetch_json(url, retries=4):  
    """GET a JSON endpoint with exponential-backoff retries."""  
    for attempt in range(retries):  
        try:  
            req = urllib.request.Request(  
                url, headers={"User-Agent": "poke-placeholder-creator/1.0"})  
            with urllib.request.urlopen(req, timeout=30) as r:  
                return json.loads(r.read().decode("utf-8"))  
        except urllib.error.HTTPError as e:  
            if e.code == 404:  
                raise  
            wait = 2 ** attempt   # 1s, 2s, 4s, 8s  
            print(f"! HTTP {e.code} fetching {url} -- retrying in {wait}s")  
            time.sleep(wait)  
        except Exception as e:  
            wait = 2 ** attempt  
            print(f"! fetch failed ({e}) {url} -- retrying in {wait}s")  
            time.sleep(wait)  
    print(f"! giving up on {url}")  
    return {}  
  
  
def get_sets(lang):  
    if lang not in _set_cache:  
        _set_cache[lang] = fetch_json(  
            f"https://api.tcgdex.net/v2/{lang}/sets")  
    return _set_cache[lang]  
  
  
def get_set_cards(lang, set_id):  
    key = (lang, set_id)  
    if key not in _card_cache:  
        data = fetch_json(  
            f"https://api.tcgdex.net/v2/{lang}/sets/{set_id}")  
        _card_cache[key] = data.get("cards", []) or []  
    return _card_cache[key]  
  
  
def find_set_id(lang, set_name, aliases):  
    """Map a sheet Set value to a TCGdex set id."""  
    target = norm(set_name)  
    alias = aliases.get(target)  
    if alias:  
        target = norm(alias)  
    if not target:  
        return None  
    target_words = set(target.split())  
    best = None  
    for s in get_sets(lang):  
        name_words = set(norm(s.get("name", "")).split())  
        if not name_words:  
            continue  
        if target_words == name_words:  
            return s["id"]  
        if target_words.issubset(name_words) or name_words.issubset(target_words):  
            best = best or s["id"]  
    return best  
  
  
def pick(cards, number, name_t):  
    """Pick a card from a set's card list by number, verified by name."""  
    num_match = []  
    name_match = []  
    digits = re.sub(r"\D", "", number)  
    for c in cards:  
        local = norm_num(str(c.get("localId", "")))  
        cname = set(norm(c.get("name", "")).split())  
        has_name_overlap = bool(name_t & cname)  
        if local == number or (digits and re.sub(r"\D", "", local) == digits):  
            if has_name_overlap:  
                num_match.append(c)  
        elif has_name_overlap:  
            name_match.append(c)  
    return num_match[0] if num_match else (name_match[0] if name_match else None)  
  
  
def _img(c):  
    img = c.get("image")  
    return f"{img}/high.png" if img else None  
  
  
def find_image(row, aliases):  
    """Return (image_url, tcgdex_id). Never guesses a wrong card."""  
    lang = TCGDEX_LANGS.get((get(row, "Lang", "Language") or "").strip().upper())  
    if not lang:  
        return None, None  
  
    name_t = set(norm(get(row, "Card Name", "Name")).split())  
    set_id = find_set_id(lang, get(row, "Set"), aliases)  
  
    number = norm_num(get(row, "#", "Number", "No."))  
    if "/" in number:  
        number = number.split("/")[0]  
  
    if set_id:  
        c = pick(get_set_cards(lang, set_id), number, name_t)  
        if c:  
            return _img(c), c.get("id")  
        return None, None  # set matched but card didn't -> don't guess elsewhere  
  
    # No set match: name search only, and only accept if the number also matches.  
    name = get(row, "Card Name", "Name").strip()  
    if name:  
        results = fetch_json(  
            f"https://api.tcgdex.net/v2/{lang}/cards?name={urllib.parse.quote(name)}") or []  
        for c in results:  
            if norm_num(str(c.get("localId", ""))) == number:  
                return _img(c), c.get("id")  
    return None, None  
  
  
def build_desc(row):  
    """Build the single descriptor line shown under the card image.  
    Holo type comes from 'Version / Notes' first (special holos like Cosmos  
    Holo, Master Ball Holo override the Art Type column); Non-Holo is hidden.  
    All other notes are appended joined by ' • '."""  
    pieces = [p.strip() for p in re.split(  
        r"[,;|]", get(row, "Version / Notes", "Notes", "Version")) if p.strip()]  
    holo = ""  
    keep = []  
    for p in pieces:  
        pl = p.lower()  
        if "non-holo" in pl or "non holo" in pl or "no rarity" in pl:  
            continue  
        if "holo" in pl:  
            m = re.search(r"([a-z0-9' +&-]*?\bholo\b)", pl)  
            holo = (m.group(1).strip().title() if m else p.strip().title())  
            rest = p[:m.start()].strip(" -–—·")  
            if rest:  
                keep.append(rest)  
        else:  
            keep.append(p)  
    art = get(row, "Art Type").strip()  
    if not holo and art:  
        al = art.lower()  
        if "non-holo" not in al and "non holo" not in al:  
            holo = art.title()  
    out = ([holo] if holo else []) + keep  
    return " • ".join(dict.fromkeys(out))  
  
  
def render_pdf(html_path, out_path, page_size):  
    size = ({"letter": "8.5in"}, {"a4": "8.27in"})  
    w, h = ("8.5in", "11in") if page_size == "letter" else ("8.27in", "11.69in")  
    with sync_playwright() as p:  
        browser = p.chromium.launch()  
        page = browser.new_page()  
        page.goto(Path(html_path).resolve().as_uri(),  
                  wait_until="networkidle", timeout=120000)  
        page.pdf(path=out_path, width=w, height=h,  
                 print_background=True,  
                 margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})  
        browser.close()  
  
  
def main():  
    ap = argparse.ArgumentParser()  
    ap.add_argument("csv", help="CSV export of your card spreadsheet")  
    ap.add_argument("-o", "--out", default="placeholders.pdf")  
    ap.add_argument("--html", default="placeholders.html",  
                    help="also write the rendered editable HTML here")  
    ap.add_argument("--page", default="letter", choices=["letter", "a4"])  
    ap.add_argument("--mapping", default="mapping.json",  
                    help="JSON with optional set_aliases for TCGdex set names")  
    args = ap.parse_args()  
  
    mapping = {}  
    if Path(args.mapping).exists():  
        mapping = json.loads(Path(args.mapping).read_text(encoding="utf-8"))  
    aliases = {norm(k): v for k, v in mapping.get("set_aliases", {}).items()}  
  
    rows = list(csv.DictReader(open(args.csv, encoding="utf-8-sig")))  
    cards = []  
    for i, row in enumerate(rows, 1):  
        if not get(row, "Card Name", "Name"):  
            continue  
        time.sleep(0.15)   # stay under TCGdex rate limits  
        url, cid = find_image(row, aliases)  
        if i % 25 == 0:  
            print(f"  matched {i}/{len(rows)} rows ...")  
        cards.append({  
            "name": get(row, "Card Name", "Name"),  
            "set": get(row, "Set", "Set Name"),  
            "number": get(row, "#", "Number", "No."),  
            "lang": get(row, "Lang", "Language"),  
            "year": get(row, "Year"),  
            "desc": build_desc(row),  
            "image": url,  
            "match": cid or "",  
        })  
  
    template = Template(Path("card_template.html").read_text(encoding="utf-8"))  
    pages = [cards[i:i + 9] for i in range(0, len(cards), 9)]  
    html = template.render(pages=pages, page_size=args.page)  
  
    Path(args.html).write_text(html, encoding="utf-8")  
    print("Rendering PDF via Chromium ...")  
    render_pdf(args.html, args.out, args.page)  
    print(f"Wrote {args.out} ({len(pages)} pages, {len(cards)} cards)")  
    print(f"Editable HTML copy: {args.html}")  
  
    with open("match_report.csv", "w", newline="", encoding="utf-8") as f:  
        w = csv.DictWriter(f, fieldnames=["row", "name", "set", "number",  
                                          "lang", "match"])  
        w.writeheader()  
        for i, c in enumerate(cards, 1):  
            w.writerow({"row": i, "name": c["name"], "set": c["set"],  
                        "number": c["number"], "lang": c["lang"],  
                        "match": c["match"]})  
    print("Match report -> match_report.csv")  
  
  
if __name__ == "__main__":  
    main()