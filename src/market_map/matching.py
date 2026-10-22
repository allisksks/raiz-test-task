"""Сопоставление товаров конкурентов со своей номенклатурой.

1. Артикул совпадает со своим → идентичный товар.
2. Иначе сравниваем названия без брендов и артикулов: совпадение с товаром того же
   бренда → идентичный, другого бренда → схожий (аналог).
"""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

MIN_NAME_SIMILARITY = 0.85

_SKU_IN_TITLE = re.compile(r"\(?\bарт\.?\s*([\w-]+)\)?|^([A-ZА-ЯЁ]{2,}-\d+)\s+", re.IGNORECASE)
_DIGIT_UNIT = re.compile(r"(\d)([^\d\s,.])")
_NON_WORD = re.compile(r"[^\w\s,]+")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class OwnProduct:
    id: int
    sku: str | None
    name: str
    brand: str | None
    group_id: int | None


@dataclass(frozen=True)
class Match:
    own_product_id: int
    match_type: str  # identical | similar
    method: str  # sku | name
    confidence: float


def sku_from_title(title: str) -> str | None:
    """Артикул, если он записан в названии: «… (арт. PM-101)» или «PM-101 …»."""
    m = _SKU_IN_TITLE.search(title)
    return (m.group(1) or m.group(2)).upper() if m else None


def clean_title(title: str) -> str:
    """Название без артикула."""
    return _SPACES.sub(" ", _SKU_IN_TITLE.sub(" ", title)).strip()


def name_key(name: str, brands: set[str]) -> str:
    """Ключ для сравнения названий: без регистра, брендов, артикула; «30кг» → «30 кг»."""
    key = clean_title(name).casefold().replace("ё", "е")
    for brand in brands:
        key = key.replace(brand.casefold().replace("ё", "е"), " ")
    key = _DIGIT_UNIT.sub(r"\1 \2", _NON_WORD.sub(" ", key))
    return _SPACES.sub(" ", key).strip()


class Matcher:
    def __init__(self, own: list[OwnProduct], brands: set[str]):
        self.brands = brands | {p.brand for p in own if p.brand}
        self.by_sku = {p.sku.upper(): p for p in own if p.sku}
        self.own = [(p, name_key(p.name, self.brands)) for p in own]

    def match(
        self, sku: str | None, title: str, brand: str | None, group_id: int | None
    ) -> Match | None:
        sku = (sku or sku_from_title(title) or "").upper()
        if sku in self.by_sku:
            return Match(self.by_sku[sku].id, "identical", "sku", 1.0)

        key = name_key(title, self.brands)
        best, best_ratio = None, 0.0
        for product, own_key in self.own:
            if group_id is not None and product.group_id != group_id:
                continue
            ratio = SequenceMatcher(None, key, own_key).ratio()
            if ratio > best_ratio:
                best, best_ratio = product, ratio
        if best is None or best_ratio < MIN_NAME_SIMILARITY:
            return None
        same_brand = (
            brand is not None
            and best.brand is not None
            and (brand.casefold() == best.brand.casefold())
        )
        return Match(
            best.id, "identical" if same_brand else "similar", "name", round(best_ratio, 2)
        )
