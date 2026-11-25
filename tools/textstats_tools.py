"""Textstatistik: Zeichen, Wörter, Sätze, Absätze, Lesezeit.

Zaehlt deterministisch im Code statt im Modell. Wichtig fuer Redaktionsvorgaben:
max. 45 Zeichen Ueberschrift, 250 Zeichen Teaser, 2.200 Zeichen Instagram,
300 Grapheme Bluesky, 3.000 Zeichen LinkedIn.
"""

import re
import os
import unicodedata
import logging

log = logging.getLogger("toolbox.textstats")

# Woerter: Buchstaben, Ziffern, Apostroph im Wort (z.B. "geht's"), Bindestrich
WORD_RE = re.compile(r"[\w\u00C0-\u024F\u1E00-\u1EFF]+(?:['\u2019-][\w\u00C0-\u024F\u1E00-\u1EFF]+)*", re.UNICODE)

# Satzende: . ! ? gefolgt von Whitespace oder Textende
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Abkuerzungs-Bausteine: Endet ein Fragment auf einem davon, wird weiter gesammelt.
# Abgedeckt sind die ueblichen deutschen Kurzformen, jeweils mit Punkt.
ABBREVIATIONS = {
    # mehrteilige, deren erster Teil allein steht
    "z.", "d.", "u.", "i.", "o.", "v.", "s.", "b.",
    # einteilige
    "bzw.", "ca.", "vgl.", "sog.", "ggf.", "evtl.", "inkl.", "exkl.",
    "max.", "min.", "mio.", "mrd.", "tsd.", "str.", "usw.", "etc.",
    "nr.", "abs.", "art.", "prof.", "dr.", "resp.", "ff.", "bzgl.",
    "jan.", "feb.", "mrz.", "apr.", "jun.", "jul.", "aug.", "sep.",
    "okt.", "nov.", "dez.",
}

# Einpunktige Kurzformen, die nur als Kombination eine Abkuerzung sind.
# "z." allein ist keine – es zaehlt nur, wenn ein einzelner Buchstabe folgt.
_SINGLE_LETTER_PAIR_RE = re.compile(r"\b[a-z]\.$", re.IGNORECASE)

# Deutsche Silbentrennung: grobe Heuristik ueber Vokalgruppen.
VOWELS = set("aeiouyäöüAEIOUYÄÖÜ")


class ToolError(Exception):
    """Fehler, den das Modell lesen und korrigieren kann."""


def _graphemes(text: str) -> int:
    """Graphem-Zahl (Bluesky zaehlt Grapheme, nicht Zeichen).

    Kombinierende Zeichen (Akzente) und Variation Selectors zaehlen als Teil
    des vorherigen Zeichens. Emoji-ZWJ-Sequenzen (Familie, Berufe) zaehlen als eins.
    """
    count = 0
    skip_next = False
    for ch in text:
        if skip_next:
            skip_next = False
            if ch == "\u200d":
                continue
            count += 1
            continue
        if ch == "\u200d":
            # ZWJ verbindet das naechste Zeichen mit dem vorigen
            skip_next = True
            continue
        if unicodedata.category(ch).startswith("M") or ch in ("\ufe0f", "\ufe0e"):
            continue
        count += 1
    return count


def _words(text: str) -> list[str]:
    return WORD_RE.findall(text)


def _ends_on_abbreviation(fragment: str) -> bool:
    """True, wenn das Fragment auf einer Abkuerzung endet (kein Satzende)."""
    tail = fragment.rstrip().lower()
    parts = tail.split()
    if not parts:
        return False
    last = parts[-1]
    # Einteilige Abkuerzung wie "dr." oder "bzw."
    if last in ABBREVIATIONS:
        return True
    # Zweiteilige Form "z. b." -> prueft das vorletzte Token "z." plus Einzelbuchstabe
    if _SINGLE_LETTER_PAIR_RE.match(last) and len(parts) >= 2 and parts[-2] in ABBREVIATIONS:
        return True
    # Form "z.b." (ohne Leerzeichen)
    if re.fullmatch(r"[a-z]\.[a-z]\.", last):
        return True
    return False


def _sentences(text: str) -> int:
    """Satzanzahl; Abkuerzungen und Aufzaehlungspunkte zaehlen nicht."""
    if not text.strip():
        return 0

    raw = [p for p in SENTENCE_SPLIT_RE.split(text.strip()) if p.strip()]
    merged: list[str] = []
    buffer = ""
    for part in raw:
        buffer = (buffer + " " + part).strip() if buffer else part
        if _ends_on_abbreviation(buffer):
            continue
        merged.append(buffer)
        buffer = ""
    if buffer.strip():
        merged.append(buffer)

    return max(len(merged), 1)


def _syllables(word: str) -> int:
    """Grobe Silbenzahl eines Wortes (deutsche Heuristik)."""
    w = word.lower()
    if not w:
        return 0
    groups = 0
    in_vowel = False
    for ch in w:
        if ch in VOWELS:
            if not in_vowel:
                groups += 1
                in_vowel = True
        else:
            in_vowel = False
    # Stummes e am Ende: "Woche" -> 2, nicht 3
    if w.endswith("e") and groups > 1:
        groups -= 1
    return max(1, groups)


def _reading_time(words: int, syllables: int) -> dict:
    """Lesezeit nach zwei gängigen Massen (deutsch ~ 200 Woerter/Min)."""
    return {
        "woerter_pro_minute_200": round(words / 200, 1) if words else 0.0,
        "vorlesen_minuten": round(words / 130, 1) if words else 0.0,
    }


def _limit_for(key: str, default: int) -> int:
    """Grenze per Umgebungsvariable ueberschreibbar: TOOLBOX_LIMIT_<KEY>."""
    raw = os.environ.get(f"TOOLBOX_LIMIT_{key.upper()}", "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        log.warning("Ungueltige Grenze in TOOLBOX_LIMIT_%s: %r", key.upper(), raw)
        return default


def _limits(checks: dict) -> list[dict]:
    """Prueft die Laengengrenzen und markiert Ueberschreitungen.

    Voreinstellungen sind redaktionelle Hausnummern, keine harten Grenzen der
    Plattformen. Jede Grenze laesst sich per Umgebungsvariable setzen:
    TOOLBOX_LIMIT_UEBERSCHRIFT, _TEASER, _INSTAGRAM, _BLUESKY, _LINKEDIN.
    """
    rules = [
        ("ueberschrift", _limit_for("ueberschrift", 45), "Ueberschrift"),
        ("teaser", _limit_for("teaser", 250), "Teaser"),
        ("instagram", _limit_for("instagram", 2200), "Instagram-Caption"),
        ("bluesky", _limit_for("bluesky", 300), "Bluesky"),
        ("linkedin", _limit_for("linkedin", 3000), "LinkedIn"),
    ]
    out = []
    for key, limit, label in rules:
        if key not in checks:
            continue
        val = checks[key]
        unit = "Grapheme" if key == "bluesky" else "Zeichen"
        out.append(
            {
                "bereich": f"{label} max. {limit} {unit}",
                "wert": val,
                "limit": limit,
                "rest": limit - val,
                "status": "ok" if val <= limit else "ueberschritten",
            }
        )
    return out


def analyze(text: str, kind: str = "frei") -> dict:
    """Zaehlt Zeichen, Woerter, Saetze; prueft optional eine Laengengrenze.

    kind: frei | ueberschrift | teaser | instagram | bluesky | linkedin
    """
    if text is None:
        raise ToolError("Kein Text uebergeben.")
    if len(text) > 500_000:
        raise ToolError("Text groesser als 500.000 Zeichen.")

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    words = _words(normalized)
    word_count = len(words)
    sentences = _sentences(normalized)
    paragraphs = len([p for p in re.split(r"\n\s*\n", normalized) if p.strip()])
    lines = len([ln for ln in normalized.split("\n") if ln.strip()])
    chars_with = len(normalized)
    chars_without = len(re.sub(r"\s", "", normalized))
    graphemes = _graphemes(normalized)
    syllables = sum(_syllables(w) for w in words)
    letters = sum(1 for ch in normalized if ch.isalpha())

    avg_word = round(chars_without / word_count, 1) if word_count else 0.0
    avg_sentence = round(word_count / sentences, 1) if sentences else 0.0

    # Flesch-Reading-Ease, deutsche Anpassung (Amstad)
    fre = 180 - (syllables / word_count * 58.5) - (word_count / sentences * 1.0) if word_count and sentences else None

    result = {
        "zeichen_mit_leerzeichen": chars_with,
        "zeichen_ohne_leerzeichen": chars_without,
        "buchstaben": letters,
        "woerter": word_count,
        "saetze": sentences,
        "absaetze": paragraphs,
        "zeilen": lines,
        "silben": syllables,
        "grapheme": graphemes,
        "schnitt_zeichen_pro_wort": avg_word,
        "schnitt_woerter_pro_satz": avg_sentence,
        "lesbarkeit_flesch": round(fre, 1) if fre is not None else None,
        "lesezeit": _reading_time(word_count, syllables),
    }
    if fre is not None:
        if fre >= 60:
            result["lesbarkeit_stufe"] = "leicht"
        elif fre >= 40:
            result["lesbarkeit_stufe"] = "mittel"
        else:
            result["lesbarkeit_stufe"] = "schwer"

    checked = _limits({kind: chars_with}) if kind and kind != "frei" else []
    if kind == "bluesky":
        checked = _limits({"bluesky": graphemes})
    if checked:
        result["laengenpruefung"] = checked
        if any(c["status"] == "ueberschritten" for c in checked):
            result["warnung"] = "Laengengrenze ueberschritten."
    return result
