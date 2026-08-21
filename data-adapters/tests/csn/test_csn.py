import json
import subprocess
import sys
from pathlib import Path

import pytest

from adapters.csn import (
    API_URL,
    SITE_URL,
    SOURCE_TZ,
    URGENT_MAGNITUDE_THRESHOLD,
    CsnAdapter,
    CsnEarthquake,
    _parse_earthquake,
)
from adapters.timeutil import to_utc

_ADAPTERS_SRC = str(Path(__file__).resolve().parents[2] / "src")
_PRINT_CONFIG_SNIPPET = (
    "import json; from adapters.csn import API_URL, SITE_URL, SOURCE_TZ, URGENT_MAGNITUDE_THRESHOLD; "
    "print(json.dumps({'API_URL': API_URL, 'SITE_URL': SITE_URL, 'SOURCE_TZ': SOURCE_TZ, "
    "'URGENT_MAGNITUDE_THRESHOLD': URGENT_MAGNITUDE_THRESHOLD}))"
)

_ADAPTERS_CSN_ENV_VARS = (
    "ADAPTERS_CSN_API_URL",
    "ADAPTERS_CSN_SITE_URL",
    "ADAPTERS_CSN_URGENT_MAGNITUDE_THRESHOLD",
    "ADAPTERS_CSN_SOURCE_TZ",
)


def _read_config_in_subprocess(env_overrides: dict) -> dict:
    """Runs a fresh Python process (isolated from this test session's
    already-imported adapters.csn module) to verify the module-level
    config constants pick up env vars at import time."""
    import os as os_module

    env = {k: v for k, v in os_module.environ.items() if k not in _ADAPTERS_CSN_ENV_VARS}
    env["PYTHONPATH"] = _ADAPTERS_SRC
    env.update(env_overrides)

    result = subprocess.run(
        [sys.executable, "-c", _PRINT_CONFIG_SNIPPET],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


MOCK_ITEMS = [
    {
        "Fecha": "2026-08-20 18:54:25",
        "Profundidad": "222",
        "Magnitud": "3.2",
        "RefGeografica": "78 km al SE de Socaire",
        "FechaUpdate": "2026-08-20T19:10:00.437Z",
    },
    {
        "Fecha": "2026-08-20 15:52:11",
        "Profundidad": "32",
        "Magnitud": "3.6",
        "RefGeografica": "56 km al S de Caldera",
        "FechaUpdate": "2026-08-20T19:10:00.440Z",
    },
    {
        "Fecha": "2026-08-20 12:00:00",
        "Profundidad": "50",
        "Magnitud": "6.1",
        "RefGeografica": "10 km al O de Iquique",
        "FechaUpdate": "2026-08-20T19:10:00.450Z",
    },
]


def test_parse_earthquake_maps_fields():
    earthquake = _parse_earthquake(MOCK_ITEMS[0])

    assert isinstance(earthquake, CsnEarthquake)
    assert earthquake.magnitud == 3.2
    assert earthquake.profundidad_km == 222
    assert earthquake.ref_geografica == "78 km al SE de Socaire"
    assert "experimental" in earthquake.experimental_disclaimer


def test_generic_contract_fields_are_inferred():
    earthquake = _parse_earthquake(MOCK_ITEMS[0])

    assert earthquake.title == "Sismo M3.2 - 78 km al SE de Socaire"
    assert "3.2" in earthquake.contents
    assert "222" in earthquake.contents
    assert earthquake.source_date_time == to_utc(earthquake.fecha, assume_tz=SOURCE_TZ)
    assert earthquake.type == "Sismo"
    assert earthquake.url == SITE_URL
    assert earthquake.dispatch_policy == "informational"  # magnitude 3.2 < default threshold


def test_generic_contract_id_is_fecha():
    earthquake = _parse_earthquake(MOCK_ITEMS[0])

    assert earthquake.id == earthquake.fecha.isoformat()


def test_generic_contract_event_key_is_fecha():
    earthquake = _parse_earthquake(MOCK_ITEMS[0])

    assert earthquake.event_key == earthquake.fecha.isoformat()


def test_generic_contract_id_ignores_magnitude_revisions():
    revised = dict(MOCK_ITEMS[0], Magnitud="3.4")

    original = _parse_earthquake(MOCK_ITEMS[0])
    updated = _parse_earthquake(revised)

    assert original.id == updated.id


def test_generic_contract_id_differs_between_distinct_earthquakes():
    a = _parse_earthquake(MOCK_ITEMS[0])
    b = _parse_earthquake(MOCK_ITEMS[1])

    assert a.id != b.id


def test_source_date_time_is_utc_converted():
    earthquake = _parse_earthquake(MOCK_ITEMS[0])  # "2026-08-20 18:54:25", winter (-04:00)

    assert earthquake.source_date_time.tzinfo is not None
    assert earthquake.source_date_time.utcoffset().total_seconds() == 0
    assert earthquake.source_date_time.isoformat() == "2026-08-20T22:54:25+00:00"


def test_id_and_event_key_are_unaffected_by_utc_conversion():
    """Deliberate, permanent decoupling — see CsnEarthquake.id's docstring.
    id/event_key must keep deriving from the raw, unconverted fecha so
    existing stored item_ids never change format."""
    earthquake = _parse_earthquake(MOCK_ITEMS[0])

    assert earthquake.id == "2026-08-20T18:54:25"
    assert earthquake.event_key == "2026-08-20T18:54:25"
    assert earthquake.id != earthquake.source_date_time.isoformat()


def test_source_tz_overridable_via_env_var():
    config = _read_config_in_subprocess({"ADAPTERS_CSN_SOURCE_TZ": "UTC"})

    assert config["SOURCE_TZ"] == "UTC"


def test_dispatch_policy_is_informational_below_magnitude_threshold():
    earthquake = _parse_earthquake(MOCK_ITEMS[0])  # magnitude 3.2

    assert earthquake.magnitud < URGENT_MAGNITUDE_THRESHOLD
    assert earthquake.dispatch_policy == "informational"


def test_dispatch_policy_is_urgent_at_or_above_magnitude_threshold():
    earthquake = _parse_earthquake(MOCK_ITEMS[2])  # magnitude 6.1

    assert earthquake.magnitud >= URGENT_MAGNITUDE_THRESHOLD
    assert earthquake.dispatch_policy == "urgent"


def test_fetch_sorts_newest_first(monkeypatch):
    monkeypatch.setattr(
        "adapters.csn._fetch_earthquakes", lambda: MOCK_ITEMS
    )

    reading = CsnAdapter().fetch()

    assert reading.ok is True
    assert reading.error is None
    assert reading.source == "csn"
    assert [e.ref_geografica for e in reading.data] == [
        "78 km al SE de Socaire",
        "56 km al S de Caldera",
        "10 km al O de Iquique",
    ]


def test_fetch_returns_error_reading_on_failure(monkeypatch):
    def _raise():
        raise ValueError("bad response")

    monkeypatch.setattr("adapters.csn._fetch_earthquakes", _raise)

    reading = CsnAdapter().fetch()

    assert reading.ok is False
    assert reading.data == []
    assert "bad response" in reading.error


@pytest.mark.integration
def test_fetch_against_live_csn_api():
    reading = CsnAdapter().fetch()

    assert reading.source == "csn"
    if not reading.ok:
        pytest.fail(f"live CSN fetch failed: {reading.error}")


def test_config_defaults_when_env_vars_unset():
    config = _read_config_in_subprocess({})

    assert config["API_URL"] == "https://api.gael.cloud/general/public/sismos"
    assert config["SITE_URL"] == "https://www.sismologia.cl/"
    assert config["SOURCE_TZ"] == "America/Santiago"
    assert config["URGENT_MAGNITUDE_THRESHOLD"] == 4.5


def test_config_overridable_via_env_vars():
    config = _read_config_in_subprocess(
        {
            "ADAPTERS_CSN_API_URL": "https://example.test/sismos",
            "ADAPTERS_CSN_SITE_URL": "https://example.test/",
            "ADAPTERS_CSN_URGENT_MAGNITUDE_THRESHOLD": "5.0",
        }
    )

    assert config["API_URL"] == "https://example.test/sismos"
    assert config["SITE_URL"] == "https://example.test/"
    assert config["URGENT_MAGNITUDE_THRESHOLD"] == 5.0
