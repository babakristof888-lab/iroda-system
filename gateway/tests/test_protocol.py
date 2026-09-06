"""A soros sorok parsolása.

A soros porton bármikor jöhet szemét, ezért itt az a fontos, hogy semmi ne
dobjon kivételt, és semmi ne csússzon át hibásan érvényes bélyegzésként.
"""

from __future__ import annotations

import pytest

import protocol


def test_card_sor():
    line = protocol.parse_line("CARD;04A1B2C3")
    assert line.kind == protocol.CARD
    assert line.uid == "04A1B2C3"


def test_card_sor_sorveggel_es_szokozzel():
    line = protocol.parse_line("  CARD;04A1B2C3\r\n")
    assert line.kind == protocol.CARD
    assert line.uid == "04A1B2C3"


def test_env_sor():
    line = protocol.parse_line("ENV;T=23.45;H=41.20;P=1013.25")
    assert line.kind == protocol.ENV
    assert (line.temp, line.hum, line.press) == (23.45, 41.20, 1013.25)


def test_env_sor_negativ_homerseklettel():
    line = protocol.parse_line("ENV;T=-5.5;H=80.0;P=990.0")
    assert line.kind == protocol.ENV
    assert line.temp == -5.5


def test_dup_sor():
    line = protocol.parse_line("DUP;04A1B2C3")
    assert line.kind == protocol.DUP
    assert line.uid == "04A1B2C3"


def test_err_es_ok_sorok():
    assert protocol.parse_line("ERR;RC522_OFFLINE").code == protocol.ERR_RC522_OFFLINE
    assert protocol.parse_line("ERR;BME_OFFLINE").code == protocol.ERR_BME_OFFLINE
    assert protocol.parse_line("OK;RC522_RECOVERED").code == protocol.OK_RC522_RECOVERED
    assert protocol.parse_line("OK;BME_RECOVERED").code == protocol.OK_BME_RECOVERED
    assert protocol.parse_line("ERR;RC522_OFFLINE").kind == protocol.ERROR
    assert protocol.parse_line("OK;BME_RECOVERED").kind == protocol.OK


def test_rdy_sorbol_kiderul_a_szenzor_es_a_firmware():
    line = protocol.parse_line("RDY;fw=1.1.0;rc522=0x92;bme=0x76")
    assert line.kind == protocol.READY
    assert line.firmware == "1.1.0"
    assert line.bme_present is True
    assert line.rc522_present is True


def test_rdy_sor_szenzor_nelkul():
    line = protocol.parse_line("RDY;fw=1.1.0;rc522=0x92;bme=0x00")
    assert line.bme_present is False
    assert line.rc522_present is True


def test_rdy_sor_hianyzo_bme_mezovel_nem_allit_semmit():
    line = protocol.parse_line("RDY;fw=1.1.0")
    assert line.bme_present is None


def test_pong_sor():
    line = protocol.parse_line("PONG;fw=1.1.0")
    assert line.kind == protocol.PONG
    assert line.firmware == "1.1.0"


@pytest.mark.parametrize(
    "raw",
    [
        "CARD",
        "CARD;",
        "CARD;   ",
        "DUP;",
        "ERR;",
        "OK;",
    ],
)
def test_csonka_sor_nem_dob_kivetelt(raw):
    line = protocol.parse_line(raw)
    assert line.kind == protocol.INVALID
    assert line.reason


@pytest.mark.parametrize(
    "raw",
    [
        "ENV;T=xx;H=41.20;P=1013.25",
        "ENV;T=;H=41.20;P=1013.25",
        "ENV;T=23,45;H=41.20;P=1013.25",
        "ENV;T=NaN-nel;H=41.20;P=1013.25",
    ],
)
def test_hibas_float_az_env_sorban_eldobja_a_mintat(raw):
    line = protocol.parse_line(raw)
    assert line.kind == protocol.INVALID
    assert "nem szám" in line.reason


@pytest.mark.parametrize(
    "raw",
    ["ENV;T=23.45;H=41.20", "ENV;H=41.20;P=1013.25", "ENV", "ENV;"],
)
def test_hianyos_env_sor(raw):
    line = protocol.parse_line(raw)
    assert line.kind == protocol.INVALID
    assert "hiányzó mező" in line.reason


@pytest.mark.parametrize(
    "raw",
    [
        "ENV;T=999;H=41.20;P=1013.25",
        "ENV;T=23.45;H=250;P=1013.25",
        "ENV;T=23.45;H=41.20;P=5",
        "ENV;T=-100;H=41.20;P=1013.25",
    ],
)
def test_fizikailag_lehetetlen_ertek_nem_rontja_el_az_orat(raw):
    """A szerver ugyanezekre `out_of_range` hibát adna, és az egész órás sor
    örökre `error` státuszban ragadna."""
    line = protocol.parse_line(raw)
    assert line.kind == protocol.INVALID
    assert "tartományon kívüli" in line.reason


def test_szemet_bajtok_nem_lesznek_belyegzesbol():
    line = protocol.parse_line(b"\xff\xfe\xffCARD;04A1B2C3")
    assert line.kind in (protocol.INVALID, protocol.UNKNOWN)
    assert line.uid is None


def test_vezerlokarakter_a_sor_belsejeben_eldobja_a_sort():
    line = protocol.parse_line("CARD;04A1\x01B2C3")
    assert line.kind == protocol.INVALID


def test_nullbajtos_keret_levagasa_utan_ertelmes_a_sor():
    line = protocol.parse_line(b"\x00CARD;04A1B2C3\x00")
    assert line.kind == protocol.CARD
    assert line.uid == "04A1B2C3"


def test_ismeretlen_prefix():
    line = protocol.parse_line("VALAMI;egyeb")
    assert line.kind == protocol.UNKNOWN
    assert "ismeretlen prefix" in line.reason


@pytest.mark.parametrize("raw", ["", "   ", "\r\n", "\x00", None])
def test_ures_sorra_nincs_teendo(raw):
    assert protocol.parse_line(raw) is None


def test_hosszu_szemet_sem_dob_kivetelt():
    assert protocol.parse_line("x" * 100000) is not None
