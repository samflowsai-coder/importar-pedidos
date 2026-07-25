"""Ranking assistido de candidatos do catálogo local para o picker de de-para.

Sugere; nunca aplica. Heurística barata sobre alguns milhares de linhas:
EAN parcial (peso alto) > sobreposição de tokens descrição×nome > código contido.
"""

from __future__ import annotations

import re


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^\w]+", (s or "").upper()) if len(t) >= 2}


def _score(description: str, product_code: str | None, ean: str | None, prod: dict) -> float:
    score = 0.0
    ean_d = re.sub(r"\D", "", ean or "")
    prod_ean = re.sub(r"\D", "", prod.get("ean") or "")
    if ean_d and prod_ean and (ean_d in prod_ean or prod_ean in ean_d):
        score += 5.0
    dt, nt = _tokens(description), _tokens(prod.get("nome", ""))
    if dt and nt:
        inter = len(dt & nt)
        union = len(dt | nt)
        if union:
            score += 3.0 * (inter / union)
    code = (product_code or "").strip().upper()
    if code and (
        code in (prod.get("codigo") or "").upper() or code in (prod.get("nome") or "").upper()
    ):
        score += 1.0
    return round(score, 4)


def rank_candidates(
    *,
    description: str,
    product_code: str | None,
    ean: str | None,
    catalog: list[dict],
    limit: int = 5,
) -> list[dict]:
    scored = []
    for prod in catalog:
        s = _score(description, product_code, ean, prod)
        if s > 0:
            scored.append({**prod, "score": s})
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:limit]
