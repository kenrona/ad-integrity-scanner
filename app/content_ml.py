"""Zero-shot content classification via static embeddings (model2vec).

No training data needed: page text is embedded and compared (cosine) to short
prototype descriptions of each IAB-style category / GARM-style risk class. This
judges the *meaning* of the whole page, so it doesn't misfire on a single
topical keyword the way the lexicon classifier does.

model2vec uses static token embeddings (no transformer forward pass) — fast
enough for the static tier. If the model can't load (offline / not installed),
`try_analyze` returns None and the caller falls back to the keyword classifier.
"""
from __future__ import annotations

from typing import Any

from app.config import get_settings

_MODEL_NAME = "minishlab/potion-base-8M"
_TEXT_CAP = 4000  # chars of page text to embed

CATEGORY_PROTOS = {
    "News": "breaking news current events reporting headlines world politics coverage",
    "Sports": "sports games teams athletes scores leagues matches tournaments coaches",
    "Business & Finance": "business finance markets stocks economy investing earnings companies",
    "Technology": "technology software gadgets computers apps startups devices internet ai",
    "Entertainment": "movies film television celebrities music streaming shows trailers reviews",
    "Health": "health medicine disease symptoms treatment doctors wellness fitness diet",
    "Travel": "travel tourism hotels flights destinations vacations trips itineraries guides",
    "Food & Drink": "food recipes cooking restaurants cuisine ingredients meals baking dining",
    "Automotive": "cars vehicles automotive engines driving models reviews electric trucks",
    "Style & Fashion": "fashion style clothing designers runway trends beauty apparel outfits",
    "Science": "science research studies scientists physics biology climate space experiments",
    "Education": "education learning school university students courses teaching academic",
    "Video Gaming": "video games gaming consoles players esports playstation xbox nintendo",
    "Home & Garden": "home garden interior decorating furniture diy renovation gardening",
    "Personal Finance": "personal finance budgeting savings loans credit mortgages retirement taxes",
    "Lifestyle": "lifestyle relationships family parenting hobbies self improvement wellbeing",
}

# (prototype, severity weight)
SUITABILITY_PROTOS = {
    "adult_explicit": ("explicit sexual pornographic adult content nudity", 4),
    "hate_speech": ("hate speech racism discrimination slurs extremist ideology", 4),
    "terrorism": ("terrorism terrorist attacks extremist violence bombing jihad", 4),
    "arms_ammunition": ("firearms guns weapons ammunition rifles selling buying", 3),
    "illegal_drugs": ("illegal drugs narcotics cocaine heroin meth dealing abuse", 3),
    "death_violence": ("graphic violence death gore brutal killing torture war atrocity", 3),
    "obscenity": ("obscene profanity vulgar crude offensive language", 1),
}
_SAFE_ANCHORS = [
    "an ordinary informative article about everyday topics",
    "a product review or shopping guide",
    "a general news report or how-to explainer",
]

# Flag a risk class only if it clearly beats benign baseline — keeps "an article
# *about* X" and semantic neighbours from tripping. Calibrated vs potion-base-8M.
_RISK_MIN_SIM = 0.22
_RISK_MARGIN = 0.10
_CAT_MIN_SIM = 0.05

_model = "unset"          # model instance, or None if unavailable
_cat_mat = None           # (labels, normalized embedding matrix)
_suit_mat = None
_safe_mat = None


def _get_model():
    global _model
    if _model == "unset":
        try:
            from model2vec import StaticModel
            _model = StaticModel.from_pretrained(_MODEL_NAME)
        except Exception:  # noqa: BLE001 — offline / not installed -> fall back
            _model = None
    return _model


def _embed(texts: list[str]):
    import numpy as np
    v = _get_model().encode(texts)
    n = np.linalg.norm(v, axis=1, keepdims=True)
    return v / (n + 1e-9)


def _ensure_protos():
    global _cat_mat, _suit_mat, _safe_mat
    if _cat_mat is not None:
        return
    cats = list(CATEGORY_PROTOS)
    _cat_mat = (cats, _embed([CATEGORY_PROTOS[c] for c in cats]))
    suits = list(SUITABILITY_PROTOS)
    _suit_mat = (suits, _embed([SUITABILITY_PROTOS[s][0] for s in suits]))
    _safe_mat = _embed(_SAFE_ANCHORS)


def is_available() -> bool:
    s = get_settings()
    if s.content_classifier == "keyword":
        return False
    return _get_model() is not None


def try_analyze(text: str, title: str | None) -> dict[str, Any] | None:
    if not is_available() or not (text or title):
        return None
    try:
        _ensure_protos()
        import numpy as np
        e = _embed([f"{title or ''} {text}"[:_TEXT_CAP]])[0]

        cat_labels, cat_emb = _cat_mat
        cat_sims = cat_emb @ e
        bi = int(np.argmax(cat_sims))
        best, best_sim = cat_labels[bi], float(cat_sims[bi])
        category = best if best_sim >= _CAT_MIN_SIM else "Unknown"

        suit_labels, suit_emb = _suit_mat
        safe = float(np.max(_safe_mat @ e))
        flagged: dict[str, int] = {}
        best_flag_sim = 0.0
        for i, lab in enumerate(suit_labels):
            sim = float(suit_emb[i] @ e)
            if sim >= _RISK_MIN_SIM and (sim - safe) >= _RISK_MARGIN:
                flagged[lab] = SUITABILITY_PROTOS[lab][1]
                best_flag_sim = max(best_flag_sim, sim)
        # Tier from the most-severe flag. 'floor' requires BOTH a high-severity
        # class AND a strong match — so crime/violence *news* lands at 'high'
        # (legit GARM adjacency) while content that strongly IS extreme hits floor.
        max_sev = max(flagged.values(), default=0)
        if max_sev >= 4 and best_flag_sim >= 0.30:
            tier = "floor"
        elif max_sev >= 3:
            tier = "high"
        elif max_sev >= 1:
            tier = "medium"
        else:
            tier = "low"
        return {
            "category": category,
            "category_confidence": round(best_sim, 3),
            "suitability": {"risk_tier": tier, "flagged_categories": sorted(flagged),
                            "risk_weight": sum(flagged.values())},
            "heuristic": True,
            "method": "embedding",
        }
    except Exception:  # noqa: BLE001 — never break the scan on classifier error
        return None
