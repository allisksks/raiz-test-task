import pytest

from market_map.normalize import channel_code, region_code
from market_map.reference import CHANNELS, REGIONS


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("Новосибирская область", "RU-NVS"),
        ("Новосибирская обл.", "RU-NVS"),
        ("новосибирская  обл", "RU-NVS"),
        ("НСО", "RU-NVS"),
        ("г. Москва", "RU-MOW"),
        ("Краснодарский кр.", "RU-KDA"),
        ("Республика Саха (Якутия)", "RU-SA"),
        ("Кемеровская область — Кузбасс", "RU-KEM"),
    ],
)
def test_region_code(value, code):
    assert region_code(value) == code


def test_every_region_name_and_alias_resolves():
    for r in REGIONS:
        for alias in (r.name, *r.aliases):
            assert region_code(alias) == r.code, alias


def test_unknown_region():
    assert region_code("Атлантида") is None
    assert region_code("") is None
    assert region_code(None) is None


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("Опт", "wholesale"),
        ("оптовая торговля", "wholesale"),
        ("Дистрибьютор", "distribution"),
        ("DIY-сеть", "diy"),
        ("Маркетплейс", "marketplace"),
        ("Строительная компания", "construction"),
    ],
)
def test_channel_code(value, code):
    assert channel_code(value) == code


def test_every_channel_name_resolves():
    for c in CHANNELS:
        assert channel_code(c.name) == c.code
