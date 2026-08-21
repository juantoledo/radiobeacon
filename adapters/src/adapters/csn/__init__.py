import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..base import DataSourceAdapter, SourceReading

logger = logging.getLogger(__name__)

# Chile's Centro Sismológico Nacional (CSN, Universidad de Chile) has no
# official public API. This is a widely-used unofficial JSON mirror of its
# auto-detected earthquake list — no auth, no key required.
API_URL = os.environ.get(
    "ADAPTERS_CSN_API_URL", "https://api.gael.cloud/general/public/sismos"
)
# CSN's site has no per-earthquake detail page in this API's data (no id/slug
# is provided) — every item links to the same site homepage.
SITE_URL = os.environ.get("ADAPTERS_CSN_SITE_URL", "https://www.sismologia.cl/")
# Not every earthquake deserves the "urgent" dispatch policy's redelivery
# — below this magnitude, .dispatch_policy points at "informational"
# instead (single delivery, no redelivery).
URGENT_MAGNITUDE_THRESHOLD = float(
    os.environ.get("ADAPTERS_CSN_URGENT_MAGNITUDE_THRESHOLD", "4.5")
)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass
class CsnEarthquake:
    fecha: datetime
    profundidad_km: float
    magnitud: float
    ref_geografica: str
    experimental_disclaimer: str = (
        "fuente experimental no oficial, consulte CSN (sismologia.cl)"
    )

    # Generic item contract (see adapters.storage.store_reading). The API
    # gives no stable id, so fecha (the earthquake's detection timestamp)
    # is used as both id and event_key.
    @property
    def id(self) -> str:
        return self.fecha.isoformat()

    @property
    def event_key(self) -> str:
        return self.fecha.isoformat()

    @property
    def url(self) -> str:
        return SITE_URL

    @property
    def title(self) -> str:
        return f"Sismo M{self.magnitud} - {self.ref_geografica}"

    @property
    def contents(self) -> str:
        return (
            f"Sismo de magnitud {self.magnitud}, profundidad "
            f"{self.profundidad_km} km, {self.ref_geografica}."
        )

    @property
    def source_date_time(self) -> datetime:
        return self.fecha

    @property
    def type(self) -> str:
        return "Sismo"

    @property
    def dispatch_policy(self) -> str:
        """Names a row in triggers' dispatch_policies table (see
        adapters.storage.store_reading's docstring). Earthquakes at/above
        URGENT_MAGNITUDE_THRESHOLD point at "urgent" (redelivered several
        times); below it, "informational" (delivered once) — a minor
        earthquake doesn't need repeated broadcasts."""
        if self.magnitud < URGENT_MAGNITUDE_THRESHOLD:
            return "informational"
        return "urgent"


def _parse_earthquake(item: dict[str, Any]) -> CsnEarthquake:
    return CsnEarthquake(
        fecha=datetime.strptime(item["Fecha"], _DATE_FORMAT),
        profundidad_km=float(item["Profundidad"]),
        magnitud=float(item["Magnitud"]),
        ref_geografica=item["RefGeografica"],
    )


def _fetch_earthquakes() -> list[dict[str, Any]]:
    # A real browser User-Agent is required — this API's WAF rejects the
    # default "Python-urllib/x.y" one with a 403, even though the request
    # itself needs no auth/key.
    request = urllib.request.Request(
        API_URL,
        headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


class CsnAdapter(DataSourceAdapter):
    """Fetches recent earthquakes from CSN's unofficial public API."""

    def fetch(self) -> SourceReading:
        now = datetime.now()
        try:
            raw_items = _fetch_earthquakes()
            earthquakes = []
            for item in raw_items:
                earthquake = _parse_earthquake(item)
                logger.info(
                    "retrieved earthquake id=%s title=%r source_date_time=%s",
                    earthquake.id,
                    earthquake.title,
                    earthquake.source_date_time,
                )
                earthquakes.append(earthquake)
            earthquakes.sort(key=lambda e: e.fecha, reverse=True)
            logger.info("fetched %d earthquake(s)", len(earthquakes))
            return SourceReading(
                source="csn", fetched_at=now, ok=True, data=earthquakes
            )
        except (urllib.error.URLError, KeyError, ValueError) as e:
            logger.error("fetch failed: %s", e, exc_info=True)
            return SourceReading(
                source="csn", fetched_at=now, ok=False, data=[], error=str(e)
            )
