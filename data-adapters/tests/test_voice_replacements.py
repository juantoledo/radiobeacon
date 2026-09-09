import re

import pytest

from adapters.voice_replacements import (
    SUGGESTED_VOICE_REPLACEMENTS,
    apply_voice_replacements,
)


def test_expands_whole_words_only():
    rules = {"km": "kilómetros", "NE": "noreste", "SENAPRED": "Senapred"}
    assert apply_voice_replacements("a 36 km al NE", rules) == "a 36 kilómetros al noreste"
    assert apply_voice_replacements("(SENAPRED)", rules) == "(Senapred)"
    # embedded in a longer word -> untouched
    assert apply_voice_replacements("kilometraje", rules) == "kilometraje"
    assert apply_voice_replacements("SENAPREDINFO", rules) == "SENAPREDINFO"


def test_is_case_sensitive():
    rules = {"SO": "suroeste", "SE": "sureste"}
    assert apply_voice_replacements("se fue al SO", rules) == "se fue al suroeste"
    assert apply_voice_replacements("SE aleja", rules) == "sureste aleja"


def test_longest_key_wins_and_output_is_not_re_scanned():
    rules = {"NE": "noreste", "NNE": "nornoreste"}
    assert apply_voice_replacements("viento del NNE", rules) == "viento del nornoreste"
    # 'kilómetros' contains no key; 'km' -> 'kilómetros' is not re-expanded
    assert apply_voice_replacements("km", {"km": "kilómetros", "m": "metros"}) == "kilómetros"


def test_punctuation_edged_keys_match():
    rules = {"km/h": "kilómetros por hora", "N°": "número", "aprox.": "aproximadamente"}
    assert (
        apply_voice_replacements("va a 5 km/h, N° 3, aprox. dos", rules)
        == "va a 5 kilómetros por hora, número 3, aproximadamente dos"
    )
    # a digit on both edges is still a word boundary failure
    assert apply_voice_replacements("1km/h2", rules) == "1km/h2"


def test_identity_cases():
    assert apply_voice_replacements("", {"km": "kilómetros"}) == ""
    assert apply_voice_replacements("nothing here", {}) == "nothing here"
    assert apply_voice_replacements("no key matches", {"km": "kilómetros"}) == "no key matches"


def test_fail_soft_when_pattern_does_not_compile(monkeypatch, caplog):
    def boom(*_a, **_k):
        raise re.error("forced")

    monkeypatch.setattr(re, "compile", boom)
    with caplog.at_level("ERROR"):
        assert apply_voice_replacements("36 km al NE", {"km": "kilómetros"}) == "36 km al NE"
    assert "voice replacements did not compile" in caplog.text


def test_suggested_set_is_sane():
    spanish_words = {"o", "e", "y", "no", "se", "es", "un", "la", "el", "de", "en", "al"}
    for key, value in SUGGESTED_VOICE_REPLACEMENTS.items():
        assert value.strip(), f"{key!r} has an empty expansion"
        assert key.lower() not in spanish_words, f"{key!r} collides with a Spanish word"
        assert not (len(key) == 1 and key.isalpha()), f"{key!r} is a bare single letter"


def test_suggested_set_applies_end_to_end():
    text = "Sismo a 10 km de profundidad, viento del SO. Consulte SENAPRED."
    out = apply_voice_replacements(text, SUGGESTED_VOICE_REPLACEMENTS)
    assert "kilómetros" in out and "suroeste" in out and "Senapred" in out
    assert "km" not in out.replace("kilómetros", "")
