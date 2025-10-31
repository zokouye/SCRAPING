# SCRAPING

Multi-platform scraping toolkit for sourcing under-priced products across
marketplaces such as Leboncoin, Vinted, and eBay. The toolkit provides a
single entry point (`multi_market_scraper.py`) that handles configuration,
HTTP throttling, per-platform scraping, filtering, CSV exports, and alert
delivery.

## Features

* Configurable keywords, price ranges, postal code, and radius.
* Per-platform scraper functions with retry-aware HTTP requests.
* Normalisation and filtering utilities for consistent listing data.
* CSV export of filtered results.
* Alert dispatch via console, email (SMTP), or webhook.

## Requirements

* Python 3.10+
* Optional: [`requests`](https://pypi.org/project/requests/) (the script falls
  back to the Python standard library if unavailable, but installing `requests`
  enables persistent sessions and webhook POST helpers.)

Install optional dependencies with:

```bash
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install requests
```

## Usage

The script can operate using a built-in demo configuration or a custom JSON
configuration file.

### Quick demo

Run the scraper with the demo configuration and sample data. In demo mode the
script skips all outbound HTTP requests and uses the bundled fixtures, making
it safe to run offline:

```bash
python multi_market_scraper.py --demo --verbose
```

This command will export filtered listings to `listings.csv` and emit console
alerts for the detected deals.

### Custom configuration

Create a JSON file (e.g. `config.json`) describing the search parameters,
platforms, request settings, and alerts:

```json
{
  "keywords": ["iphone", "ps4"],
  "max_price": 600,
  "radius_km": 30,
  "postal_code": "75000",
  "export_path": "output/deals.csv",
  "use_sample_data": false,
  "request": {
    "delay": 2.0,
    "max_retries": 4,
    "retry_backoff": 2.0,
    "headers": {
      "User-Agent": "CustomScraper/1.0"
    }
  },
  "alerts": {
    "channels": ["console", "email"],
    "email_from": "bot@example.com",
    "email_to": "team@example.com",
    "smtp_host": "smtp.example.com",
    "smtp_port": 587
  },
  "platforms": [
    {
      "name": "leboncoin",
      "base_url": "https://api.leboncoin.fr",
      "search_path": "finder/search",
      "extra_params": {
        "owner_type": "private"
      }
    },
    {
      "name": "vinted",
      "base_url": "https://www.vinted.fr/api",
      "search_path": "catalog/items",
      "extra_params": {
        "order": "newest_first"
      }
    }
  ]
}
```

Set `"use_sample_data": true` in the configuration if you want to reuse the
fixture payloads instead of performing live network requests (useful for CI or
when developing new filters).

Run the scraper with the configuration file:

```bash
python multi_market_scraper.py --config config.json --verbose
```

### Command-line arguments

* `--config PATH` – JSON configuration file. If omitted, the default config is
  used.
* `--export PATH` – Override the CSV export destination.
* `--demo` – Use built-in configuration and sample responses, bypassing network
  calls. Helpful for local testing without network access.
* `--verbose` – Enable debug logging.

## Alert channels

* **Console**: Always available. Uses the configured logger to output the
  listings.
* **Email**: Requires `email_from`, `email_to`, `smtp_host`, and `smtp_port` in
  the configuration. The script uses SMTP with no authentication by default;
  adjust parameters to match your mail provider.
* **Webhook**: Provide `webhook_url` in the alert settings. Listings are sent as
  JSON via POST.

## Extending scrapers

Per-platform scraping logic lives in `multi_market_scraper.py`. Each scraper
function receives the global configuration, HTTP client, and its associated
`PlatformConfig`. They can be extended to call real APIs (e.g. add
authentication tokens, payload transformations, pagination) while reusing
shared filtering, exporting, and alerting utilities.

