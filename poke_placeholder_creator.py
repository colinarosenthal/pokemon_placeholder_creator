#!/usr/bin/env python3  
"""  
poke_placeholder_creator  
Reads a CSV export of your card spreadsheet, matches each row to an image  
in a local card-image dataset, and renders a 3x3-per-page print-ready PDF  
of card-sized placeholders (63mm x 88mm) with cut lines.  
"""  
  
import argparse  
import csv  
import json  
import re  
import sys  
import unicodedata  
from pathlib import Path  
  
from jinja2 import Template  
from weasyprint import HTML  
  
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}  
  
  
def norm(s: str) -> str:  
    """Normalize a string for fuzzy matching."""  
    if not s:  
        return ""  
    s = unicodedata.normalize("NFKC", str(s))  
    s = s.lower()  
    s = re.sub(r"[^a-z0-9一-鿿]+", " ", s)  
    return s.strip()  
  
  
def norm_num(s: str) -> str:  
    """Normalize a card number: '004' -> '4', 'TG04' -> 'tg4'."""  
    if not s:  
        return ""  
    s = str(s).strip().lower()  
    m = re.match(r"([a-z]*)\s*0*(\d+)(.*)", s)  
    if m:  
        return f"{m.group(1)}{m.group(2)}{m.group(3)}".strip()  
    return s  
  
  
def build_index(image_root: Path):  
    """Walk the dataset folder and index every image file."""  
    index = []  # list of dicts: path, norm_text (folder+filename), number  
    for p in image_root.rglob("*"):  
        if p.suffix.lower() not in IMAGE_EXTS:  
            continue  
        rel = p.relative_to(image_root)  
        text = norm(" ".join(rel.parts))          # "base1 4 charmander"  
        num = norm_num(p.stem)                    # number in filename, if any  
        m = re.search(r"(\d+)", p.stem)  
        index.append({"path": p, "text": text, "num": num,  
                      "digits": m.group(1).lstrip("0") if m else ""})  
    return index  
  
  
def load_mapping(path: Path):  
    if path.exists():  
        return json.loads(path.read_text(encoding="utf-8"))  
    return {}  
  
  
def find_image(row, index, set_aliases):  
    """Return image Path or None. Match set+number first, then name+number."""  
    card_set = norm(row.get("Set", ""))  
    card_set = norm(set_aliases.get(card_set, card_set))  
    name = norm(row.get("Card Name", ""))  
    number = norm_num(row.get("#", ""))  
    digits = re.sub(r"\D", "", number)  
  
    set_tokens = set(card_set.split())  
  
    # Pass 1: set name (all tokens) + number  
    if set_tokens and digits:  
        for e in index:  
            if digits == e["digits"] and set_tokens.issubset(set(e["text"].split())):  
                return e["path"]  
  
    # Pass 2: card name + number  
    name_tokens = set(name.split())  
    if name_tokens and digits:  
        for e in index:  
            if digits == e["digits"] and name_tokens.issubset(set(e["text"].split())):  
                return e["path"]  
  
    # Pass 3: name alone  
    if name_tokens:  
        for e in index:  
            if name_tokens.issubset(set(e["text"].split())):  
                return e["path"]  
  
    return None  
  
  
def get(row, *names):  
    """Flexible column lookup (case/space-insensitive, ignores empty cols)."""  
    lowered = {k.strip().lower(): v for k, v in row.items() if k}  
    for n in names:  
        for k, v in lowered.items():  
            if k == n.lower() and v:  
                return v.strip()  
    return ""  
  
  
def main():  
    ap = argparse.ArgumentParser()  
    ap.add_argument("csv", help="CSV export of your Google Sheet")  
    ap.add_argument("images", help="Root folder of the card image dataset")  
    ap.add_argument("-o", "--out", default="placeholders.pdf")  
    ap.add_argument("--mapping", default="mapping.json")  
    ap.add_argument("--html", default="placeholders.html",  
                    help="Also write the intermediate HTML here for editing")  
    ap.add_argument("--page", choices=["letter", "a4"], default="letter")  
    args = ap.parse_args()  
  
    image_root = Path(args.images)  
    if not image_root.is_dir():  
        sys.exit(f"Image folder not found: {image_root}")  
  
    print(f"Indexing images in {image_root} ...")  
    index = build_index(image_root)  
    print(f"  {len(index)} images indexed")  
  
    set_aliases = {norm(k): v for k, v in  
                   load_mapping(Path(args.mapping)).get("set_aliases", {}).items()}  
  
    rows = list(csv.DictReader(open(args.csv, encoding="utf-8-sig")))  
    print(f"{len(rows)} rows in spreadsheet")  
  
    cards, unmatched = [], []  
    for i, row in enumerate(rows, 1):  
        if not any(v and v.strip() for v in row.values()):  
            continue  # skip blank rows  
        img = find_image(row, index, set_aliases)  
        card = {  
            "name": get(row, "Card Name", "Name"),  
            "set": get(row, "Set"),  
            "number": get(row, "#", "Number", "No."),  
            "rarity": get(row, "Rarity"),  
            "lang": get(row, "Lang", "Language"),  
            "year": get(row, "Year"),  
            "notes": get(row, "Version / Notes", "Notes", "Version"),  
            "art": get(row, "Artwork", "Art Type"),  
            "image": img.as_uri() if img else None,  
        }  
        cards.append(card)  
        if not img:  
            unmatched.append({"row": i, **{k: card[k] for k in  
                              ("name", "set", "number", "lang")}})  
  
    template = Template(Path("card_template.html").read_text(encoding="utf-8"))  
    pages = [cards[i:i + 9] for i in range(0, len(cards), 9)]  
    html = template.render(pages=pages, page_size=args.page)  
  
    Path(args.html).write_text(html, encoding="utf-8")  
    HTML(string=html, base_url=".").write_pdf(args.out)  
    print(f"Wrote {args.out} ({len(pages)} pages, {len(cards)} cards)")  
    print(f"Editable HTML copy: {args.html}")  
  
    if unmatched:  
        with open("unmatched.csv", "w", newline="", encoding="utf-8") as f:  
            w = csv.DictWriter(f, fieldnames=["row", "name", "set", "number", "lang"])  
            w.writeheader()  
            w.writerows(unmatched)  
        print(f"{len(unmatched)} unmatched cards -> unmatched.csv")  
  
  
if __name__ == "__main__":  
    main()