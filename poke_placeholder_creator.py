#!/usr/bin/env python3  
"""  
poke_placeholder_creator  
Reads a CSV export of your card spreadsheet, matches each row to an image  
URL in the priyamchoksi/pokemon-cards Kaggle dataset (CSV with image_url  
column pointing at images.pokemontcg.io), and renders a 3x3-per-page  
print-ready PDF of card-sized placeholders (63mm x 88mm) with cut lines.  
PDF rendering is done with Playwright/Chromium (no GTK needed).  
"""  
  
import argparse  
import csv  
import json  
import re  
import unicodedata  
from pathlib import Path  
  
from jinja2 import Template  
from playwright.sync_api import sync_playwright  
  
  
def norm(s):  
    """Normalize a string for fuzzy matching."""  
    if not s:  
        return ""  
    s = unicodedata.normalize("NFKC", str(s)).lower()  
    return re.sub(r"[^a-z0-9一-鿿]+", " ", s).strip()  
  
  
def norm_num(s):  
    """Normalize a card number: '004' -> '4', 'TG04' -> 'tg4'."""  
    if not s:  
        return ""  
    s = str(s).strip().lower()  
    m = re.match(r"([a-z]*)\s*0*(\d+)(.*)", s)  
    return f"{m.group(1)}{m.group(2)}{m.group(3)}".strip() if m else s  
  
  
def load_dataset(csv_path):  
    """Load the priyamchoksi/pokemon-cards dataset CSV."""  
    cards = []  
    for r in csv.DictReader(open(csv_path, encoding="utf-8-sig")):  
        cid = (r.get("id") or "").strip()          # e.g. "pl3-1", "base1-46"  
        num = norm_num(cid.split("-")[-1]) if "-" in cid else ""  
        cards.append({  
            "id": cid,  
            "num": num,  
            "name": norm(r.get("name", "")),  
            "set": norm(r.get("set_name", "")),  
            "url": (r.get("image_url") or "").strip(),  
        })  
    return cards  
  
  
def get(row, *names):  
    """Flexible column lookup (case/space-insensitive, skips empty values)."""  
    lowered = {k.strip().lower(): v for k, v in row.items() if k}  
    for n in names:  
        for k, v in lowered.items():  
            if k == n.lower() and v:  
                return v.strip()  
    return ""  
  
  
def find_image(row, ds, aliases):  
    """Return image URL or None. Match set+number, then name+number, then name+set."""  
    name = norm(get(row, "Card Name", "Name"))  
    card_set = norm(get(row, "Set"))  
    card_set = norm(aliases.get(card_set, card_set))  
    number = norm_num(get(row, "#", "Number", "No."))  
    set_t = set(card_set.split())  
    name_t = set(name.split())  
  
    # Pass 1: set + number  
    if set_t and number:  
        for e in ds:  
            if number == e["num"] and set_t.issubset(set(e["set"].split())):  
                return e["url"]  
    # Pass 2: name + number  
    if name_t and number:  
        for e in ds:  
            if number == e["num"] and name_t.issubset(set(e["name"].split())):  
                return e["url"]  
    # Pass 3: name + set  
    if name_t and set_t:  
        for e in ds:  
            if name_t.issubset(set(e["name"].split())) and set_t.issubset(set(e["set"].split())):  
                return e["url"]  
    return None  
  
  
def render_pdf(html_path, pdf_path, page_size):  
    """Print the rendered HTML to PDF via headless Chromium."""  
    width, height = ("8.5in", "11in") if page_size == "letter" else ("210mm", "297mm")  
    with sync_playwright() as p:  
        browser = p.chromium.launch()  
        page = browser.new_page()  
        page.goto(Path(html_path).resolve().as_uri())  
        page.wait_for_load_state("networkidle")  # wait for remote card images  
        page.pdf(path=pdf_path, width=width, height=height,  
                 print_background=True, margin={"top": "0", "bottom": "0",  
                                                "left": "0", "right": "0"})  
        browser.close()  
  
  
def main():  
    ap = argparse.ArgumentParser()  
    ap.add_argument("csv", help="CSV export of your Google Sheet")  
    ap.add_argument("dataset", help="The Kaggle pokemon-cards CSV file")  
    ap.add_argument("-o", "--out", default="placeholders.pdf")  
    ap.add_argument("--mapping", default="mapping.json")  
    ap.add_argument("--html", default="placeholders.html",  
                    help="Also write the rendered HTML here for editing")  
    ap.add_argument("--page", choices=["letter", "a4"], default="letter")  
    args = ap.parse_args()  
  
    print("Loading dataset ...")  
    ds = load_dataset(args.dataset)  
    print(f"  {len(ds)} cards")  
  
    mapping = {}  
    mapping_path = Path(args.mapping)  
    if mapping_path.exists():  
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))  
    aliases = {norm(k): v for k, v in mapping.get("set_aliases", {}).items()}  
  
    rows = list(csv.DictReader(open(args.csv, encoding="utf-8-sig")))  
    print(f"{len(rows)} rows in spreadsheet")  
  
    cards, unmatched = [], []  
    for i, row in enumerate(rows, 1):  
        if not any(v and v.strip() for v in row.values()):  
            continue  # skip blank rows  
        url = find_image(row, ds, aliases)  
        card = {  
            "name": get(row, "Card Name", "Name"),  
            "set": get(row, "Set"),  
            "number": get(row, "#", "Number", "No."),  
            "rarity": get(row, "Rarity"),  
            "lang": get(row, "Lang", "Language"),  
            "year": get(row, "Year"),  
            "notes": get(row, "Version / Notes", "Notes", "Version"),  
            "art": get(row, "Artwork", "Art Type"),  
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