import datetime
import logging

import pandas as pd
from requests import get

JEROENNL_API_URL = "https://jeroen.nl/api/dynamische-energieprijzen/v2/"


class JeroenNlPrices:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("Jeroen.nl API key is required for this price source")
        self.api_key = api_key

    def get_prices(self, period: str) -> list[dict]:
        response = get(
            JEROENNL_API_URL,
            params={"key": self.api_key, "period": period, "type": "json"},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise ValueError(f"Jeroen.nl returned an invalid response for {period}")
        return data

    @staticmethod
    def parse_prices(
        data: list[dict], price_field: str = "prijs_excl_belastingen"
    ) -> pd.DataFrame:
        rows = []
        for item in data:
            try:
                utc_value = item["datum_utc"].replace("Z", "+00:00")
                timestamp = datetime.datetime.fromisoformat(utc_value)
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=datetime.UTC)
                price = float(item[price_field].replace(",", "."))
            except (AttributeError, KeyError, TypeError, ValueError) as ex:
                logging.warning(f"Skipping invalid Jeroen.nl price row: {ex}")
                continue
            rows.append([str(int(timestamp.timestamp())), "da", price])
        return pd.DataFrame(rows, columns=["time", "code", "value"])

    def get_day_ahead_prices(self) -> pd.DataFrame:
        rows = []
        for period in ("vandaag", "morgen"):
            try:
                rows.extend(self.get_prices(period))
            except Exception as ex:
                logging.error(
                    f"Error fetching Jeroen.nl {period} prices: {type(ex).__name__}"
                )
        return self.parse_prices(rows)

    def get_predicted_prices(
        self, know_at: datetime.datetime, new_horizon: datetime.datetime
    ) -> pd.DataFrame:
        prices = self.parse_prices(self.get_prices("voorspelling"))
        rows = []
        for row in prices.itertuples(index=False):
            timestamp = int(row.time)
            dt = datetime.datetime.fromtimestamp(timestamp, datetime.UTC)
            if dt > know_at and dt <= new_horizon:
                rows.append([timestamp, dt, "da", row.value])
        return pd.DataFrame(rows, columns=["time", "tijd", "code", "value"])
