"""
Ραντάρ συζητήσεων για ανταλλακτικά αυτοκινήτων.

Διαβάζει ροές RSS/Atom (fora, Reddit, Google Alerts), κρατά όσες συζητήσεις
μοιάζουν με "ψάχνω ανταλλακτικό για X", και φτιάχνει μια σελίδα HTML με:
  - τη λίστα συζητήσεων και link για να τις ανοίξεις,
  - ένα ΣΧΕΔΙΟ απάντησης για καθεμία, που το διορθώνεις και το ποστάρεις εσύ,
  - έτοιμα links αναζήτησης για Facebook ομάδες, Instagram, TikTok, X, car.gr.

Δεν ποστάρει τίποτα μόνο του. Χρειάζεται μόνο Python 3.11+ (χωρίς πρόσθετα).
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

def norm(text: str) -> str:
    """Πεζά, χωρίς τόνους, ς→σ, χωρίς HTML — για σύγκριση λέξεων."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text.lower().replace("ς", "σ")


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


# ---------------------------------------------------------------- βαθμολογία

def score_item(item: dict, kw: dict) -> dict | None:
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

    real_parts = {c: h for c, h in parts.items() if c != "Γενικά"}
    score = (1 if brands else 0) \
        + min(4, 2 * len(real_parts)) + (1 if "Γενικά" in parts else 0) \
        + min(2, len(intent))
    return {"score": score, "brands": brands, "models": models, "model_label": label,
            "parts": parts, "intent": intent}


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


def make_draft(m: dict, store: dict) -> str:
    model = m["model_label"] or "αυτοκίνητό σου"
    cats = [c for c in m["parts"] if c != "Γενικά"]
    cat = cats[0] if cats else None
    part = m["parts"][cat][0] if cat else ""
    body = TEMPLATES.get(cat, GENERIC).format(model=model)
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
ul.links{columns:2;padding-left:18px;font-size:14px}ul.links li{margin:3px 0}
@media(max-width:600px){ul.links{columns:1}}
"""
JS = """
function cp(id,btn){const t=document.getElementById(id);t.select();
 (navigator.clipboard?navigator.clipboard.writeText(t.value):Promise.reject()).catch(()=>document.execCommand('copy'));
 btn.textContent='Αντιγράφηκε ✓';setTimeout(()=>btn.textContent='Αντιγραφή απάντησης',1500)}
"""


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


# ---------------------------------------------------------------- κύριο

def main() -> int:
    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "config.toml"
    cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    settings = {"days_back": 7, "min_score": 3, "report_file": "anafora.html", **cfg.get("settings", {})}
    kwc = cfg["keywords"]
    kw = {"brands": {b: compile_terms(t) for b, t in kwc["brands"].items()},
          "parts": {c: compile_terms(t) for c, t in kwc["parts"].items()},
          "intent": compile_terms(kwc.get("intent", []))}

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
            m = score_item(it, kw)
            if m["score"] < settings["min_score"]:
                continue
            ids.add(it["id"])
            results.append({"item": it, "match": m, "source": name,
                            "new": it["id"] not in seen,
                            "draft": make_draft(m, cfg["store"])})
            kept += 1
        print(f"  ✓ {name}: {len(items)} αναρτήσεις, {kept} σχετικές")

    results.sort(key=lambda r: (r["new"], r["match"]["score"],
                                r["item"]["date"] or cutoff), reverse=True)
    report = HERE / settings["report_file"]
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render(results, errors, cfg.get("manual", {}), settings, now.astimezone(TZ)),
                      encoding="utf-8")
    SEEN_FILE.write_text(json.dumps(sorted(seen | ids), ensure_ascii=False), encoding="utf-8")
    print(f"\nΈτοιμο: {len(results)} συζητήσεις ({sum(r['new'] for r in results)} νέες) → {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
