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
import time  
import unicodedata  
import urllib.error  
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
_full_cache = {}  # (lang, card_id) -> card dict or None  
  
  
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
  
  
def alt_numbers(raw_num):  
    """All plausible normalized localIds for a sheet number.  
    Handles '58/102' -> {58}, '180/SV-P' -> {180, sv p180, 180sv p},  
    'BPDP#004' -> {bpdp 4, 4}."""  
    nums = set()  
    if not raw_num:  
        return nums  
    raw = str(raw_num).strip()  
    nums.add(norm_num(raw.split("/")[0]))           # "58/102" -> "58"  
    if "/" in raw:  
        parts = raw.split("/")  
        nums.add(norm_num(parts[-1] + parts[0]))    # "180/SV-P" -> "sv p180"  
        nums.add(norm_num(raw.replace("/", "")))    # -> "180sv p"  
    nums.add(norm_num(raw))                         # "BPDP#004" -> "bpdp 4"  
    nums.discard("")  
    return nums  
  
  
def get(row, *names):  
    """Flexible column lookup (case/space-insensitive, skips empty values)."""  
    lowered = {k.strip().lower(): v for k, v in row.items() if k}  
    for n in names:  
        for k, v in lowered.items():  
            if k == n.lower() and v:  
                return v.strip()  
    return ""  
  
  
def fetch_json(url, retries=1):  
    """GET JSON from TCGdex. 404 raises urllib.error.HTTPError(404);  
    other transient errors are retried once then re-raised."""  
    for attempt in range(retries + 1):  
        try:  
            req = urllib.request.Request(  
                url, headers={"User-Agent": "poke-placeholder-creator"})  
            with urllib.request.urlopen(req, timeout=30) as r:  
                return json.loads(r.read().decode("utf-8"))  
        except urllib.error.HTTPError:  
            raise                                   # real HTTP status, no retry  
        except Exception:  
            if attempt >= retries:  
                raise  
            time.sleep(1)  
  
  
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
    """All cards in a set, cached. Returns [] ONLY on HTTP 404 (set genuinely  
    absent in that language); other failures also return [] but warn."""  
    key = (lang, set_id)  
    if key not in _card_cache:  
        try:  
            data = fetch_json(f"https://api.tcgdex.net/v2/{lang}/sets/{set_id}")  
            _card_cache[key] = data.get("cards", [])  
        except urllib.error.HTTPError as e:  
            if e.code == 404:  
                _card_cache[key] = []               # set doesn't exist here  
            else:  
                print(f"  ! HTTP {e.code} fetching set {lang}/{set_id}")  
                _card_cache[key] = []  
        except Exception as e:  
            print(f"  ! error fetching set {lang}/{set_id}: {e}")  
            _card_cache[key] = []  
    return _card_cache[key]  
  
  
def get_card(lang, card_id):  
    """Fetch a full card by its id (e.g. 'swsh3-136') — the authoritative  
    source for its set name, since brief cards carry no set object."""  
    key = (lang, card_id)  
    if key not in _full_cache:  
        try:  
            _full_cache[key] = fetch_json(  
                f"https://api.tcgdex.net/v2/{lang}/cards/"  
                f"{urllib.parse.quote(card_id)}")  
        except Exception:  
            _full_cache[key] = None  
    return _full_cache[key]  
  
  
def find_set_id(lang, sheet_set, aliases):  
    """Match your sheet's Set name to a TCGdex set id in `lang`.  
    Accepts an alias value that is a set NAME or a set ID. Exact normalized-name  
    match wins; otherwise the subset match with the most tokens wins."""  
    raw = aliases.get(norm(sheet_set), sheet_set)  
    if isinstance(raw, dict):                       # per-language alias  
        raw = raw.get(lang, raw.get("default", ""))  
    want = norm(raw)  
    want_t = set(want.split())  
    if not want_t:  
        return None  
    for s in get_sets(lang):  
        if norm(s.get("id", "")) == want:           # alias/-sheet gave a set id  
            return s["id"]  
    for s in get_sets(lang):  
        if norm(s.get("name", "")) == want:  
            return s["id"]  
    best, best_len = None, 0  
    for s in get_sets(lang):  
        tokens = set(norm(s.get("name", "")).split())  
        if want_t.issubset(tokens) and len(tokens) > best_len:  
            best, best_len = s["id"], len(tokens)  
    return best  
  
  
def resolve_set_id(lang, sheet_set, aliases):  
    """Resolve the sheet's (English) set name to a set id usable in `lang`.  
  
    The sheet's Set column is English but TCGdex localizes set names, so the  
    ENGLISH set list resolves the name first; the id is then reused in the  
    card's own language, where TCGdex shares set ids when the set exists.  
    Returns an id verified to exist in `lang`, or None."""  
    raw = aliases.get(norm(sheet_set))  
    candidates = []  
  
    if isinstance(raw, dict):                       # per-lang alias -> try id directly  
        direct = raw.get(lang, raw.get("default"))  
        if direct:  
            candidates.append(direct)  
    elif raw and raw != sheet_set:                  # plain alias: could be id or name  
        for s in get_sets(lang):  
            if norm(s.get("id", "")) == norm(raw):  
                candidates.append(s["id"])  
                break  
        else:  
            candidates.append(raw)                  # treat as name, verified below  
  
    if lang != "en":  
        en_id = find_set_id("en", sheet_set, aliases)  
        if en_id and en_id not in candidates:  
            candidates.append(en_id)  
  
    own = find_set_id(lang, sheet_set, aliases)     # works when sheet uses  
    if own and own not in candidates:               # localized set names too  
        candidates.append(own)  
  
    for cid in candidates:  
        cards = get_set_cards(lang, cid)            # [] = absent (404) or unresolvable  
        if cards:  
            return cid  
    return None  
  
  
def name_tokens(s):  
    return set(norm(s).split())  
  
  
def pick_by_number(cards, numbers, name_t):  
    """First card whose localId matches any acceptable number; prefer one  
    whose name also overlaps the target name."""  
    if not numbers:  
        return None  
    best = None  
    best_overlap = -1  
    for c in cards:  
        if norm_num(c.get("localId", "")) not in numbers:  
            continue  
        overlap = len(name_t & name_tokens(c.get("name", "")))  
        if overlap > best_overlap:  
            best_overlap = overlap  
            best = c  
    return best  
  
  
def pick_by_name(cards, name_t):  
    """Name-only match inside a set — ONLY if the card's name fully covers the  
    sheet name's tokens (prevents returning a wrong same-name-adjacent card)."""  
    if not name_t:  
        return None  
    for c in cards:  
        if name_t <= name_tokens(c.get("name", "")):  
            return c  
    return None  
  
  
def card_image(card):  
    """TCGdex image base URL + quality/extension."""  
    img = card.get("image")  
    return f"{img}/high.png" if img else None  
  
  
def card_set_id(card):  
    """Set id of a card brief: documented `set.id` if present, else parse the  
    'setid-localId' id format."""  
    sid = (card.get("set") or {}).get("id")  
    if sid:  
        return sid  
    cid = card.get("id", "")  
    return cid.split("-")[0] if "-" in cid else ""  
  
  
def candidate_in_set(lang, card, want, want_t):  
    """Verify a Pass-2 candidate belongs to the sheet's set. Briefs have no  
    set object, so fetch the full card for its authoritative set name."""  
    sid = card_set_id(card)  
    if norm(sid) == want:  
        return True  
    full = get_card(lang, card.get("id", ""))  
    sname = norm((full or {}).get("set", {}).get("name", ""))  
    st = set(sname.split())  
    return bool(want_t) and (want_t <= st)  
  
  
def find_image(row, aliases):  
    """Card image URL from TCGdex, or None."""  
    lang = TCGDEX_LANGS.get(get(row, "Lang", "Language").upper())  
    if not lang:  
        return None  
    sheet_set = get(row, "Set")  
    name_t = name_tokens(get(row, "Card Name", "Name"))  
    raw_num = get(row, "#", "Number", "No.")  
    numbers = alt_numbers(raw_num)                  # {"58"} or {"180","sv p180",...}  
  
    # Pass 1: resolve the set, then number -> strict name inside that set only.  
    set_id = resolve_set_id(lang, sheet_set, aliases)  
    if set_id:  
        cards = get_set_cards(lang, set_id)  
        c = pick_by_number(cards, numbers, name_t) or pick_by_name(cards, name_t)  
        if c:  
            return card_image(c)  
        return None     # right set, card genuinely absent -> don't guess elsewhere  
  
    # Pass 2: no set resolved at all. Server-side filter with STRICT localId  
    # matching ('eq:' — a bare 'localId=4' is a contains-match and would hit  
    # '004', '14', '40'), then verify each candidate's real set name via the  
    # full-card endpoint before accepting it.  
    name = get(row, "Card Name", "Name").strip()  
    if name and numbers:  
        q = urllib.parse.quote  
        try:  
            results = fetch_json(  
                f"https://api.tcgdex.net/v2/{lang}/cards"  
                f"?name={q(name)}&localId=eq:{q(sorted(numbers)[0])}")  
            if isinstance(results, dict):  
                results = [results]  
        except Exception:  
            results = []  
        raw = aliases.get(norm(sheet_set), sheet_set)  
        if isinstance(raw, dict):  
            raw = raw.get(lang, raw.get("default", sheet_set))  
        want = norm(raw)  
        want_t = set(want.split()) - {"the", "set"}  
        for c in results:  
            if candidate_in_set(lang, c, want, want_t):  
                return card_image(c)  
    return None  
  
  
def build_desc(row):  
    """Extra info shown under the image: Rarity + Variant (from Variant/Variant Type)."""  
    parts = []  
    for k in ("Rarity",):  
        v = get(row, k)  
        if v:  
            parts.append(v)  
    v = get(row, "Variant", "Variant Type", "Varient")  
    if v:  
        parts.append(v)  
    return " - ".join(parts)  
  
  
def render_pdf(html_path, out_pdf, page_key):  
    page_css = {"a4": "A4", "letter": "Letter"}.get(page_key.lower(), "A4")  
    with sync_playwright() as p:  
        browser = p.chromium.launch()  
        page = browser.new_page()  
        page.goto(Path(html_path).resolve().as_uri(), wait_until="networkidle")  
        page.pdf(path=out_pdf, format=page_css, print_background=True,  
                 margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})  
        browser.close()  
  
  
def main():  
    ap = argparse.ArgumentParser(description="TCGdex -> placeholder PDF")  
    ap.add_argument("csv", help="CSV export of your sheet (first column = order)")  
    ap.add_argument("-o", "--out", default="placeholders.pdf")  
    ap.add_argument("--html", default="placeholders.html",  
                    help="intermediate HTML (edit/re-render without refetching)")  
    ap.add_argument("--page", default="a4", choices=["a4", "letter"])  
    args = ap.parse_args()  
  
    # optional manual aliases for set names that differ from TCGdex  
    # e.g. {"set_aliases": {"Base Set": "base1", "Pokémon Card 151":  
    #                        {"ja": "sv2a", "default": "sv03.5"}}}  
    aliases = {}  
    if Path("mapping.json").exists():  
        aliases = json.loads(Path("mapping.json")  
                             .read_text(encoding="utf-8")).get("set_aliases", {})  
        aliases = {norm(k): v for k, v in aliases.items()}  
  
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