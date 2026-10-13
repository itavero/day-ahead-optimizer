import datetime
import logging
from zoneinfo import ZoneInfo

import pandas as pd
from requests import get

JEROENNL_API_URL = "https://jeroen.nl/api/dynamische-energieprijzen/v2/"
LOCAL_TIME_ZONE = ZoneInfo("Europe/Amsterdam")


class JeroenNlPrices:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("Jeroen.nl API key is required for this price source")
        self.api_key = api_key

    def get_prices(self, period: str, year: int | None = None) -> list[dict]:
        params = {"key": self.api_key, "period": period, "type": "json"}
        if year is not None:
            params["year"] = year
        response = get(
            JEROENNL_API_URL,
            params=params,
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
                timestamp = datetime.datetime.fromisoformat(item["datum_utc"])
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=datetime.UTC)
                else:
                    timestamp = timestamp.astimezone(datetime.UTC)
                price = float(item[price_field].replace(",", "."))
            except (AttributeError, KeyError, TypeError, ValueError) as ex:
                logging.warning(f"Skipping invalid Jeroen.nl price row: {ex}")
                continue
            rows.append([str(int(timestamp.timestamp())), "da", price])
        return pd.DataFrame(rows, columns=["time", "code", "value"])

    @staticmethod
    def _local_datetime(value: datetime.datetime) -> datetime.datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=LOCAL_TIME_ZONE)
        return value.astimezone(LOCAL_TIME_ZONE)

    @classmethod
    def _day_start(cls, value: datetime.date) -> datetime.datetime:
        return datetime.datetime.combine(value, datetime.time.min, LOCAL_TIME_ZONE)

    @staticmethod
    def _today() -> datetime.date:
        return datetime.datetime.now(LOCAL_TIME_ZONE).date()

    def _fetch_bounded_prices(
        self,
        period: str,
        start: datetime.datetime,
        end: datetime.datetime,
        *,
        year: int | None = None,
        start_exclusive: bool = False,
    ) -> pd.DataFrame:
        prices = self.parse_prices(self.get_prices(period, year=year))
        if prices.empty:
            return prices
        timestamps = pd.to_numeric(prices["time"])
        start_ts = self._local_datetime(start).timestamp()
        end_ts = self._local_datetime(end).timestamp()
        if start_exclusive:
            mask = (timestamps > start_ts) & (timestamps < end_ts)
        else:
            mask = (timestamps >= start_ts) & (timestamps < end_ts)
        return prices.loc[mask].reset_index(drop=True)

    @staticmethod
    def _empty_price_frame() -> pd.DataFrame:
        return pd.DataFrame(columns=["time", "code", "value"])

    def get_day_ahead_prices(
        self, start: datetime.datetime, end: datetime.datetime
    ) -> pd.DataFrame:
        start = self._local_datetime(start)
        end = self._local_datetime(end)
        if end <= start:
            return self._empty_price_frame()

        today = self._today()
        today_start = self._day_start(today)
        tomorrow_start = self._day_start(today + datetime.timedelta(days=1))
        day_after_start = self._day_start(today + datetime.timedelta(days=2))
        frames = []

        historical_end = min(end, today_start)
        if start < historical_end:
            year_start = start.year
            year_end = (historical_end - datetime.timedelta(microseconds=1)).year
            for year in range(year_start, year_end + 1):
                try:
                    period_start = max(start, self._day_start(datetime.date(year, 1, 1)))
                    period_end = min(
                        historical_end,
                        self._day_start(datetime.date(year + 1, 1, 1)),
                    )
                    frames.append(
                        self._fetch_bounded_prices(
                            "jaar", period_start, period_end, year=year
                        )
                    )
                except Exception as ex:
                    logging.error(
                        f"Error fetching Jeroen.nl prices for {year}: {type(ex).__name__}"
                    )

        for period, period_start, period_end in (
            ("vandaag", today_start, tomorrow_start),
            ("morgen", tomorrow_start, day_after_start),
        ):
            if start >= period_end or end <= period_start:
                continue
            try:
                frames.append(
                    self._fetch_bounded_prices(
                        period, max(start, period_start), min(end, period_end)
                    )
                )
            except Exception as ex:
                logging.error(
                    f"Error fetching Jeroen.nl {period} prices: {type(ex).__name__}"
                )
        nonempty_frames = [frame for frame in frames if not frame.empty]
        if not nonempty_frames:
            return self._empty_price_frame()
        return (
            pd.concat(nonempty_frames, ignore_index=True)
            .drop_duplicates(subset=["time"], keep="last")
            .sort_values("time")
            .reset_index(drop=True)
        )

    def get_predicted_prices(
        self, start: datetime.datetime, end: datetime.datetime
    ) -> pd.DataFrame:
        return self._fetch_bounded_prices(
            "voorspelling", start, end, start_exclusive=True
        )
