import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..base import DataSourceAdapter, SourceReading
from ..storage import get_setting
from ..timeutil import to_utc, utc_now

logger = logging.getLogger(__name__)

# Chile's Centro Sismológico Nacional (CSN, Universidad de Chile) has no
# official public API. This is a widely-used unofficial JSON mirror of its
# auto-detected earthquake list — no auth, no key required.
API_URL = get_setting(
    "ADAPTERS_CSN_API_URL", "https://api.gael.cloud/general/public/sismos"
)
# CSN's site has no per-earthquake detail page in this API's data (no id/slug
# is provided) — every item links to the same site homepage.
SITE_URL = get_setting("ADAPTERS_CSN_SITE_URL", "https://www.sismologia.cl/")
# Not every earthquake deserves the "urgent" dispatch policy's redelivery
# — below this magnitude, .dispatch_policy points at "informational"
# instead (single delivery, no redelivery).
URGENT_MAGNITUDE_THRESHOLD = float(
    get_setting("ADAPTERS_CSN_URGENT_MAGNITUDE_THRESHOLD", "4.5")
)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
# CSN's api.gael.cloud mirror gives no explicit offset on `Fecha` at all.
# A live comparison against the feed's own regeneration timestamp
# strongly suggests the underlying data is Chile local time throughout,
# not UTC — but there's no official documentation confirming this. Kept
# as an env var specifically so this assumption can be corrected in one
# line, without a code change, if it's ever proven wrong.
SOURCE_TZ = get_setting("ADAPTERS_CSN_SOURCE_TZ", "America/Santiago")


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
    # is used as both id and event_key — deliberately the RAW,
    # unconverted value (not source_date_time below), so existing stored
    # item_ids never change format. This is a permanent, deliberate
    # exception to this repo's "everything is UTC" rule — see root
    # README. Do not "fix" this to use source_date_time: doing so would
    # change every id, causing dispatcher to treat every historical
    # earthquake as newly-discovered again.
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
        """UTC-normalized for storage/dispatch/display. Deliberately NOT
        used for `id`/`event_key` above — those intentionally keep
        deriving from the raw, unconverted `fecha` so existing stored
        item_ids never change format."""
        return to_utc(self.fecha, assume_tz=SOURCE_TZ)

    @property
    def type(self) -> str:
        return "Sismo"

    @property
    def dispatch_policy(self) -> str:
        """Names a row in dispatcher's dispatch_policies table (see
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
        now = utc_now()
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
