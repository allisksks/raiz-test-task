from market_map.signals import _cheaper, _percent, _pp


def test_number_formatting():
    assert _percent(0.0912) == "9,1%"
    assert _pp(-0.083) == "8,3 п.п."
    assert _cheaper(-0.253) == "на 25% ниже"
    assert _cheaper(0.12) == "на 12% выше"
