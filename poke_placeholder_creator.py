#!/usr/bin/env python3  
"""  
poke_placeholder_creator  
Reads a CSV export of your card spreadsheet, fetches card images from the  
TCGdex API (https://api.tcgdex.net) in the card's own language, and renders  
a 3x3-per-page print-ready PDF of card-sized placeholders (63mm x 88mm)  
with cut lines. PDF rendering is done with Playwright/Chromium.  
"""  
  
import argparse  
import csv  
import json  
import re  
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
    # DUT has no TCGdex language -> returns None (NO IMAGE FOUND)  
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
    """Flexible column lookup (case/space-insensitive, skips empty values)."""  
    lowered = {k.strip().lower(): v for k, v in row.items() if k}  
    for n in names:  
        for k, v in lowered.items():  
            if k == n.lower() and v:  
                return v.strip()  
    return ""  
  
  
def fetch_json(url):  
    req = urllib.request.Request(url, headers={"User-Agent": "poke-placeholder-creator"})  
    with urllib.request.urlopen(req, timeout=30) as r:  
        return json.loads(r.read().decode("utf-8"))  
  
  
def get_sets(lang):  
    """All sets in a TCGdex language, cached."""  
    if lang not in _set_cache:  
        try:  
            _set_cache[lang] = fetch_json(f"https://api.tcgdex.net/v2/{lang}/sets")  
        except Exception as e:  
            print(f"  ! could not fetch {lang} sets: {e}")  
            _set_cache[lang] = []  
    return _set_cache[lang]  
  
  
def get_set_cards(lang, set_id):  
    """All cards in a set, cached. Returns [] if the set doesn't exist  
    in that language (TCGdex 404s)."""  
    key = (lang, set_id)  
    if key not in _card_cache:  
        try:  
            data = fetch_json(f"https://api.tcgdex.net/v2/{lang}/sets/{set_id}")  
            _card_cache[key] = data.get("cards", [])  
        except Exception:  
            _card_cache[key] = []   # set absent in this language -> no match  
    return _card_cache[key]  
  
  
def _resolve_alias(sheet_set, aliases):  
    """Return the alias target for a sheet set name, or the name itself."""  
    return aliases.get(norm(sheet_set), sheet_set)  
  
  
def find_set_id(lang, sheet_set, aliases):  
    """Match a set name to a TCGdex set id within one language's set list.  
  
    The alias target may be either a set name OR a set id (e.g. "base1").  
    Exact normalized-name match wins; otherwise the subset match with the  
    most tokens is returned so ambiguous short names don't grab the first  
    partial hit."""  
    target = _resolve_alias(sheet_set, aliases)  
    want = norm(target)  
    want_t = set(want.split())  
    if not want_t:  
        return None  
    sets = get_sets(lang)  
    # alias that is a raw set id -> direct hit  
    for s in sets:  
        if norm(s.get("id", "")) == want:  
            return s["id"]  
    for s in sets:  
        if norm(s.get("name", "")) == want:  
            return s["id"]  
    best_id, best_overlap = None, 0  
    for s in sets:  
        set_t = set(norm(s.get("name", "")).split())  
        if want_t.issubset(set_t) and len(set_t) > best_overlap:  
            best_id, best_overlap = s["id"], len(set_t)  
    return best_id  
  
  
def resolve_set_id(lang, sheet_set, aliases):  
    """Resolve the sheet's (English) set name to a set id, preferring the  
    English set list since sheet set names are English, then falling back  
    to the card's own language (in case a sheet uses localized names).  
    Returns the set id only if that set actually exists in the card's  
    language -- TCGdex shares set ids across languages."""  
    candidates = []  
    if lang != "en":  
        en_id = find_set_id("en", sheet_set, aliases)  
        if en_id:  
            candidates.append(en_id)  
    own_id = find_set_id(lang, sheet_set, aliases)  
    if own_id and own_id not in candidates:  
        candidates.append(own_id)  
    for sid in candidates:  
        if get_set_cards(lang, sid):  
            return sid  
    return None  
  
  
def name_tokens(s):  
    return set(norm(s).split())  
  
  
def pick_by_number(cards, number, name_t):  
    """Cards matching localId; require a name-token overlap to guard against  
    promo-number collisions (e.g. '059/M-P' matching localId 59)."""  
    num_match = [c for c in cards  
                 if norm_num(str(c.get("localId", ""))) == number  
                 or re.sub(r"\D", "", str(c.get("localId", ""))) == re.sub(r"\D", "", number)]  
    if not num_match:  
        return None  
    if not name_t:  
        return num_match[0]  
    for c in num_match:  
        if name_t & name_tokens(c.get("name", "")):  
            return c  
    return None  # number matched but wrong card name -> don't return a wrong image  
  
  
def pick_by_name(cards, name_t):  
    """Name-only match inside a set. Only returns a card when ALL of the  
    sheet's name tokens appear in the card name, so a partial overlap  
    (e.g. sheet 'Charmander ex' vs a plain 'Charmander') can never produce  
    a wrong image. Safe to use as a fallback for collector-number notation  
    mismatches like sheet '58/102' vs TCGdex localId '46'."""  
    if not name_t:  
        return None  
    for c in cards:  
        if name_t <= name_tokens(c.get("name", "")):  
            return c  
    return None  
  
  
def card_set_id(c):  
    """Set id from a brief card result (id looks like 'swsh3-136')."""  
    cid = str(c.get("id", ""))  
    return cid.rsplit("-", 1)[0] if "-" in cid else ""  
  
  
def card_image(c):  
    img = c.get("image")  
    return f"{img}/high.png" if img else None  
  
  
def find_image(row, aliases):  
    """Return card image URL from TCGdex, or None."""  
    lang = TCGDEX_LANGS.get(get(row, "Lang", "Language").strip().upper())  
    if not lang:  
        return None  
  
    name = get(row, "Card Name", "Name").strip()  
    name_t = name_tokens(name)  
    raw_num = get(row, "#", "Number", "No.")  
    number = norm_num(raw_num.split("/")[0])      # "58/102" -> "58"  
    sheet_set = get(row, "Set")  
  
    # Pass 1: resolve the set (English names -> shared set ids), then match  
    # inside it by number+name, falling back to a fully-covering name match  
    # for collector-notation mismatches. If the set resolves but the card  
    # isn't in it, STOP -- a same-numbered card from another set is wrong.  
    set_id = resolve_set_id(lang, sheet_set, aliases)  
    if set_id:  
        cards = get_set_cards(lang, set_id)  
        c = pick_by_number(cards, number, name_t) or pick_by_name(cards, name_t)  
        if c:  
            return card_image(c)  
        return None  
  
    # Pass 2: the set couldn't be resolved at all (typo, promo set, or set  
    # absent from TCGdex). Language-wide name search is allowed, but a hit  
    # is only accepted when its own set name fuzzily matches the sheet's  
    # set -- a bare number match across all sets is never accepted.  
    if name:  
        try:  
            results = fetch_json(  
                f"https://api.tcgdex.net/v2/{lang}/cards?name={urllib.parse.quote(name)}")  
            if isinstance(results, dict):  
                results = [results]  
        except Exception:  
            results = []  
        want = norm(_resolve_alias(sheet_set, aliases))  
        want_t = set(want.split())  
        set_names = {s["id"]: norm(s.get("name", "")) for s in get_sets(lang)}  
        set_names.update({s["id"]: norm(s.get("name", "")) for s in get_sets("en")})  
        candidates = []  
        for c in results:  
            sid = card_set_id(c)  
            set_t = set(set_names.get(sid, "").split())  
            if want_t and (want_t.issubset(set_t) or set_t.issubset(want_t)):  
                candidates.append(c)  
            elif norm(sid) == want:   # alias was a raw set id  
                candidates.append(c)  
        c = pick_by_number(candidates, number, name_t) or pick_by_name(candidates, name_t)  
        if c:  
            return card_image(c)  
    return None  
  
  
def holo_label(row):  
    """Generic holo label from Art Type when no note specifies a special holo."""  
    text = get(row, "Art Type", "Artwork").lower()  
    if "non-holo" in text or "non holo" in text:  
        return ""  
    if "reverse holo" in text:  
        return "Reverse Holo"  
    if "shiny holo" in text:  
        return "Shiny Holo"  
    if "full art" in text:  
        return "Full Art"  
    if re.search(r"\bholo\b", text):  
        return "Holo"  
    return ""  
  
  
def build_desc(row):  
    """Single descriptor line: special-holo phrase first, then all other  
    notes, joined with ' • '."""  
    notes_raw = get(row, "Version / Notes", "Notes", "Version")  
    parts = [p.strip() for p in notes_raw.split(",") if p.strip()]  
  
    holo = ""  
    other = []  
    for p in parts:  
        if "holo" in p.lower():  
            m = re.search(r"([A-Za-zÀ-ÿ' ]+?\b[Hh]olo)\b", p)  
            label = m.group(1).strip().title() if m else p.title()  
            generic = {"holo", "reverse holo", "non holo", "non-holo"}  
            if label.lower() not in generic:  
                holo = label            # note's holo wins over Art Type  
            rest = (p[:m.start()] + p[m.end():]).strip() if m else ""  
            if rest:  
                other.append(rest)  
        else:  
            other.append(p)  
  
    if not holo:  
        holo = holo_label(row)  
  
    segs = ([holo] if holo else []) + other  
    return " • ".join(segs)  
  
  
def render_pdf(html_path, out_path, page_size):  
    with sync_playwright() as pw:  
        browser = pw.chromium.launch()  
        page = browser.new_page()  
        page.goto(Path(html_path).resolve().as_uri())  
        page.wait_for_load_state("networkidle")  
        page.pdf(  
            path=out_path,  
            width="8.5in" if page_size == "letter" else "210mm",  
            height="11in" if page_size == "letter" else "297mm",  
            print_background=True,  
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},  
        )  
        browser.close()  
  
  
def main():  
    ap = argparse.ArgumentParser()  
    ap.add_argument("csv", help="CSV export of your Google Sheet")  
    ap.add_argument("-o", "--out", default="placeholders.pdf")  
    ap.add_argument("--html", default="placeholders.html")  
    ap.add_argument("--mapping", default="mapping.json")  
    ap.add_argument("--page", choices=["letter", "a4"], default="letter")  
    args = ap.parse_args()  
  
    mapping = {}  
    mapping_path = Path(args.mapping)  
    if mapping_path.exists():  
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))  
    aliases = {norm(k): v for k, v in mapping.get("set_aliases", {}).items()}  
  
    rows = list(csv.DictReader(open(args.csv, encoding="utf-8-sig")))  
    print(f"{len(rows)} rows in spreadsheet")  
    print("Fetching card images from TCGdex ...")  
  
    cards, unmatched = [], []  
    for i, row in enumerate(rows, 1):  
        if not any(v and v.strip() for v in row.values()):  
            continue  # skip blank rows  
        if i % 25 == 0:  
            print(f"  {i}/{len(rows)} rows processed ...")  
        url = find_image(row, aliases)  
        card = {  
            "name": get(row, "Card Name", "Name"),  
            "set": get(row, "Set"),  
            "number": get(row, "#", "Number", "No."),  
            "lang": get(row, "Lang", "Language"),  
            "year": get(row, "Year"),  
            "desc": build_desc(row),  
            "image": url,  
        }  
        cards.append(card)  
        if not url:  
            unmatched.append({  
                "row": i,  
                "name": card["name"],  
                "set": card["set"],  
                "number": card["number"],  
                "lang": card["lang"],  
            })  
  
    template = Template(Path("card_template.html").read_text(encoding="utf-8"))  
    pages = [cards[i:i + 9] for i in range(0, len(cards), 9)]  
    html = template.render(pages=pages, page_size=args.page)  
  
    Path(args.html).write_text(html, encoding="utf-8")  
    print("Rendering PDF via Chromium ...")  
    render_pdf(args.html, args.out, args.page)  
    print(f"Wrote {args.out} ({len(pages)} pages, {len(cards)} cards)")  
    print(f"Editable HTML copy: {args.html}")  
  
    if unmatched:  
        with open("unmatched.csv", "w", newline="", encoding="utf-8") as f:  
            w = csv.DictWriter(f, fieldnames=["row", "name", "set", "number", "lang"])  
            w.writeheader()  
            w.writerows(unmatched)  
        print(f"{len(unmatched)} unmatched -> unmatched.csv")  
  
  
if __name__ == "__main__":  
    main()