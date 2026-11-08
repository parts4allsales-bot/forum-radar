"""
Ραντάρ συζητήσεων για ανταλλακτικά αυτοκινήτων.

Διαβάζει ροές RSS/Atom (fora, Reddit, Google Alerts), κρατά όσες συζητήσεις
μοιάζουν με "ψάχνω ανταλλακτικό για X", και φτιάχνει μια σελίδα HTML με:
  - τη λίστα συζητήσεων και link για να τις ανοίξεις,
  - ένα ΣΧΕΔΙΟ απάντησης για καθεμία, που το διορθώνεις και το ποστάρεις εσύ,
  - έτοιμα links αναζήτησης για Facebook ομάδες, Instagram, TikTok, X, car.gr.

Δεν ποστάρει τίποτα μόνο του. Χρειάζεται μόνο Python 3.11+ (χωρίς πρόσθετα).
Τρέξιμο:  python radar.py            (ή διπλό κλικ στο run.bat)
"""

from __future__ import annotations

import html
import json
import re
import sys
import time
import tomllib
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SEEN_FILE = HERE / "seen.json"
USER_AGENT = "Mozilla/5.0 (compatible; ForumRadar/1.0)"
try:
    TZ = ZoneInfo("Europe/Athens")
except Exception:  # Windows χωρίς tzdata: τοπική ώρα του υπολογιστή
    TZ = None


# ---------------------------------------------------------------- κείμενο

LATIN2GREEK = str.maketrans("abehikmnoptxyz", "αβεηικμνοπτχυζ")
GREEK = re.compile(r"[α-ω]")


def _fix_mixed(word: str) -> str:
    # "aμορτισερ" (λατινικό a) → "αμορτισερ": λέξεις με ελληνικά και λατινικά γράμματα
    return word.translate(LATIN2GREEK) if GREEK.search(word) and re.search(r"[a-z]", word) else word


def norm(text: str) -> str:
    """Πεζά, χωρίς τόνους, ς→σ, χωρίς HTML — για σύγκριση λέξεων."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.lower().replace("ς", "σ")
    return re.sub(r"\w+", lambda m: _fix_mixed(m.group(0)), text)


def plain(text: str, limit: int = 400) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def compile_terms(terms: list[str]) -> list[tuple[str, re.Pattern]]:
    out = []
    for t in terms:
        n = norm(t).strip()
        if n:
            out.append((t, re.compile(r"(?<!\w)" + re.escape(n) + r"(?!\w)")))
    return out


# ---------------------------------------------------------------- ροές

def fetch(url: str) -> bytes:
    if url.startswith(("http://", "https://")):
        # κωδικοποίηση ελληνικών χαρακτήρων στο URL
        parts = urllib.parse.urlsplit(url)
        url = urllib.parse.urlunsplit(parts._replace(
            path=urllib.parse.quote(parts.path, safe="/%"),
            query=urllib.parse.quote(parts.query, safe="=&+%:/,")))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(data: bytes) -> list[dict]:
    root = ET.fromstring(data)
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        rec = {"title": "", "link": "", "text": "", "date": None, "id": ""}
        for ch in el:
            t = local(ch.tag)
            if t == "title":
                rec["title"] = "".join(ch.itertext())
            elif t == "link":
                href = ch.get("href")
                if href and (ch.get("rel") in (None, "alternate")):
                    rec["link"] = rec["link"] or href
                elif ch.text:
                    rec["link"] = rec["link"] or ch.text.strip()
            elif t in ("description", "summary", "content", "encoded"):
                txt = "".join(ch.itertext())
                if len(txt) > len(rec["text"]):
                    rec["text"] = txt
            elif t in ("pubDate", "published", "updated", "date"):
                rec["date"] = rec["date"] or parse_date(ch.text)
            elif t in ("guid", "id"):
                rec["id"] = (ch.text or "").strip()
        # Google Alerts βάζει το πραγματικό link μέσα σε google.com/url?url=...
        if "google.com/url" in rec["link"]:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(rec["link"]).query)
            rec["link"] = (q.get("url") or q.get("q") or [rec["link"]])[0]
        rec["id"] = rec["id"] or rec["link"] or rec["title"]
        items.append(rec)
    return items



# ---------------------------------------------------------------- κατάλογος προϊόντων

POSITION = {"εμπροσ", "πισω", "αριστερα", "δεξια", "ανω", "κατω", "αξονασ", "εσωτερικοσ",
            "εξωτερικοσ", "εσωτερικο", "εξωτερικο", "κιτ", "σετ", "kit", "set", "με", "και",
            "χωρισ", "για", "του", "τησ", "των", "το", "η", "ο", "τα", "οι"}
GENERIC_HEAD = {"βαση", "μοτερ", "αντλια", "σωληνασ", "σωληνακι", "φιλτρο", "αισθητηρασ",
                "διακοπτησ", "διακοπτεσ", "καλυμμα", "ψυγειο", "βαλβιδα", "δοχειο", "λαστιχο",
                "μπεκ", "αντισταση", "μπρατσο", "μοχλοσ", "ντιζα", "καπακι", "ταπα", "πεταλουδα",
                "μετρητησ", "φλαντζα", "συνδετικη", "κεντρικοσ", "λαβη", "χερουλι", "τροχαλια",
                "ρουλεμαν", "βιδα", "παξιμαδι", "κλιπ", "ελατηριο", "δισκοσ", "πλακα", "θηκη",
                "κουμπι", "μονάδα", "μοναδα", "τσιμουχα", "συνδεσμοσ", "αεροσακοσ", "καλωδιο"}
ROMAN = {"1": "i", "2": "ii", "3": "iii", "4": "iv", "5": "v", "6": "vi", "7": "vii", "8": "viii"}


def _stem(w: str) -> str:
    return w[:-2] if len(w) > 6 else (w[:-1] if len(w) > 4 else w)


def family_of(title: str) -> tuple[str, str] | None:
    """Από τον τίτλο βγάζει το είδος ανταλλακτικού: (εμφάνιση, μοτίβο regex)."""
    head = title.split(" για ")[0] if " για " in title else re.split(r"\s[A-Z]{3,}", title)[0]
    head = re.sub(r"\([^)]*\)", " ", head)
    words = []
    for raw in re.split(r"[\s/,]+", head):
        n = norm(raw).strip(".-:")
        if not n or n in POSITION or not GREEK.search(n) or re.search(r"\d", n):
            continue
        words.append((raw.strip(".,:-"), n))
    if not words:
        return None
    take = words[:2] if words[0][1] in GENERIC_HEAD and len(words) > 1 else words[:1]
    if len(take[0][1]) < 4:
        return None
    display = " ".join(w for w, _ in take)
    display = display[0].upper() + display[1:]
    pat = r"\s+(?:του\s+|τησ\s+|των\s+)?".join(re.escape(_stem(n)) + r"\w*" for _, n in take)
    return display, r"(?<!\w)" + pat


def load_catalog(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        import openpyxl
    except ImportError:
        print("  ! Για τον κατάλογο χρειάζεται: pip install openpyxl")
        return None
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb["ΤΙΤΛΟΙ"] if "ΤΙΤΛΟΙ" in wb.sheetnames else wb.worksheets[0]
    fams: dict[str, dict] = {}
    products, oe = [], {}
    for row in ws.iter_rows(values_only=True):
        if not row or len(row) < 2 or not row[0] or not row[1]:
            continue
        sku, title = str(row[0]).strip(), str(row[1]).strip()
        f = family_of(title)
        if not f:
            continue
        fam = fams.setdefault(f[1], {"display": f[0], "pattern": f[1], "count": 0})
        fam["count"] += 1
        vehicle = title.split(" για ", 1)[1] if " για " in title else title
        vehicle = norm(vehicle.split("(OE")[0])
        products.append((sku, title, f[1], vehicle))
        m = re.search(r"\(OE:\s*(?:\(OE:\s*)?([^)]*)\)", title)
        if m:
            for code in re.split(r"[,;/ ]+", m.group(1)):
                k = re.sub(r"[^0-9a-z]", "", code.lower())
                if len(k) >= 6 and re.search(r"\d", k):
                    oe.setdefault(k, []).append(len(products) - 1)
    keep = {k: v for k, v in fams.items() if v["count"] >= 3}
    for v in keep.values():
        v["re"] = re.compile(v["pattern"])
    print(f"  ✓ Κατάλογος: {len(products)} προϊόντα, {len(keep)} είδη ανταλλακτικών, {len(oe)} κωδικοί OE")
    return {"families": keep, "products": products, "oe": oe}


def catalog_match(text: str, raw: str, models: list[str], cat: dict) -> dict:
    """Ποια είδη/προϊόντα του καταλόγου αναφέρονται στη συζήτηση."""
    fams = [k for k, v in cat["families"].items() if v["re"].search(text)]
    # κωδικοί OE γραμμένοι στη συζήτηση
    oe_hits = []
    for tok in re.findall(r"[0-9A-Za-z][0-9A-Za-z .\-]{4,18}[0-9A-Za-z]", raw):
        k = re.sub(r"[^0-9a-z]", "", tok.lower())
        if k in cat["oe"]:
            oe_hits += cat["oe"][k]
    # μοντέλα + γενιά (π.χ. "golf 5" → golf v)
    want = []
    for mdl in models:
        n = norm(mdl)
        g = re.search(re.escape(n) + r"\s*(?:mk\s*)?(\d)(?!\d|\.)", text)
        want.append((n, g.group(1) if g else None))
    prods = []
    if fams and want:
        for i, (sku, title, fam, veh) in enumerate(cat["products"]):
            if fam not in fams:
                continue
            best = 0
            for n, gen in want:
                if re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", veh):
                    best = max(best, 1)
                    if gen and re.search(r"(?<!\w)" + re.escape(n) + r"\s*(?:" + ROMAN.get(gen, gen)
                                         + r"|mk\s*" + gen + r"|" + gen + r")(?!\w)", veh):
                        best = 2
            if best:
                prods.append((best, i))
        sides = [w for w in ("πισω", "εμπροσ", "αριστερ", "δεξι") if re.search(r"(?<!\w)" + w, text)]

        def side_hits(i: int) -> int:
            t = norm(cat["products"][i][1].split(" για ")[0])
            return sum(1 for w in sides if w in t)
        prods.sort(key=lambda x: (-x[0], -side_hits(x[1]), len(cat["products"][x[1]][1])))
        if any(b == 2 for b, _ in prods):
            prods = [x for x in prods if x[0] == 2]
    order = list(dict.fromkeys(oe_hits + [i for _, i in prods]))
    return {"families": [cat["families"][k]["display"] for k in fams],
            "oe": bool(oe_hits), "products": [cat["products"][i] for i in order], "fam_keys": fams}

# ---------------------------------------------------------------- βαθμολογία

def score_item(item: dict, kw: dict, catalog: dict | None = None) -> dict:
    text = norm(item["title"] + " \n " + item["text"])
    brands, models = [], []
    for brand, pats in kw["brands"].items():
        for orig, p in pats:
            if p.search(text):
                if brand not in brands:
                    brands.append(brand)
                if orig not in models:
                    models.append(orig)
    parts = {}
    for cat, pats in kw["parts"].items():
        hits = [orig for orig, p in pats if p.search(text)]
        if hits:
            parts[cat] = hits
    intent = [orig for orig, p in kw["intent"] if p.search(text)]

    # Ετικέτα μοντέλου για το σχέδιο: π.χ. "Golf 5", "Yaris", "BMW E46"
    label = ""
    brand_names = {norm(b) for b in kw["brands"]} | {"vw", "volkswagen", "bmw", "mercedes", "benz", "toyota", "audi"}
    specific = [x for x in models if norm(x) not in brand_names]
    pick = specific[0] if specific else (models[0] if models else "")
    if pick:
        g = re.search(r"(?<!\w)" + re.escape(norm(pick)) + r"\s*(mk\s*\d|\d(?:\.\d)?)(?!\w)", text)
        label = pick + (" " + g.group(1).upper().replace(" ", "") if g else "")
        if re.fullmatch(r"[A-Z]\d+", pick) and brands and brands[0] not in ("Άλλες",):
            label = brands[0] + " " + label

    cm = {"families": [], "oe": False, "products": []}
    if catalog:
        raw = plain(item["title"], 10**6) + " " + plain(item["text"], 10**6)
        cm = catalog_match(text, raw, specific, catalog)
        # είδη του καταλόγου που δεν τα έπιασαν ήδη οι λέξεις του config
        known = " ".join(norm(" ".join(h)) for h in parts.values())
        extra = [f for f in cm["families"] if _stem(norm(f).split()[0]) not in known]
        if extra:
            parts["Από κατάλογο"] = extra

    real_parts = {c: h for c, h in parts.items() if c != "Γενικά"}
    score = (1 if brands else 0) \
        + min(4, 2 * len(real_parts)) + (1 if "Γενικά" in parts else 0) \
        + min(2, len(intent)) + (4 if cm["oe"] else 0) + (1 if cm["products"] else 0)
    return {"score": score, "brands": brands, "models": models, "model_label": label,
            "parts": parts, "intent": intent, "catalog": cm}


# ---------------------------------------------------------------- σχέδια απαντήσεων

TEMPLATES = {
    "Φρένα": "Καλησπέρα. Στο {model} τα φρένα διαφέρουν ανάλογα με κινητήρα και χρονολογία "
             "(διάμετρος δισκόπλακας, με ή χωρίς αισθητήρα φθοράς). Τσέκαρε πρώτα τη διάμετρο "
             "ή δώσε αριθμό πλαισίου στο κατάστημα για να σου πουν τον σωστό κωδικό.",
    "Ανάρτηση": "Καλησπέρα. Αν ο θόρυβος βγαίνει σε λακκούβες, στο {model} συνήθως φταίνε "
                "μπαλάκια, ψαλίδια ή βάσεις αμορτισέρ. Σήκωσέ το και κούνησε τον τροχό πάνω-κάτω "
                "και δεξιά-αριστερά για να δεις πού έχει τζόγο πριν αλλάξεις κάτι.",
    "Τιμόνι": "Καλησπέρα. Αν στο {model} ακούγεται τακ-τακ στη στροφή με κλειδωμένο τιμόνι, "
              "συνήθως είναι το εξωτερικό μπιλιοφόρο. Πρόσεξε αν το αυτοκίνητο έχει ABS, "
              "γιατί αλλάζει ο κωδικός.",
    "Ψύξη": "Καλησπέρα. Στο {model} πριν αλλάξεις κάτι στην ψύξη τσέκαρε και θερμοστάτη και "
            "κολάρα, συχνά πάνε μαζί. Αν η αντλία νερού δουλεύει από τον ιμάντα χρονισμού, "
            "συμφέρει να αλλάξεις και το κιτ.",
    "Ηλεκτρικά": "Καλησπέρα. Στο {model}, αν έχεις διαγνωστικό, δες πρώτα τον κωδικό σφάλματος "
                 "για να μην αλλάζεις κομμάτια στα τυφλά. Ο σωστός κωδικός ανταλλακτικού "
                 "εξαρτάται από τον κινητήρα.",
    "Κινητήρας": "Καλησπέρα. Για το {model} χρειάζεσαι τον κωδικό κινητήρα (γράφεται στην άδεια "
                 "ή πάνω στο μπλοκ), γιατί στην ίδια γενιά υπάρχουν διαφορετικά κιτ.",
    "Φίλτρα": "Καλησπέρα. Για το {model} το σωστό φίλτρο βγαίνει από κινητήρα και χρονολογία. "
              "Αν δεν είσαι σίγουρος, κοίτα τον κωδικό πάνω στο παλιό.",
}
GENERIC = ("Καλησπέρα. Για να βρεις το σωστό ανταλλακτικό στο {model} βοηθάει πολύ ο αριθμός "
           "πλαισίου (VIN) ή ο κωδικός κινητήρα, γιατί στο ίδιο μοντέλο αλλάζουν οι κωδικοί.")


def make_draft(m: dict, store: dict, kw: dict | None = None) -> str:
    model = m["model_label"] or "αυτοκίνητό σου"
    cats = [c for c in m["parts"] if c not in ("Γενικά", "Από κατάλογο")]
    cat = cats[0] if cats else None
    part = m["parts"][cat][0] if cat else ""
    if not cat and m["parts"].get("Από κατάλογο"):
        part = m["parts"]["Από κατάλογο"][0]
        # ταίριαξε το είδος του καταλόγου σε κατηγορία για το σωστό κείμενο
        for c, pats in (kw or {}).get("parts", {}).items():
            if any(p.search(norm(part)) for _, p in pats):
                cat = c
                break
    body = TEMPLATES.get(cat, GENERIC).format(model=model)
    cm = m.get("catalog") or {}
    if cm.get("oe") and cm.get("products"):
        q = cm["products"][0][0]  # ακριβής κωδικός προϊόντος
    else:
        q = " ".join(x for x in (part, m["model_label"]) if x).strip()
    link = store["search_url"].format(q=urllib.parse.quote(q)) if q else ""
    tail = f"\n\nΑν θες να δεις τι υπάρχει για το μοντέλο σου: {link}" if link else ""
    disclose = f"\n(Για να είμαι ξεκάθαρος: είμαι από το {store['name']}.)"
    return body + tail + disclose


# ---------------------------------------------------------------- αναφορά

def manual_links(manual: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    q = urllib.parse.quote
    sections = []
    searches = manual.get("searches", [])
    groups = manual.get("facebook_groups", [])
    tags = manual.get("hashtags", [])
    fb = [(f"{g}: «{s}»", f"https://www.facebook.com/groups/{g}/search/?q={q(s)}")
          for g in groups for s in searches]
    fb += [(f"Όλο το Facebook: «{s}»", f"https://www.facebook.com/search/posts/?q={q(s)}")
           for s in searches]
    sections.append(("Facebook", fb))
    sections.append(("X / Twitter (πιο πρόσφατα)",
                     [(f"«{s}»", f"https://x.com/search?q={q(s)}&f=live") for s in searches]))
    sections.append(("TikTok", [(f"«{s}»", f"https://www.tiktok.com/search?q={q(s)}") for s in searches]
                     + [(f"#{t}", f"https://www.tiktok.com/tag/{q(t)}") for t in tags]))
    sections.append(("Instagram", [(f"#{t}", f"https://www.instagram.com/explore/tags/{q(t)}/")
                                   for t in tags]))
    sections.append(("car.gr – Ερωτήσεις & απαντήσεις",
                     [("Άνοιγμα", "https://www.car.gr/qna/")]))
    return sections


CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f5f6f8;color:#1d2330;margin:0}
.wrap{max-width:860px;margin:0 auto;padding:18px}
h1{font-size:22px;margin:4px 0}h2{font-size:17px;margin:28px 0 10px}
.sub{color:#5b6475;font-size:14px}
.card{background:#fff;border:1px solid #e3e6ec;border-radius:10px;padding:14px 16px;margin:12px 0}
.card h3{font-size:16px;margin:0 0 6px}.card h3 a{color:#1d2330;text-decoration:none}
.meta{font-size:13px;color:#5b6475;margin-bottom:8px}
.tag{display:inline-block;background:#eef1f6;border-radius:4px;padding:1px 7px;margin:2px 4px 2px 0;font-size:12px}
.new{background:#1f7a4d;color:#fff}.score{background:#24427a;color:#fff}
.snip{font-size:14px;color:#333;margin:8px 0}
textarea{width:100%;box-sizing:border-box;min-height:170px;font-family:inherit;font-size:14px;line-height:1.45;border:1px solid #cfd5df;border-radius:6px;padding:8px}
.btns{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}
.btns a,.btns button{font-family:inherit;font-size:14px;padding:7px 12px;border-radius:6px;border:1px solid #24427a;background:#24427a;color:#fff;text-decoration:none;cursor:pointer}
.btns button.sec{background:#fff;color:#24427a}
.warn{background:#fff8e6;border-color:#f0d58a}
.cat{background:#eef7f1;border:1px solid #cde6d6;border-radius:6px;padding:8px 10px;margin:8px 0;font-size:13px}
.cat ul{margin:4px 0 0;padding-left:18px}
ul.links{columns:2;padding-left:18px;font-size:14px}ul.links li{margin:3px 0}
@media(max-width:600px){ul.links{columns:1}}
"""
JS = """
function cp(id,btn){const t=document.getElementById(id);t.select();
 (navigator.clipboard?navigator.clipboard.writeText(t.value):Promise.reject()).catch(()=>document.execCommand('copy'));
 btn.textContent='Αντιγράφηκε ✓';setTimeout(()=>btn.textContent='Αντιγραφή απάντησης',1500)}
"""


def cat_html(m: dict) -> str:
    prods = (m.get("catalog") or {}).get("products") or []
    if not prods:
        return ""
    e = html.escape
    head = "Ακριβής κωδικός OE από τον κατάλογό σου" if m["catalog"]["oe"] else "Ταιριάζουν από τον κατάλογό σου"
    items = "".join(f"<li><b>{e(s)}</b> — {e(t)}</li>" for s, t, *_ in prods[:5])
    more = f"<li>…και άλλα {len(prods) - 5}</li>" if len(prods) > 5 else ""
    return f"<div class='cat'><b>{head}:</b><ul>{items}{more}</ul></div>"


def render(results, errors, manual, settings, now) -> str:
    e = html.escape
    out = [f"<!DOCTYPE html><html lang='el'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<meta name='robots' content='noindex'>"
           f"<title>Ραντάρ συζητήσεων</title><style>{CSS}</style></head><body><div class='wrap'>",
           f"<h1>Ραντάρ συζητήσεων</h1><div class='sub'>{now:%d/%m/%Y %H:%M} · "
           f"{len(results)} συζητήσεις τις τελευταίες {settings['days_back']} μέρες · "
           f"{sum(r['new'] for r in results)} νέες</div>",
           "<div class='card warn'>Οι απαντήσεις είναι <b>σχέδια</b>. Διάβασε τη συζήτηση, "
           "διόρθωσε το κείμενο και πόσταρε με τον δικό σου λογαριασμό. Βάζε link μόνο εκεί "
           "όπου το επιτρέπουν οι κανόνες του forum ή της ομάδας.</div>"]
    if errors:
        out.append("<div class='card warn'><b>Πηγές που δεν διαβάστηκαν:</b><ul>"
                   + "".join(f"<li>{e(n)}: {e(msg)}</li>" for n, msg in errors) + "</ul></div>")
    out.append("<h2>Συζητήσεις</h2>")
    if not results:
        out.append("<div class='card'>Τίποτα σχετικό αυτή τη φορά.</div>")
    for i, r in enumerate(results):
        it, m = r["item"], r["match"]
        tags = "".join(f"<span class='tag'>{e(b)}</span>" for b in m["brands"])
        tags += "".join(f"<span class='tag'>{e(c)}: {e(', '.join(h))}</span>"
                        for c, h in m["parts"].items())
        badge = "<span class='tag new'>ΝΕΟ</span>" if r["new"] else ""
        when = it["date"].astimezone(TZ).strftime("%d/%m %H:%M") if it["date"] else "—"
        out.append(
            f"<div class='card'><h3><a href='{e(it['link'])}' target='_blank'>{e(plain(it['title'], 160))}</a></h3>"
            f"<div class='meta'>{badge}<span class='tag score'>βαθμός {m['score']}</span>"
            f"{e(r['source'])} · {when}</div><div>{tags}</div>"
            f"<div class='snip'>{e(plain(it['text']))}</div>"
            f"{cat_html(m)}"
            f"<textarea id='d{i}'>{e(r['draft'])}</textarea>"
            f"<div class='btns'><a href='{e(it['link'])}' target='_blank'>Άνοιγμα συζήτησης</a>"
            f"<button class='sec' onclick=\"cp('d{i}',this)\">Αντιγραφή απάντησης</button></div></div>")
    out.append("<h2>Χειροκίνητος έλεγχος (με τον δικό σου λογαριασμό)</h2>")
    for title, links in manual_links(manual):
        if not links:
            continue
        out.append(f"<div class='card'><b>{e(title)}</b><ul class='links'>"
                   + "".join(f"<li><a href='{e(u)}' target='_blank'>{e(t)}</a></li>" for t, u in links)
                   + "</ul></div>")
    out.append(f"</div><script>{JS}</script></body></html>")
    return "".join(out)


# ---------------------------------------------------------------- ειδοποιήσεις

def notify(results: list, cfg: dict) -> None:
    """Στέλνει ειδοποίηση (ntfy) για κάθε ΝΕΑ συζήτηση — εμφανίζεται κάτω δεξιά στα Windows."""
    conf = cfg.get("notify") or {}
    topic = conf.get("ntfy_topic")
    new = [r for r in results if r["new"]]
    if not topic or not new:
        return
    server = conf.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    report_url = conf.get("report_url", "")
    limit = int(conf.get("max_per_run", 5))

    def send(title: str, message: str, click: str) -> None:
        payload = {"topic": topic, "title": title, "message": message, "tags": ["wrench"]}
        if click:
            payload["click"] = click
        if report_url:
            payload["actions"] = [{"action": "view", "label": "Αναφορά", "url": report_url}]
        req = urllib.request.Request(server, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as ex:  # noqa: BLE001 — η ειδοποίηση δεν πρέπει να σταματά το ραντάρ
            print(f"  ! Ειδοποίηση απέτυχε: {ex}")

    for r in new[:limit]:
        it, m = r["item"], r["match"]
        msg = plain(it["title"], 200)
        prods = (m.get("catalog") or {}).get("products") or []
        if prods:
            msg += f"\nΈχεις: {prods[0][0]}" + (f" (+{len(prods) - 1} ακόμα)" if len(prods) > 1 else "")
        send("Κάποιος ψάχνει ανταλλακτικό", msg, it["link"])
    if len(new) > limit:
        send("Ραντάρ: κι άλλες συζητήσεις", f"Υπάρχουν ακόμα {len(new) - limit} νέες συζητήσεις.", report_url)
    print(f"  ✓ Ειδοποιήσεις: {min(len(new), limit)}")


# ---------------------------------------------------------------- κύριο

def main() -> int:
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "config.toml"
    cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    settings = {"days_back": 7, "min_score": 3, "report_file": "anafora.html",
                "catalog_file": "catalog.xlsx", "require_vehicle": True, **cfg.get("settings", {})}
    kwc = cfg["keywords"]
    kw = {"brands": {b: compile_terms(t) for b, t in kwc["brands"].items()},
          "parts": {c: compile_terms(t) for c, t in kwc["parts"].items()},
          "intent": compile_terms(kwc.get("intent", []))}

    cat = load_catalog(HERE / settings["catalog_file"]) if settings["catalog_file"] else None
    seen = set(json.loads(SEEN_FILE.read_text(encoding="utf-8"))) if SEEN_FILE.exists() else set()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=settings["days_back"])
    results, errors, ids = [], [], set()

    for n, feed in enumerate(cfg.get("feeds", [])):
        if n:
            time.sleep(5)  # παύση ανάμεσα στις πηγές, για να μη μας κόβουν
        name = feed.get("name", feed["url"])
        try:
            items = parse_feed(fetch(feed["url"]))
        except Exception as ex:  # noqa: BLE001 — μία χαλασμένη πηγή δεν σταματά τις άλλες
            errors.append((name, f"{type(ex).__name__}: {ex}"))
            print(f"  ✗ {name}: {ex}")
            continue
        kept = 0
        for it in items:
            if it["date"] and it["date"] < cutoff:
                continue
            if it["id"] in ids:
                continue
            m = score_item(it, kw, cat)
            if m["score"] < settings["min_score"]:
                continue
            if settings["require_vehicle"] and not (m["brands"] or m["catalog"]["oe"]):
                continue  # χωρίς μάρκα/μοντέλο ή κωδικό δεν είναι συζήτηση για αυτοκίνητο
            ids.add(it["id"])
            results.append({"item": it, "match": m, "source": name,
                            "new": it["id"] not in seen,
                            "draft": make_draft(m, cfg["store"], kw)})
            kept += 1
        print(f"  ✓ {name}: {len(items)} αναρτήσεις, {kept} σχετικές")

    results.sort(key=lambda r: (r["new"], r["match"]["score"],
                                r["item"]["date"] or cutoff), reverse=True)
    report = HERE / settings["report_file"]
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render(results, errors, cfg.get("manual", {}), settings, now.astimezone(TZ)),
                      encoding="utf-8")
    SEEN_FILE.write_text(json.dumps(sorted(seen | ids), ensure_ascii=False), encoding="utf-8")
    notify(results, cfg)
    print(f"\nΈτοιμο: {len(results)} συζητήσεις ({sum(r['new'] for r in results)} νέες) → {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
