"""Digitising the medicines list out of the page text.

EDLIZ is a book of tables. A row reads

    Medicine        Codes   Adult dose      Frequency     Duration
    cotrimoxazole   po C V_ 1920mg          3 times a day 21 days

OCR flattens that to one line:

    cotrimoxazole po C V_ 1920mg 3 times a day 21 days

Two shapes cover almost every row in the book:

    <name> <route> ... <dose> ...        (treatment tables, name is lowercase)
    <name> <dose><form>                  (dosage tables, e.g. "Amoxicillin 30mg/kg/dose")

The parser accepts a line when the token after the name is a route, a dosage
form, or something containing a digit. That single condition is what keeps
prose out: in a sentence the word after the subject is an ordinary word.

Nothing here invents a dose. The row is stored with its page as the citation,
and the page remains the authority.
"""

from __future__ import annotations

import re
import sqlite3

FORMS = {
    "tablet", "tablets", "tab", "tabs", "capsule", "capsules", "injection", "inj",
    "syrup", "suspension", "infusion", "solution", "cream", "ointment", "drops",
    "suppository", "suppositories", "pessary", "pessaries", "inhaler", "patch",
    "gel", "lotion", "powder", "sachet", "vial", "ampoule", "elixir", "lozenge",
    "spray", "pess", "syr", "susp",
}
ROUTES = {
    "po", "iv", "im", "sc", "pr", "pv", "top", "inh", "neb", "ng", "rectal",
    "oral", "iv/sc", "iv/im", "iv/po", "im/po", "iv/im/po", "sl", "vag",
    "lv",  # OCR of "iv"
}
UNITS = ("mg", "ml", "mcg", "g", "iu", "mmol", "meq", "%", "w/v")

NOT_MEDICINES = {
    "medicine", "medicines", "dosage", "dose", "doses", "duration", "frequency",
    "table", "chapter", "section", "note", "notes", "caution", "warning", "warn",
    "adult", "adults", "child", "children", "paediatric", "pediatric", "paeds",
    "infant", "infants", "neonate", "neonates", "patient", "patients", "route",
    "code", "codes", "level", "primary", "district", "provincial", "central",
    "hospital", "clinic", "treatment", "management", "refer", "referral",
    "investigation", "investigations", "diagnosis", "clinical", "edliz",
    "essential", "list", "guidelines", "total", "daily", "every", "single",
    "once", "twice", "three", "four", "five", "times", "day", "days", "week",
    "weeks", "month", "months", "hour", "hours", "stat", "then", "and", "or",
    "with", "without", "until", "review", "see", "give", "start", "stop",
    "continue", "avoid", "use", "loading", "maintenance", "if", "add", "also",
    "first", "second", "third", "line", "mild", "moderate", "severe", "for",
    "the", "this", "that", "these", "those", "a", "an", "in", "on", "at", "to",
    "of", "is", "are", "be", "as", "by", "may", "can", "should", "must",
    "but", "not", "less", "than", "more", "include", "including", "remove",
    "available", "lower", "higher", "tidal", "volumes", "maximum", "minimum",
    "plus", "per", "kg", "up", "down", "over", "under", "between", "each",
}

_BULLET = re.compile(r"^[»«*•\-–—]+\s*")
_NAME_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9'’./()\-]{1,30}$")
_DOSE = re.compile(r"\d")
_STRENGTH = re.compile(
    r"(\d[\d.,]*\s*(?:" + "|".join(map(re.escape, UNITS)) + r"))\b", re.I)

# strength + form, e.g. "125mg/5ml syrup", "1g injection", "500mg tablet"
_STRENGTH_FORM = re.compile(
    r"(\d[\d.,]*\s*(?:" + "|".join(map(re.escape, UNITS)) + r")\b"
    r"(?:\s*/\s*\d[\d.,]*\s*(?:mg|ml|g|mcg))?)"
    r"\s*(" + "|".join(sorted(FORMS)) + r")?\b",
    re.I,
)


def name_key(name: str) -> str:
    key = re.sub(r"\(.*?\)", " ", name.lower())
    key = re.sub(r"[^a-z0-9 ]+", " ", key)
    return re.sub(r"\s+", " ", key).strip()


def _is_name_token(token: str) -> bool:
    low = token.lower().strip(".,;:")
    if low in NOT_MEDICINES or low in ROUTES or low in FORMS:
        return False
    if _DOSE.search(low):
        return False
    return bool(_NAME_TOKEN.match(token))


def _strip_codes(tokens: list[str]) -> list[str]:
    """Drop the `C V_` level columns and route fragments OCR folds into names."""
    while tokens:
        last = tokens[-1]
        low = last.lower()
        if (re.fullmatch(r"[A-Z]{1,3}_?", last)
                or last in {"V_", "C_"}
                or low in ROUTES
                or re.fullmatch(r"[a-z]{2,3}(?:/[a-z]{2,3})+", low)):
            tokens.pop()
            continue
        break
    return tokens


def _clean(name: str) -> str | None:
    name = name.strip(" .,:;-")
    if len(name) < 4 or len(re.sub(r"[^A-Za-z]", "", name)) < 4:
        return None
    if not re.search(r"[aeiouyAEIOUY]", name):
        return None
    words = name.split()
    if len(words) > 3:
        return None
    # "acid 250mg" is a fragment of a longer name ("Folic acid"), never a
    # medicine on its own. Only blocks the single-word form, so "Folic acid"
    # and "Calcium gluconate" still pass.
    if len(words) == 1 and words[0].lower() in {
            "acid", "sodium", "potassium", "calcium", "magnesium", "iron",
            "water", "saline", "solution", "injection", "tablet", "capsule",
            "syrup", "cream", "ointment", "acetate", "hydrochloride", "citrate",
            "sulfate", "sulphate", "phosphate", "chloride", "tartrate",
            "maleate", "fumarate", "besylate", "mesylate"}:
        return None
    for word in words:
        low = word.lower().strip(".,;:()")
        if low in NOT_MEDICINES or low in ROUTES or low in FORMS:
            return None
        if re.search(r"\d", word) or re.search(r"[=()\[\]]", word):
            return None
        # OCR'd route fragments such as "imiiv", "iv/", "lv" that the route
        # test could not normalise; never a medicine name.
        if re.fullmatch(r"[iIlLvVmMpP/|]{2,6}", word) and not re.search(r"[aeouy]", low):
            return None
    return name


def parse_line(line: str) -> tuple[str, str | None] | None:
    """Return (medicine_name, form_or_strength) for a table row, else None."""
    line = _BULLET.sub("", line.strip())
    line = line.strip(" .|:-")
    if len(line) < 8 or len(line) > 240:
        return None

    tokens = line.split()

    def strength_at(i: int) -> bool:
        if i >= len(tokens):
            return False
        if _STRENGTH.match(tokens[i]):
            return True
        return i + 1 < len(tokens) and bool(_STRENGTH.match(f"{tokens[i]} {tokens[i + 1]}"))

    # Shape A, the treatment tables: <name...> <route> ...
    # A few real rows carry no dose at all ("artemether-lumefantrine po ... see
    # table"), so the route -- not a digit -- is the gate here.
    def is_route(t: str) -> bool:
        return t.lower().strip(".,;:/|") in ROUTES

    route_at = next((i for i, t in enumerate(tokens) if i >= 1 and is_route(t)), None)
    if route_at is not None and len(line) <= 90 and all(_is_name_token(t) for t in tokens[:route_at]):
        name = _clean(" ".join(_strip_codes(tokens[:route_at])))
        if name:
            form = None
            m = _STRENGTH_FORM.search(line[len(" ".join(tokens[:route_at])):])
            if m:
                form = re.sub(r"\s+", " ", f"{m.group(1)} {m.group(2) or ''}").strip()
            return name, form

    # Shape B, the dosage tables: <Capitalised name...> <strength>
    if not _DOSE.search(line):
        return None
    anchor = next((i for i in range(1, min(len(tokens), 5)) if strength_at(i)), None)
    if anchor is not None and tokens[0][:1].isupper():
        name = _clean(" ".join(_strip_codes(tokens[:anchor])))
        if name:
            m = _STRENGTH_FORM.match(" ".join(tokens[anchor:]))
            form = re.sub(r"\s+", " ", f"{m.group(1)} {m.group(2) or ''}").strip() if m else None
            return name, form

    return None


def extract(conn: sqlite3.Connection, corpus_id: int) -> dict[str, int]:
    """Walk the stored pages and populate the medicines table."""
    conn.execute("DELETE FROM medicines WHERE corpus_id = ?", (corpus_id,))
    seen: set[tuple[str, int]] = set()
    rows: list[tuple] = []

    for page in conn.execute(
        "SELECT page, heading, text FROM pages WHERE corpus_id = ? ORDER BY page",
        (corpus_id,),
    ):
        for raw_line in page["text"].splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parsed = parse_line(line)
            if not parsed:
                continue
            name, form = parsed
            key = name_key(name)
            if not key or (key, page["page"]) in seen:
                continue
            seen.add((key, page["page"]))
            rows.append((corpus_id, name, key, form, page["heading"], page["page"],
                         line[:300]))

    conn.executemany(
        "INSERT INTO medicines (corpus_id, name, name_key, form, section, page, raw) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    distinct = conn.execute(
        "SELECT COUNT(DISTINCT name_key) FROM medicines WHERE corpus_id = ?", (corpus_id,)
    ).fetchone()[0]
    return {"rows": len(rows), "distinct_medicines": distinct}


def search(conn: sqlite3.Connection, corpus_id: int, query: str, limit: int = 20) -> list[dict]:
    """Name search, prefix-friendly, best match first."""
    key = name_key(query)
    if not key:
        return []
    rows = conn.execute(
        "SELECT name, name_key, MAX(form) AS form, MAX(section) AS section, "
        "MIN(page) AS page, COUNT(*) AS hits "
        "FROM medicines WHERE corpus_id = ? AND name_key LIKE ? "
        "GROUP BY name_key ORDER BY (name_key = ?) DESC, "
        "(name_key LIKE ?) DESC, hits DESC, name LIMIT ?",
        (corpus_id, f"%{key}%", key, f"{key}%", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def top(conn: sqlite3.Connection, corpus_id: int, letter: str = "", limit: int = 200) -> list[dict]:
    sql = ("SELECT name, name_key, MAX(form) AS form, MAX(section) AS section, "
           "MIN(page) AS page, COUNT(*) AS hits "
           "FROM medicines WHERE corpus_id = ?")
    args: list = [corpus_id]
    if letter and letter.isalpha():
        sql += " AND name_key LIKE ?"
        args.append(f"{letter.lower()}%")
    sql += " GROUP BY name_key ORDER BY name_key LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args).fetchall()]
