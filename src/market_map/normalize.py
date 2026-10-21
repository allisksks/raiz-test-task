"""Нормализация значений из источников: регионы, каналы, числа."""

import re

from market_map.reference import CHANNELS, REGIONS

_SPACES = re.compile(r"\s+")
_SUFFIXES = (
    (re.compile(r"\bобл\.?(?=\s|$)"), "область"),
    (re.compile(r"\bкр\.?(?=\s|$)"), "край"),
    (re.compile(r"\bресп\.?(?=\s|$)"), "республика"),
)


def _key(value: str) -> str:
    key = _SPACES.sub(" ", value.replace("ё", "е").replace("Ё", "Е")).strip().casefold()
    for pattern, full in _SUFFIXES:
        key = pattern.sub(full, key)
    return key


_REGION_INDEX = {_key(alias): r.code for r in REGIONS for alias in (r.name, *r.aliases)}


def region_code(value: str | None) -> str | None:
    """Код региона ISO по названию или его варианту; None — если не распознан."""
    if not value:
        return None
    return _REGION_INDEX.get(_key(value))


_CHANNEL_INDEX = {_key(c.name): c.code for c in CHANNELS} | {
    _key(alias): code
    for code, aliases in {
        "wholesale": ("оптовая торговля", "оптовик"),
        "distribution": ("дистрибьютор", "дистрибутор"),
        "retail": ("розничная торговля", "магазин"),
        "diy": ("diy", "строительный гипермаркет"),
        "construction": ("строительство", "застройщик", "подрядчик"),
        "marketplace": ("интернет-магазин", "онлайн"),
    }.items()
    for alias in aliases
}


def channel_code(value: str | None) -> str | None:
    """Код канала сбыта по названию; None — если не распознан."""
    if not value:
        return None
    return _CHANNEL_INDEX.get(_key(value))
