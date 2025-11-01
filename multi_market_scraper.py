"""Multi-platform market scraper.

This module provides a configurable scraping workflow that can target
multiple marketplace platforms (e.g. Leboncoin, Vinted, eBay).  It offers
utilities for HTTP requests with retry/delay handling, per-platform
scrapers that normalise listings, filtering helpers, CSV exporting and
alert dispatching.

The default implementation favours clarity over API completeness.  Real
world integrations can extend the provided scraper functions while
re-using the shared infrastructure.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

try:  # pragma: no cover - optional dependency
    import requests
except ModuleNotFoundError:  # pragma: no cover - fallback
    requests = None  # type: ignore


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration dataclasses


@dataclass
class RequestSettings:
    """Settings applied to every HTTP request."""

    delay: float = 1.0
    max_retries: int = 3
    retry_backoff: float = 1.5
    timeout: float = 10.0
    headers: Dict[str, str] = field(
        default_factory=lambda: {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/118.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
        }
    )


@dataclass
class PlatformConfig:
    """Configuration for a given marketplace platform."""

    name: str
    base_url: str
    search_path: str
    enabled: bool = True
    extra_params: Dict[str, Any] = field(default_factory=dict)

    def search_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.search_path.lstrip('/')}"


@dataclass
class AlertSettings:
    """Settings describing which alert channels to use."""

    channels: List[str] = field(default_factory=lambda: ["console"])
    email_to: Optional[str] = None
    email_from: Optional[str] = None
    smtp_host: str = "localhost"
    smtp_port: int = 25
    webhook_url: Optional[str] = None


@dataclass
class SearchConfig:
    """Top-level configuration for a scraping run."""

    keywords: List[str]
    max_price: Optional[float] = None
    min_price: Optional[float] = None
    currency: str = "EUR"
    radius_km: Optional[float] = None
    postal_code: Optional[str] = None
    platforms: List[PlatformConfig] = field(default_factory=list)
    request: RequestSettings = field(default_factory=RequestSettings)
    alerts: AlertSettings = field(default_factory=AlertSettings)
    export_path: Path = Path("listings.csv")
    use_sample_data: bool = False

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "SearchConfig":
        request = RequestSettings(**raw.get("request", {}))
        alerts = AlertSettings(**raw.get("alerts", {}))
        platforms = [PlatformConfig(**cfg) for cfg in raw.get("platforms", [])]
        return SearchConfig(
            keywords=list(raw.get("keywords", [])),
            max_price=raw.get("max_price"),
            min_price=raw.get("min_price"),
            currency=raw.get("currency", "EUR"),
            radius_km=raw.get("radius_km"),
            postal_code=raw.get("postal_code"),
            platforms=platforms,
            request=request,
            alerts=alerts,
            export_path=Path(raw.get("export_path", "listings.csv")),
            use_sample_data=bool(raw.get("use_sample_data", False)),
        )


# ---------------------------------------------------------------------------
# HTTP utility


class HttpClient:
    """Simple HTTP client that respects delays and retries."""

    def __init__(self, settings: RequestSettings) -> None:
        self.settings = settings
        self.session = None
        if requests is not None:
            self.session = requests.Session()
            self.session.headers.update(settings.headers)
        self._last_request_at: Optional[float] = None

    def _respect_delay(self) -> None:
        if self._last_request_at is None:
            return
        elapsed = time.time() - self._last_request_at
        if elapsed < self.settings.delay:
            sleep_for = self.settings.delay - elapsed
            logger.debug("Sleeping %.2fs to respect delay", sleep_for)
            time.sleep(max(0, sleep_for))

    def get_json(self, url: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        """Perform a GET request returning parsed JSON."""

        attempts = 0
        clean_params = _clean_params(params)
        while attempts <= self.settings.max_retries:
            attempts += 1
            self._respect_delay()
            try:
                logger.debug("GET %s params=%s attempt=%s", url, clean_params, attempts)
                if self.session is not None:
                    response = self.session.get(
                        url,
                        params=clean_params,
                        timeout=self.settings.timeout,
                    )
                    self._last_request_at = time.time()
                    response.raise_for_status()
                    return response.json()

                # Fallback to urllib when requests is unavailable
                request_headers = dict(self.settings.headers)
                if clean_params:
                    url_with_params = f"{url}?{urllib.parse.urlencode(clean_params)}"
                else:
                    url_with_params = url
                request = urllib.request.Request(url_with_params, headers=request_headers)
                with urllib.request.urlopen(
                    request, timeout=self.settings.timeout
                ) as response:  # noqa: S310 - trusted destinations configured by user
                    self._last_request_at = time.time()
                    payload = response.read().decode("utf-8")
                    return json.loads(payload)
            except Exception as exc:  # noqa: BLE001 - high level catch to retry
                logger.warning("Request to %s failed (%s)", url, exc)
                if attempts > self.settings.max_retries:
                    raise
                backoff = self.settings.retry_backoff ** (attempts - 1)
                logger.debug("Retrying in %.2fs", backoff)
                time.sleep(backoff)


# Utility helpers ----------------------------------------------------------


def _clean_params(params: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Remove null/empty values from query dictionaries."""

    if not params:
        return {}

    clean: Dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, str):
            if not value.strip():
                continue
            clean[key] = value
            continue
        if isinstance(value, Mapping) and not value:
            continue
        if isinstance(value, (list, tuple, set)) and not value:
            continue
        clean[key] = value
    return clean


# ---------------------------------------------------------------------------
# Listing model and helpers


@dataclass
class Listing:
    platform: str
    title: str
    price: Optional[float]
    currency: str
    url: str
    location: Optional[str] = None
    distance_km: Optional[float] = None
    posted_at: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def matches_keywords(self, keywords: Iterable[str]) -> bool:
        keywords = [kw.lower() for kw in keywords if kw]
        if not keywords:
            return True
        haystack = " ".join(
            filter(None, [self.title, json.dumps(self.raw, ensure_ascii=False)])
        ).lower()
        return any(keyword in haystack for keyword in keywords)

    def to_row(self) -> Dict[str, Any]:
        data = asdict(self)
        # Remove raw payload from CSV output to keep file small
        data.pop("raw", None)
        return data


def filter_listings(listings: Iterable[Listing], config: SearchConfig) -> List[Listing]:
    """Filter listings according to price, keywords and radius constraints."""

    filtered: List[Listing] = []
    for listing in listings:
        if config.keywords and not listing.matches_keywords(config.keywords):
            logger.debug("Skipping %s - keywords mismatch", listing.title)
            continue
        if config.max_price is not None and listing.price is not None:
            if listing.price > config.max_price:
                logger.debug("Skipping %s - price %.2f above max", listing.title, listing.price)
                continue
        if config.min_price is not None and listing.price is not None:
            if listing.price < config.min_price:
                logger.debug("Skipping %s - price %.2f below min", listing.title, listing.price)
                continue
        if config.radius_km is not None and listing.distance_km is not None:
            if listing.distance_km > config.radius_km:
                logger.debug(
                    "Skipping %s - distance %.2f > %.2f",
                    listing.title,
                    listing.distance_km,
                    config.radius_km,
                )
                continue
        filtered.append(listing)
    return filtered


# ---------------------------------------------------------------------------
# Scraper implementations (demo-friendly)


SAMPLE_RESPONSES: Dict[str, List[Dict[str, Any]]] = {
    "leboncoin": [
        {
            "title": "iPhone 13 Pro Max",
            "price": 680,
            "currency": "EUR",
            "url": "https://www.leboncoin.fr/iphone-13-pro-max",
            "location": "Paris",
            "distance_km": 4.0,
            "posted_at": "2024-03-20T09:00:00",
        },
        {
            "title": "PS4 Slim 500Go",
            "price": 140,
            "currency": "EUR",
            "url": "https://www.leboncoin.fr/ps4-slim",
            "location": "Versailles",
            "distance_km": 18.0,
            "posted_at": "2024-03-18T12:30:00",
        },
    ],
    "vinted": [
        {
            "title": "Nike Air Jordan 1",
            "price": 120,
            "currency": "EUR",
            "url": "https://www.vinted.fr/jordan-1",
            "location": "Lyon",
            "distance_km": 460.0,
            "posted_at": "2024-03-21T10:00:00",
        }
    ],
    "ebay": [
        {
            "title": "Apple iPhone 13 Pro Max - 256GB",
            "price": 720,
            "currency": "EUR",
            "url": "https://www.ebay.fr/itm/iphone-13-pro-max",
            "location": "Berlin",
            "distance_km": None,
            "posted_at": "2024-03-19T08:45:00",
        }
    ],
}


def _normalise_item(platform: str, item: Mapping[str, Any], currency: str) -> Listing:
    price = item.get("price")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None

    return Listing(
        platform=platform,
        title=str(item.get("title", "")),
        price=price,
        currency=str(item.get("currency", currency)),
        url=str(item.get("url", "")),
        location=item.get("location"),
        distance_km=_coerce_float(item.get("distance_km")),
        posted_at=item.get("posted_at"),
        raw=dict(item),
    )


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _radius_to_int(radius: Optional[float]) -> Optional[int]:
    if radius is None:
        return None
    try:
        return int(round(float(radius)))
    except (TypeError, ValueError):
        logger.debug("Invalid radius value: %s", radius)
        return None


def scrape_leboncoin(
    config: SearchConfig, http_client: Optional[HttpClient], platform: PlatformConfig
) -> List[Listing]:
    if config.use_sample_data:
        logger.debug("Using sample data for Leboncoin")
        items = SAMPLE_RESPONSES["leboncoin"]
        return [_normalise_item("Leboncoin", item, config.currency) for item in items]

    if http_client is None:
        raise RuntimeError("HTTP client unavailable for live Leboncoin scraping")

    params = {
        "text": " ".join(config.keywords),
        "price_min": config.min_price,
        "price_max": config.max_price,
        "zipcode": config.postal_code,
        "radius": _radius_to_int(config.radius_km),
        **platform.extra_params,
    }

    try:
        response = http_client.get_json(platform.search_url(), params=params)
        items = response.get("ads", []) if isinstance(response, Mapping) else response
    except Exception:
        logger.info("Falling back to sample data for Leboncoin")
        items = SAMPLE_RESPONSES["leboncoin"]

    return [_normalise_item("Leboncoin", item, config.currency) for item in items]


def scrape_vinted(
    config: SearchConfig, http_client: Optional[HttpClient], platform: PlatformConfig
) -> List[Listing]:
    if config.use_sample_data:
        logger.debug("Using sample data for Vinted")
        items = SAMPLE_RESPONSES["vinted"]
        return [_normalise_item("Vinted", item, config.currency) for item in items]

    if http_client is None:
        raise RuntimeError("HTTP client unavailable for live Vinted scraping")

    params = {
        "search_text": " ".join(config.keywords),
        "price_from": config.min_price,
        "price_to": config.max_price,
        "search_postal_code": config.postal_code,
        "search_radius": _radius_to_int(config.radius_km),
        **platform.extra_params,
    }

    try:
        response = http_client.get_json(platform.search_url(), params=params)
        items = response.get("items", []) if isinstance(response, Mapping) else response
    except Exception:
        logger.info("Falling back to sample data for Vinted")
        items = SAMPLE_RESPONSES["vinted"]

    return [_normalise_item("Vinted", item, config.currency) for item in items]


def scrape_ebay(
    config: SearchConfig, http_client: Optional[HttpClient], platform: PlatformConfig
) -> List[Listing]:
    if config.use_sample_data:
        logger.debug("Using sample data for eBay")
        items = SAMPLE_RESPONSES["ebay"]
        return [_normalise_item("eBay", item, config.currency) for item in items]

    if http_client is None:
        raise RuntimeError("HTTP client unavailable for live eBay scraping")

    params = {
        "_nkw": " ".join(config.keywords),
        "_udlo": config.min_price,
        "_udhi": config.max_price,
        **platform.extra_params,
    }

    postal_code = config.postal_code
    if postal_code:
        params["buyerPostalCode"] = postal_code

    radius = _radius_to_int(config.radius_km)
    if radius is not None and postal_code:
        params["itemFilter(0).name"] = "MaxDistance"
        params["itemFilter(0).value"] = radius

    try:
        response = http_client.get_json(platform.search_url(), params=params)
        items = response.get("items", []) if isinstance(response, Mapping) else response
    except Exception:
        logger.info("Falling back to sample data for eBay")
        items = SAMPLE_RESPONSES["ebay"]

    return [_normalise_item("eBay", item, config.currency) for item in items]


SCRAPERS = {
    "leboncoin": scrape_leboncoin,
    "vinted": scrape_vinted,
    "ebay": scrape_ebay,
}


# ---------------------------------------------------------------------------
# Export helpers


def export_to_csv(listings: Iterable[Listing], output_path: Path) -> None:
    listings = list(listings)
    if not listings:
        logger.info("No listings to export")
        return

    fieldnames = list(listings[0].to_row().keys())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for listing in listings:
            writer.writerow(listing.to_row())
    logger.info("Exported %s listings to %s", len(listings), output_path)


# ---------------------------------------------------------------------------
# Alert dispatching


class AlertDispatcher:
    def __init__(self, settings: AlertSettings):
        self.settings = settings

    def dispatch(self, listings: Iterable[Listing]) -> None:
        listings = list(listings)
        if not listings:
            logger.info("No listings to alert")
            return

        for channel in self.settings.channels:
            handler = getattr(self, f"_send_{channel}_alert", None)
            if handler is None:
                logger.warning("Unknown alert channel: %s", channel)
                continue
            try:
                handler(listings)
            except Exception as exc:  # noqa: BLE001 - keep alerts resilient
                logger.error("Failed to send %s alert: %s", channel, exc)

    # Individual channels -------------------------------------------------

    def _send_console_alert(self, listings: Iterable[Listing]) -> None:
        logger.info("Sending console alert for %s listings", len(listings))
        for listing in listings:
            logger.info("[%s] %s - %.2f %s (%s)", listing.platform, listing.title, listing.price or 0.0, listing.currency, listing.url)

    def _send_email_alert(self, listings: Iterable[Listing]) -> None:
        if not (self.settings.email_to and self.settings.email_from):
            logger.warning("Email alert configured without recipients")
            return

        body_lines = [
            f"Platform: {listing.platform}\nTitle: {listing.title}\nPrice: {listing.price} {listing.currency}\nURL: {listing.url}\n"
            for listing in listings
        ]
        message = EmailMessage()
        message["Subject"] = f"{len(listings)} new marketplace listing(s)"
        message["From"] = self.settings.email_from
        message["To"] = self.settings.email_to
        message.set_content("\n".join(body_lines))

        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=10) as smtp:
            smtp.send_message(message)
        logger.info("Email alert sent to %s", self.settings.email_to)

    def _send_webhook_alert(self, listings: Iterable[Listing]) -> None:
        if not self.settings.webhook_url:
            logger.warning("Webhook alert configured without URL")
            return

        payload = {
            "text": "New marketplace listings",
            "items": [listing.to_row() for listing in listings],
        }
        if requests is not None:
            response = requests.post(
                self.settings.webhook_url, json=payload, timeout=10
            )
            response.raise_for_status()
        else:
            data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                self.settings.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=10):  # noqa: S310
                pass
        logger.info("Webhook alert sent to %s", self.settings.webhook_url)


# ---------------------------------------------------------------------------
# Configuration utilities


def load_config_from_file(path: Path) -> SearchConfig:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return SearchConfig.from_dict(data)


def default_config() -> SearchConfig:
    return SearchConfig(
        keywords=["iphone", "ps4", "jordan"],
        max_price=750,
        radius_km=25,
        postal_code="75000",
        platforms=[
            PlatformConfig(
                name="leboncoin",
                base_url="https://api.leboncoin.fr",
                search_path="finder/search",
            ),
            PlatformConfig(
                name="vinted",
                base_url="https://www.vinted.fr/api",
                search_path="catalog/items",
                extra_params={"order": "newest_first"},
            ),
            PlatformConfig(
                name="ebay",
                base_url="https://svcs.ebay.com",
                search_path="services/search/FindingService/v1",
                extra_params={"SERVICE-VERSION": "1.0.0"},
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Orchestration


def run_scrapers(config: SearchConfig) -> List[Listing]:
    http_client: Optional[HttpClient]
    if config.use_sample_data:
        http_client = None
        logger.debug("Demo mode active - HTTP client disabled")
    else:
        http_client = HttpClient(config.request)
    listings: List[Listing] = []

    for platform in config.platforms:
        if not platform.enabled:
            logger.debug("Skipping disabled platform %s", platform.name)
            continue

        scraper = SCRAPERS.get(platform.name.lower())
        if scraper is None:
            logger.warning("No scraper registered for %s", platform.name)
            continue

        logger.info("Scraping %s", platform.name)
        platform_listings = scraper(config, http_client, platform)
        listings.extend(platform_listings)
    return listings


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s - %(levelname)s - %(message)s")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape multiple marketplaces")
    parser.add_argument("--config", type=Path, help="Path to JSON configuration file")
    parser.add_argument(
        "--export", type=Path, help="Override export CSV path"
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run using the built-in configuration and sample data",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> SearchConfig:
    if args.demo or args.config is None:
        config = default_config()
    else:
        config = load_config_from_file(args.config)

    if args.demo:
        config.use_sample_data = True

    if args.export:
        config.export_path = args.export
    return config


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)
    try:
        config = build_config(args)
    except Exception as exc:  # noqa: BLE001 - fail gracefully on config errors
        logger.error("Failed to load configuration: %s", exc)
        return 1

    try:
        listings = run_scrapers(config)
        filtered = filter_listings(listings, config)
        export_to_csv(filtered, config.export_path)
        AlertDispatcher(config.alerts).dispatch(filtered)
    except Exception as exc:  # noqa: BLE001 - top-level error handling
        logger.exception("Scraping run failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation entry point
    sys.exit(main())
