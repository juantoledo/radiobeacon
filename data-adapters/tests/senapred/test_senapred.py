import json
import subprocess
import sys
from pathlib import Path

import pytest

from adapters.senapred import (
    ALERTA_BASE_URL,
    EVENTO_BASE_URL,
    SenapredAdapter,
    SenapredAlert,
    _parse_alerta,
)

_ADAPTERS_SRC = str(Path(__file__).resolve().parents[2] / "src")
_PRINT_CONFIG_SNIPPET = (
    "import json; from adapters.senapred import ("
    "IDENTITY_POOL_ID, COGNITO_REGION, APPSYNC_HOST, APPSYNC_ENDPOINT, "
    "ALERTA_BASE_URL, EVENTO_BASE_URL, QUERY_LIMIT); "
    "print(json.dumps({"
    "'IDENTITY_POOL_ID': IDENTITY_POOL_ID, 'COGNITO_REGION': COGNITO_REGION, "
    "'APPSYNC_HOST': APPSYNC_HOST, 'APPSYNC_ENDPOINT': APPSYNC_ENDPOINT, "
    "'ALERTA_BASE_URL': ALERTA_BASE_URL, 'EVENTO_BASE_URL': EVENTO_BASE_URL, "
    "'QUERY_LIMIT': QUERY_LIMIT}))"
)


_ADAPTERS_SENAPRED_ENV_VARS = (
    "ADAPTERS_SENAPRED_IDENTITY_POOL_ID",
    "ADAPTERS_SENAPRED_COGNITO_REGION",
    "ADAPTERS_SENAPRED_APPSYNC_REGION",
    "ADAPTERS_SENAPRED_APPSYNC_HOST",
    "ADAPTERS_SENAPRED_ALERTA_BASE_URL",
    "ADAPTERS_SENAPRED_EVENTO_BASE_URL",
    "ADAPTERS_SENAPRED_QUERY_LIMIT",
)


def _read_config_in_subprocess(env_overrides: dict) -> dict:
    """Runs a fresh Python process (isolated from this test session's
    already-imported adapters.senapred module) to verify the module-level
    config constants pick up env vars at import time."""
    import os as os_module

    env = {k: v for k, v in os_module.environ.items() if k not in _ADAPTERS_SENAPRED_ENV_VARS}
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
        "id": "1",
        "titulo": "Lluvias intensas Region de Coquimbo",
        "contenido": "<p>Acumulados <strong>40-60mm</strong> en 24h,"
        " riesgo de anegamientos &amp; crecidas</p>",
        "fechaHora": "2026-08-19T12:00:00.000Z",
        "autor": "SENAPRED",
        "isActive": True,
        "isDeleted": False,
        "type": "Alerta",
        "urlAccess": "lluvias-intensas-region-de-coquimbo-2026-08-19",
        "isPrincipal": True,
        "variableRiesgo": {"nombre": "Hidrometeorologico", "codigo": "HM"},
    },
    {
        "id": "2",
        "titulo": "Alerta desactivada",
        "contenido": "Ya no vigente",
        "fechaHora": "2026-08-10T08:00:00.000Z",
        "autor": "SENAPRED",
        "isActive": False,
        "isDeleted": False,
        "type": "Alerta",
        "urlAccess": None,
        "isPrincipal": False,
        "variableRiesgo": None,
    },
    {
        "id": "3",
        "titulo": "Alerta eliminada",
        "contenido": "Borrada por el operador",
        "fechaHora": "2026-08-05T08:00:00.000Z",
        "autor": "SENAPRED",
        "isActive": True,
        "isDeleted": True,
        "type": "Alerta",
        "urlAccess": None,
        "isPrincipal": False,
        "variableRiesgo": None,
    },
]

# "Evento" items (e.g. seismic activity) come from a separate GraphQL field
# (eventosByDate) with no variableRiesgo — it's simply absent from the dict,
# not None, since it's not part of that type's schema at all.
MOCK_EVENTO_ITEMS = [
    {
        "id": "4",
        "titulo": "Sismo de menor intensidad en la Región de Coquimbo",
        "contenido": "Sismo detectado sin daños reportados",
        "fechaHora": "2026-08-19T13:28:28.000-04:00",
        "autor": "SENAPRED",
        "isActive": True,
        "isDeleted": False,
        "type": "Evento",
        "urlAccess": "sismo-de-menor-intensidad-en-la-region-de-coquimbo-2026-08-19-13-28-28",
        "isPrincipal": True,
    },
]


def test_parse_alerta_maps_fields():
    alert = _parse_alerta(MOCK_ITEMS[0])

    assert isinstance(alert, SenapredAlert)
    assert alert.id == "1"
    assert alert.titulo == "Lluvias intensas Region de Coquimbo"
    assert alert.tipo == "Hidrometeorologico"
    assert alert.type == "Alerta"
    assert alert.is_principal is True
    assert "experimental" in alert.experimental_disclaimer


def test_parse_alerta_handles_missing_variable_riesgo():
    alert = _parse_alerta(MOCK_ITEMS[1])

    assert alert.tipo is None
    assert alert.url_access is None


def test_generic_contract_fields_are_inferred():
    alert = _parse_alerta(MOCK_ITEMS[0])

    assert alert.title == "Lluvias intensas Region de Coquimbo"
    assert alert.contents == "Acumulados 40-60mm en 24h, riesgo de anegamientos & crecidas"
    assert alert.url == ALERTA_BASE_URL + "lluvias-intensas-region-de-coquimbo-2026-08-19"
    assert alert.source_date_time == alert.fecha_hora
    assert alert.event_key == alert.url_access
    assert alert.subtype == alert.tipo
    assert alert.dispatch_policy == "urgent"


def test_generic_contract_dispatch_policy_is_informational_for_evento_type():
    evento = _parse_alerta(MOCK_EVENTO_ITEMS[0])

    assert evento.dispatch_policy == "informational"


def test_generic_contract_url_uses_evento_base_url_for_evento_type():
    evento = _parse_alerta(MOCK_EVENTO_ITEMS[0])

    assert evento.url == (
        EVENTO_BASE_URL
        + "sismo-de-menor-intensidad-en-la-region-de-coquimbo-2026-08-19-13-28-28"
    )


def test_generic_contract_event_key_is_none_without_url_access():
    alert = _parse_alerta(MOCK_ITEMS[1])

    assert alert.event_key is None


def test_generic_contract_url_is_none_without_url_access():
    alert = _parse_alerta(MOCK_ITEMS[1])

    assert alert.url is None


def test_generic_contract_subtype_is_none_without_variable_riesgo():
    alert = _parse_alerta(MOCK_ITEMS[1])

    assert alert.subtype is None


def test_generic_contract_type_distinguishes_alerta_and_evento():
    alerta = _parse_alerta(MOCK_ITEMS[0])
    evento = _parse_alerta(MOCK_EVENTO_ITEMS[0])

    assert alerta.type == "Alerta"
    assert evento.type == "Evento"


def test_fetch_filters_inactive_and_deleted(monkeypatch):
    monkeypatch.setattr(
        "adapters.senapred._get_anonymous_credentials", lambda: object()
    )
    monkeypatch.setattr(
        "adapters.senapred._query_alertas", lambda credentials, limit=20: MOCK_ITEMS
    )
    monkeypatch.setattr(
        "adapters.senapred._query_eventos", lambda credentials, limit=20: []
    )

    reading = SenapredAdapter().fetch()

    assert reading.ok is True
    assert reading.error is None
    assert reading.source == "senapred"
    assert len(reading.data) == 1
    assert reading.data[0].id == "1"


def test_fetch_includes_eventos_alongside_alertas(monkeypatch):
    """Regression test: senapred.cl/eventos/ merges "Alerta" and "Evento"
    feeds (e.g. seismic activity is an Evento) — the adapter must too."""
    monkeypatch.setattr(
        "adapters.senapred._get_anonymous_credentials", lambda: object()
    )
    monkeypatch.setattr(
        "adapters.senapred._query_alertas", lambda credentials, limit=20: MOCK_ITEMS
    )
    monkeypatch.setattr(
        "adapters.senapred._query_eventos",
        lambda credentials, limit=20: MOCK_EVENTO_ITEMS,
    )

    reading = SenapredAdapter().fetch()

    assert reading.ok is True
    ids = [alert.id for alert in reading.data]
    assert ids == ["4", "1"]  # sorted newest first: Evento (13:28) before Alerta (12:00)


def test_fetch_returns_error_reading_on_failure(monkeypatch):
    def _raise():
        raise RuntimeError("cognito unreachable")

    monkeypatch.setattr("adapters.senapred._get_anonymous_credentials", _raise)

    reading = SenapredAdapter().fetch()

    assert reading.ok is False
    assert reading.data == []
    assert "cognito unreachable" in reading.error


@pytest.mark.integration
def test_fetch_against_live_senapred_backend():
    reading = SenapredAdapter().fetch()

    assert reading.source == "senapred"
    if not reading.ok:
        pytest.fail(f"live SENAPRED fetch failed: {reading.error}")


def test_config_defaults_when_env_vars_unset():
    config = _read_config_in_subprocess({})

    assert config["IDENTITY_POOL_ID"] == (
        "us-east-1:17c696bc-53e1-49a2-991f-f1b65f752fda"
    )
    assert config["COGNITO_REGION"] == "us-east-1"
    assert config["APPSYNC_HOST"] == (
        "rz2uv7ifxbgflh2bqmp6kmh4le.appsync-api.us-east-1.amazonaws.com"
    )
    assert config["ALERTA_BASE_URL"] == "https://senapred.cl/alerta/"
    assert config["EVENTO_BASE_URL"] == "https://senapred.cl/evento/"
    assert config["QUERY_LIMIT"] == 20


def test_config_overridable_via_env_vars():
    config = _read_config_in_subprocess(
        {
            "ADAPTERS_SENAPRED_IDENTITY_POOL_ID": "us-west-2:fake-pool-id",
            "ADAPTERS_SENAPRED_COGNITO_REGION": "us-west-2",
            "ADAPTERS_SENAPRED_APPSYNC_HOST": "example.appsync-api.us-west-2.amazonaws.com",
            "ADAPTERS_SENAPRED_ALERTA_BASE_URL": "https://example.test/alerta/",
            "ADAPTERS_SENAPRED_EVENTO_BASE_URL": "https://example.test/evento/",
            "ADAPTERS_SENAPRED_QUERY_LIMIT": "5",
        }
    )

    assert config["IDENTITY_POOL_ID"] == "us-west-2:fake-pool-id"
    assert config["COGNITO_REGION"] == "us-west-2"
    assert config["APPSYNC_HOST"] == "example.appsync-api.us-west-2.amazonaws.com"
    assert config["APPSYNC_ENDPOINT"] == (
        "https://example.appsync-api.us-west-2.amazonaws.com/graphql"
    )
    assert config["ALERTA_BASE_URL"] == "https://example.test/alerta/"
    assert config["EVENTO_BASE_URL"] == "https://example.test/evento/"
    assert config["QUERY_LIMIT"] == 5
