"""In-house content classification + brand-suitability lexicons.

A lightweight, dependency-free keyword classifier (NOT ML / NOT a vendor). It
maps page text to a coarse IAB-style tier-1 category and a GARM-style suitability
risk tier. Matching is word-boundary based to avoid substring false positives
(e.g. "arms" must not fire on "pharmacy"). Treat outputs as low-confidence hints;
they are flagged as heuristic in the scan record.
"""
from __future__ import annotations

import re
from typing import Any

# --- IAB-style tier-1 category lexicons -------------------------------------
_CATEGORY_TERMS: dict[str, list[str]] = {
    "News": ["news", "breaking", "reuters", "associated press", "headlines", "reporter"],
    "Sports": ["game", "team", "season", "playoff", "coach", "league", "tournament", "score"],
    "Business & Finance": ["market", "stocks", "investor", "earnings", "revenue", "economy", "nasdaq", "shares"],
    "Technology": ["software", "app", "gadget", "startup", "ai", "chip", "smartphone", "device", "computing"],
    "Entertainment": ["movie", "film", "celebrity", "music", "trailer", "tv show", "streaming", "box office"],
    "Health": ["health", "symptoms", "doctor", "disease", "treatment", "wellness", "diet", "medical"],
    "Travel": ["travel", "hotel", "flight", "destination", "vacation", "tourism", "itinerary"],
    "Food & Drink": ["recipe", "cooking", "restaurant", "ingredient", "cuisine", "baking", "dinner"],
    "Automotive": ["car", "vehicle", "engine", "horsepower", "sedan", "suv", "automaker", "ev"],
    "Style & Fashion": ["fashion", "outfit", "designer", "runway", "wardrobe", "trend", "apparel"],
    "Science": ["research", "study", "scientists", "experiment", "physics", "climate", "species"],
    "Politics": ["election", "senate", "congress", "president", "policy", "campaign", "legislation"],
    "Video Gaming": ["gameplay", "console", "playstation", "xbox", "nintendo", "esports", "gamer"],
}

# --- GARM-style suitability lexicons (term -> severity weight) ---------------
_SUITABILITY_TERMS: dict[str, dict[str, int]] = {
    "adult_explicit": {"porn": 3, "xxx": 3, "nude": 2, "explicit sex": 3},
    "hate_speech": {"slur": 2, "white supremacy": 3, "ethnic cleansing": 3},
    "arms_ammunition": {"firearm": 2, "assault rifle": 3, "ammunition": 2, "handgun": 2},
    "illegal_drugs": {"cocaine": 2, "heroin": 3, "meth": 2, "buy drugs": 3},
    "terrorism": {"terrorist": 3, "isis": 3, "jihad": 2, "bomb attack": 3},
    "death_violence": {"massacre": 2, "graphic violence": 3, "execution": 2, "gore": 2},
    "obscenity": {"profanity": 1, "obscene": 1},
    "piracy": {"torrent": 1, "pirated": 2, "crack download": 2},
}

# Severity threshold per tier (weighted match sum). Set high enough that a single
# topical mention ("an article about drugs") does not trip a flag — keyword
# matching cannot distinguish "about X" from "is X", so we bias toward NOT
# flagging. Suitability remains a low-confidence, advisory heuristic.
_FLOOR = 12
_HIGH = 7
_MEDIUM = 4


def _compile(terms: list[str]) -> re.Pattern:
    return re.compile(r"|".join(rf"\b{re.escape(t)}\b" for t in terms), re.IGNORECASE)


_CATEGORY_PATTERNS = {cat: _compile(terms) for cat, terms in _CATEGORY_TERMS.items()}
_SUITABILITY_PATTERNS = {
    cat: {_compile([t]): w for t, w in terms.items()}
    for cat, terms in _SUITABILITY_TERMS.items()
}


def classify_category(text: str, title: str | None) -> tuple[str, float]:
    hay = f"{title or ''} {text}".lower()
    scores = {cat: len(pat.findall(hay)) for cat, pat in _CATEGORY_PATTERNS.items()}
    best = max(scores, key=lambda c: scores[c])
    total = sum(scores.values())
    if scores[best] == 0 or total == 0:
        return "Unknown", 0.0
    return best, round(scores[best] / total, 3)


def assess_suitability(text: str) -> dict[str, Any]:
    hay = text.lower()
    flagged: dict[str, int] = {}
    weighted = 0
    for cat, pats in _SUITABILITY_PATTERNS.items():
        cat_score = sum(w for pat, w in pats.items() if pat.search(hay))
        if cat_score:
            flagged[cat] = cat_score
            weighted += cat_score
    if weighted >= _FLOOR:
        tier = "floor"
    elif weighted >= _HIGH:
        tier = "high"
    elif weighted >= _MEDIUM:
        tier = "medium"
    else:
        tier = "low"
    return {"risk_tier": tier, "flagged_categories": sorted(flagged), "risk_weight": weighted}


def analyze(text: str, *, title: str | None = None) -> dict[str, Any]:
    # Prefer the embedding classifier (semantic); fall back to keyword lexicons.
    from app import content_ml
    ml = content_ml.try_analyze(text, title)
    if ml is not None:
        return ml
    category, confidence = classify_category(text, title)
    return {
        "category": category,
        "category_confidence": confidence,
        "suitability": assess_suitability(text),
        "heuristic": True,
        "method": "keyword",
    }
