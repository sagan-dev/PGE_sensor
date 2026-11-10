"""Utility for retrieving outstanding payments from PGE Sensor (mobile API)."""
from __future__ import annotations

import argparse
import logging
import sys
import os
import importlib.util

# Bezpośrednie załadowanie pliku api.py za pomocą importlib.
api_path = os.path.join(os.path.dirname(__file__), "custom_components", "pge_sensor", "api.py")
spec = importlib.util.spec_from_file_location("pge_sensor_api", api_path)
pge_sensor_api = importlib.util.module_from_spec(spec)
sys.modules["pge_sensor_api"] = pge_sensor_api
spec.loader.exec_module(pge_sensor_api)

PgeScraper = pge_sensor_api.PgeScraper
PgeScraperError = pge_sensor_api.PgeScraperError

_LOGGER = logging.getLogger(__name__)

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch outstanding balance from PGE mobile API")
    parser.add_argument("username", help="Login (email) used on ekob/mobile portal")
    parser.add_argument("password", help="Password used on ekob/mobile portal")
    parser.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="Request timeout in seconds (default: 15)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging",
    )
    return parser.parse_args()

def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    scraper = PgeScraper(args.username, args.password, timeout=args.timeout)
    try:
        balance = scraper.get_balance_details()
    except PgeScraperError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    amount = f"{balance.amount:.2f} PLN" if balance.amount is not None else "brak danych"
    print(f"PGE Saldo: {amount}")
    due_text = balance.due_date.strftime("%d.%m.%Y") if balance.due_date else "Brak danych"
    print(f"PGE Termin płatności: {due_text}")
    print(f"PGE Status umowy: {balance.contract_status}")
        
    print(f"PGE Kwota faktury: {balance.invoice_amount} PLN")
    print(f"PGE Energia pobrana: {balance.consumed_energy} kWh")
    print(f"PGE Energia wprowadzona: {balance.feed_in_energy} kWh")
    print(f"PGE Zużycie po rozliczeniu: {balance.settled_energy} kWh")
    print(f"PGE Okres rozliczeniowy: {balance.billing_period}")
    print(f"PGE Bieżąca płatność: {balance.current_payment} PLN")
    
    storage = balance.energy_storage
    if storage:
        zones = storage.get("zones", {})
        strefa_1 = zones.get("Strefa 1") or zones.get("Strefa 1 w sumie") or next(iter(zones.values()), {})
        summary = strefa_1.get("dataWarehouseSummary", {})
        print(f"PGE Magazyn energii: {summary.get('leftEnergyAmountSum', 0)} kWh")
        history = strefa_1.get("dataWarehousePpm", [])
        if history:
            print("\nRozbicie na miesiące:")
            # Sort from newest to oldest
            history_sorted = sorted(history, key=lambda x: x.get("insertToGridDate", ""), reverse=True)
            for item in history_sorted:
                date_str = item.get("insertToGridDate", "").split("T")[0]
                print(f" - {date_str}: rozliczono {item.get('settledEnergyAmount')} kWh | pozostaje {item.get('leftEnergyAmount')} kWh")
        print("-----------------------")
        
    return 0

if __name__ == "__main__":
    sys.exit(main())
