import json
import subprocess
import sys
from pathlib import Path

import pytest

from adapters.csn import API_URL, SITE_URL, CsnAdapter, CsnEarthquake, _parse_earthquake

_ADAPTERS_SRC = str(Path(__file__).resolve().parents[2] / "src")
_PRINT_CONFIG_SNIPPET = (
    "import json; from adapters.csn import API_URL, SITE_URL; "
    "print(json.dumps({'API_URL': API_URL, 'SITE_URL': SITE_URL}))"
)

_ADAPTERS_CSN_ENV_VARS = ("ADAPTERS_CSN_API_URL", "ADAPTERS_CSN_SITE_URL")


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
    assert earthquake.source_date_time == earthquake.fecha
    assert earthquake.type == "Sismo"
    assert earthquake.url == SITE_URL


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


def test_config_overridable_via_env_vars():
    config = _read_config_in_subprocess(
        {
            "ADAPTERS_CSN_API_URL": "https://example.test/sismos",
            "ADAPTERS_CSN_SITE_URL": "https://example.test/",
        }
    )

    assert config["API_URL"] == "https://example.test/sismos"
    assert config["SITE_URL"] == "https://example.test/"
