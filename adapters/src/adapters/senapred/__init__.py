import html
import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from ..base import DataSourceAdapter, SourceReading

logger = logging.getLogger(__name__)

# senapred.cl/eventos/ is a client-rendered SPA with no server-rendered data.
# Its JS bundle embeds a real backend: an AWS AppSync GraphQL API, reached
# anonymously via a public Cognito Identity Pool (AWS_IAM auth) when no user
# is logged in — the same flow every visitor's browser uses. None of this is
# secret (it's public in senapred.cl's own JS bundle), but it's still
# environment-configurable in case SENAPRED changes their infra or someone
# wants to point this at a different instance.
IDENTITY_POOL_ID = os.environ.get(
    "ADAPTERS_SENAPRED_IDENTITY_POOL_ID", "us-east-1:17c696bc-53e1-49a2-991f-f1b65f752fda"
)
COGNITO_REGION = os.environ.get("ADAPTERS_SENAPRED_COGNITO_REGION", "us-east-1")
COGNITO_ENDPOINT = f"https://cognito-identity.{COGNITO_REGION}.amazonaws.com/"

APPSYNC_REGION = os.environ.get("ADAPTERS_SENAPRED_APPSYNC_REGION", "us-east-1")
APPSYNC_HOST = os.environ.get(
    "ADAPTERS_SENAPRED_APPSYNC_HOST",
    "rz2uv7ifxbgflh2bqmp6kmh4le.appsync-api.us-east-1.amazonaws.com",
)
APPSYNC_ENDPOINT = f"https://{APPSYNC_HOST}/graphql"

# senapred.cl's router serves per-item detail pages under a singular,
# type-specific path — /alerta/{slug} or /evento/{slug} — not the plural
# /eventos/ listing page. Confirmed from the site's own JS bundle: it
# defines a dynamic route "/:typePath/:urlPath" alongside separate
# constants "/alerta/" and "/evento/" used to build these links depending
# on which feed (Alerta/Evento) an item came from.
ALERTA_BASE_URL = os.environ.get(
    "ADAPTERS_SENAPRED_ALERTA_BASE_URL", "https://senapred.cl/alerta/"
)
EVENTO_BASE_URL = os.environ.get(
    "ADAPTERS_SENAPRED_EVENTO_BASE_URL", "https://senapred.cl/evento/"
)
QUERY_LIMIT = int(os.environ.get("ADAPTERS_SENAPRED_QUERY_LIMIT", "20"))

# SENAPRED's backend splits content into two separate feeds/GraphQL fields,
# not one: "Alerta" (weather/volcanic warnings, has a variableRiesgo risk
# category) and "Evento" (a different schema — no variableRiesgo — covers
# things like seismic activity). senapred.cl/eventos/ renders both merged;
# querying only alertasByDate silently misses everything under "Evento".
ALERTAS_BY_DATE_QUERY = """
query AlertasByDate($type: String!, $sortDirection: ModelSortDirection, $limit: Int) {
  alertasByDate(type: $type, sortDirection: $sortDirection, limit: $limit) {
    items {
      id
      titulo
      contenido
      fechaHora
      autor
      isActive
      isDeleted
      type
      urlAccess
      isPrincipal
      variableRiesgo {
        nombre
        codigo
      }
    }
    nextToken
  }
}
"""

EVENTOS_BY_DATE_QUERY = """
query EventosByDate($type: String!, $sortDirection: ModelSortDirection, $limit: Int) {
  eventosByDate(type: $type, sortDirection: $sortDirection, limit: $limit) {
    items {
      id
      titulo
      contenido
      fechaHora
      autor
      isActive
      isDeleted
      type
      urlAccess
      isPrincipal
    }
    nextToken
  }
}
"""


_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return _WHITESPACE_RE.sub(" ", text).strip()


@dataclass
class SenapredAlert:
    id: str
    titulo: str
    contenido: str
    fecha_hora: datetime
    autor: str | None
    tipo: str | None
    type: str | None
    url_access: str | None
    is_principal: bool
    experimental_disclaimer: str = (
        "fuente experimental no oficial, consulte SENAPRED"
    )

    # Generic item contract (see adapters.storage.store_reading): these are
    # properties, not dataclass fields, so they're excluded from rawdata's
    # dataclasses.asdict() serialization and only used for the items table's
    # dedicated columns.
    @property
    def title(self) -> str:
        return self.titulo

    @property
    def contents(self) -> str:
        """`contenido` is raw HTML from senapred.cl's CMS; infer plain-text
        contents from it for storage/formatting downstream."""
        return _strip_html(self.contenido)

    @property
    def url(self) -> str | None:
        if not self.url_access:
            return None
        base_url = EVENTO_BASE_URL if self.type == "Evento" else ALERTA_BASE_URL
        return base_url + self.url_access

    @property
    def source_date_time(self) -> datetime:
        return self.fecha_hora

    @property
    def event_key(self) -> str | None:
        """SENAPRED assigns `url_access` once when an event is first
        declared, and every subsequent update to that event (monitored,
        modified, cancelled) reuses it — it's the shared identifier that
        groups an event's separate item snapshots into one timeline. Not a
        SENAPRED-specific concept in the generic contract's terms: any
        adapter with a natural "these items are updates to the same
        thing" key can expose it as `event_key`."""
        return self.url_access

    @property
    def subtype(self) -> str | None:
        """`tipo` (from `variableRiesgo.nombre`, e.g. "Hidrometeorologico")
        is the risk category — exposed under the generic `subtype` name
        alongside `type` ("Alerta"/"Evento"), so any adapter with a
        two-level category (broad type + finer-grained subtype) can use
        the same columns."""
        return self.tipo


def _post_json(url: str, headers: dict[str, str], body: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=body.encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_anonymous_credentials() -> Credentials:
    """Mirrors senapred.cl's own anonymous-visitor auth flow: an
    unauthenticated Cognito Identity Pool session, no login required."""
    logger.debug("requesting anonymous Cognito identity credentials")
    identity = _post_json(
        COGNITO_ENDPOINT,
        {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityService.GetId",
        },
        json.dumps({"IdentityPoolId": IDENTITY_POOL_ID}),
    )
    creds = _post_json(
        COGNITO_ENDPOINT,
        {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity",
        },
        json.dumps({"IdentityId": identity["IdentityId"]}),
    )["Credentials"]
    logger.debug("obtained anonymous Cognito identity credentials")
    return Credentials(
        access_key=creds["AccessKeyId"],
        secret_key=creds["SecretKey"],
        token=creds["SessionToken"],
    )


def _query(
    credentials: Credentials, query: str, field_name: str, type_value: str, limit: int
) -> list[dict[str, Any]]:
    logger.debug("querying %s (type=%s, limit=%d)", field_name, type_value, limit)
    body = json.dumps(
        {
            "query": query,
            "variables": {
                "type": type_value,
                "sortDirection": "DESC",
                "limit": limit,
            },
        }
    )

    request = AWSRequest(
        method="POST",
        url=APPSYNC_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/json", "host": APPSYNC_HOST},
    )
    SigV4Auth(credentials, "appsync", APPSYNC_REGION).add_auth(request)

    response = _post_json(APPSYNC_ENDPOINT, dict(request.headers), body)
    if "errors" in response:
        raise RuntimeError(f"AppSync GraphQL error: {response['errors']}")
    return response["data"][field_name]["items"]


def _query_alertas(
    credentials: Credentials, limit: int = QUERY_LIMIT
) -> list[dict[str, Any]]:
    return _query(credentials, ALERTAS_BY_DATE_QUERY, "alertasByDate", "Alerta", limit)


def _query_eventos(
    credentials: Credentials, limit: int = QUERY_LIMIT
) -> list[dict[str, Any]]:
    return _query(credentials, EVENTOS_BY_DATE_QUERY, "eventosByDate", "Evento", limit)


def _parse_alerta(item: dict[str, Any]) -> SenapredAlert:
    variable_riesgo = item.get("variableRiesgo") or {}
    return SenapredAlert(
        id=item["id"],
        titulo=item["titulo"],
        contenido=item["contenido"],
        fecha_hora=datetime.fromisoformat(item["fechaHora"]),
        autor=item.get("autor"),
        tipo=variable_riesgo.get("nombre"),
        type=item.get("type"),
        url_access=item.get("urlAccess"),
        is_principal=bool(item.get("isPrincipal")),
    )


class SenapredAdapter(DataSourceAdapter):
    """Fetches active early-warning alerts from senapred.cl's real backend
    (AWS AppSync GraphQL, reached via anonymous Cognito identity — see
    module docstring above). Queries both the "Alerta" and "Evento" feeds,
    since senapred.cl's own /eventos/ page merges both."""

    def fetch(self) -> SourceReading:
        now = datetime.now()
        try:
            credentials = _get_anonymous_credentials()
            raw_items = _query_alertas(credentials) + _query_eventos(credentials)
            alerts = []
            for item in raw_items:
                if not (item.get("isActive") and not item.get("isDeleted")):
                    continue
                alert = _parse_alerta(item)
                logger.info(
                    "retrieved alert id=%s title=%r source_date_time=%s",
                    alert.id,
                    alert.title,
                    alert.source_date_time,
                )
                alerts.append(alert)
            alerts.sort(key=lambda a: a.fecha_hora, reverse=True)
            logger.info(
                "fetched %d active item(s) (%d total returned)",
                len(alerts),
                len(raw_items),
            )
            return SourceReading(
                source="senapred", fetched_at=now, ok=True, data=alerts
            )
        except (urllib.error.URLError, KeyError, RuntimeError, ValueError) as e:
            logger.error("fetch failed: %s", e, exc_info=True)
            return SourceReading(
                source="senapred", fetched_at=now, ok=False, data=[], error=str(e)
            )
