from market_map.reference import CHANNELS, REGIONS, REGIONS_BY_CODE

# Коды регионов в GeoJSON карты России Superset 4.1
SUPERSET_CODES = {
    "RU-TOM", "RU-AD", "RU-KC", "RU-IN", "RU-KB", "RU-SE", "RU-STA", "RU-CHU", "RU-KGD", "RU-MUR",
    "RU-NGR", "RU-PSK", "RU-LEN", "RU-SPE", "RU-BRY", "RU-SMO", "RU-KR", "RU-ARK", "RU-IVA",
    "RU-VLG", "RU-KOS", "RU-NIZ", "RU-TVE", "RU-YAR", "RU-KLU", "RU-KRS", "RU-LIP", "RU-MOW",
    "RU-MOS", "RU-ORL", "RU-ROS", "RU-TUL", "RU-VGG", "RU-BEL", "RU-KDA", "RU-MO", "RU-PNZ",
    "RU-RYA", "RU-TAM", "RU-VLA", "RU-VOR", "RU-BA", "RU-CHE", "RU-KGN", "RU-NEN", "RU-YAN",
    "RU-KO", "RU-KIR", "RU-ME", "RU-SVE", "RU-UD", "RU-AST", "RU-CU", "RU-KL", "RU-SAM", "RU-ORE",
    "RU-SAR", "RU-TA", "RU-ULY", "RU-KHM", "RU-OMS", "RU-TYU", "RU-ALT", "RU-AL", "RU-KEM",
    "RU-KK", "RU-NVS", "RU-CE", "RU-DA", "RU-IRK", "RU-KYA", "RU-TY", "RU-BU", "RU-AMU", "RU-ZAB",
    "RU-PRI", "RU-SA", "RU-YEV", "RU-KHA", "RU-MAG", "RU-SAK", "UA-43", "RU-PER", "RU-KAM",
    "UA-40",
}  # fmt: skip


def test_all_85_regions_unique():
    assert len(REGIONS) == 85
    assert len(REGIONS_BY_CODE) == 85
    assert len({r.name for r in REGIONS}) == 85


def test_map_codes_cover_superset_map():
    assert {r.map_code for r in REGIONS} == SUPERSET_CODES


def test_moscow_codes_swapped_for_superset():
    # В GeoJSON Superset RU-MOS — это город, RU-MOW — область
    assert REGIONS_BY_CODE["RU-MOW"].map_code == "RU-MOS"
    assert REGIONS_BY_CODE["RU-MOS"].map_code == "RU-MOW"


def test_aliases_do_not_collide():
    seen: dict[str, str] = {}
    for r in REGIONS:
        for alias in (r.name, *r.aliases):
            assert seen.setdefault(alias.lower(), r.code) == r.code, alias


def test_channels_unique():
    assert len({c.code for c in CHANNELS}) == len(CHANNELS)
