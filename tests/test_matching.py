import pytest

from market_map.matching import Matcher, OwnProduct, clean_title, name_key, sku_from_title
from market_map.normalize import parse_price

OWN = [
    OwnProduct(1, "PM-101", "Штукатурка гипсовая ПрофМикс 30 кг", "ПрофМикс", 10),
    OwnProduct(2, "PM-102", "Штукатурка цементная ПрофМикс 25 кг", "ПрофМикс", 10),
    OwnProduct(3, "GS-302", "Гидроизоляция эластичная 2К ГидроСтоп 15 кг", "ГидроСтоп", 20),
]


@pytest.fixture
def matcher():
    return Matcher(OWN, brands={"БазаМикс", "АкваБарьер"})


@pytest.mark.parametrize(
    ("text", "price"),
    [("1234.50", 1234.5), ("1 234,50 ₽", 1234.5), ("1 234 руб.", 1234.0), ("498,41", 498.41),
     (512, 512.0), ("", None), ("по запросу", None), ("0", None), (None, None)],
)  # fmt: skip
def test_parse_price(text, price):
    assert parse_price(text) == price


def test_sku_from_title():
    assert sku_from_title("Штукатурка гипсовая ПрофМикс 30 кг (арт. PM-101)") == "PM-101"
    assert sku_from_title("PM-101 Штукатурка гипсовая ПрофМикс 30 кг") == "PM-101"
    assert sku_from_title("Штукатурка гипсовая ПрофМикс 30 кг") is None
    assert clean_title("PM-101 Штукатурка (арт. PM-101)") == "Штукатурка"


def test_name_key_ignores_case_brand_and_unit_spacing():
    brands = {"ПрофМикс", "БазаМикс"}
    assert name_key("ШТУКАТУРКА ГИПСОВАЯ ПРОФМИКС 30КГ", brands) == name_key(
        "Штукатурка гипсовая БазаМикс 30 кг", brands
    )


def test_match_by_sku(matcher):
    m = matcher.match("pm-102", "что угодно", "ПрофМикс", 10)
    assert (m.own_product_id, m.match_type, m.method) == (2, "identical", "sku")


def test_match_by_sku_in_title(matcher):
    m = matcher.match(None, "ШТУКАТУРКА ГИПСОВАЯ ПРОФМИКС 30 КГ (арт. PM-101)", "ПрофМикс", 10)
    assert (m.own_product_id, m.match_type, m.method) == (1, "identical", "sku")


def test_same_brand_by_name_is_identical(matcher):
    m = matcher.match(None, "ШТУКАТУРКА ГИПСОВАЯ ПРОФМИКС 30КГ", "ПрофМикс", 10)
    assert (m.own_product_id, m.match_type, m.method) == (1, "identical", "name")


def test_other_brand_is_similar(matcher):
    m = matcher.match("АК-302", "Гидроизоляция эластичная 2К АкваБарьер 15 кг", "АкваБарьер", 20)
    assert (m.own_product_id, m.match_type, m.method) == (3, "similar", "name")
    assert m.confidence >= 0.85


def test_no_match_for_unrelated_product(matcher):
    assert matcher.match(None, "Перфоратор аккумуляторный", "Дрель-М", 10) is None


def test_group_restricts_candidates(matcher):
    # Штукатурка в группе гидроизоляции не ищется
    assert matcher.match(None, "Штукатурка гипсовая БазаМикс 30 кг", "БазаМикс", 20) is None
