"""Tests for Jeroen.nl price requests and response conversion."""

from datetime import UTC, date, datetime
from types import SimpleNamespace

from dao.lib import da_prices_jeroennl
from dao.lib.da_prices import DaPrices
from dao.lib.da_prices_jeroennl import JeroenNlPrices
from dao.prog.config.models.base import SecretStr
from dao.prog.config.models.pricing import PricingConfig


class JsonResponse:
    """Minimal response object matching the requests API used by the client."""

    def __init__(self, json_data):
        self.json_data = json_data

    def raise_for_status(self):
        """Match requests.Response.raise_for_status for successful responses."""

    def json(self):
        """Return the fixed endpoint payload."""
        return self.json_data


class FakeDatabase:
    """Capture price rows saved by DaPrices."""

    def __init__(self, latest_price=None):
        self.latest_price = latest_price
        self.saved = []

    def get_time_border_record(self, code):
        return self.latest_price

    def savedata(self, frame, tablename="values"):
        self.saved.append((tablename, frame.copy()))


def make_prices_config(prediction_extension=48):
    return SimpleNamespace(
        interval="15min",
        prices=SimpleNamespace(
            jeroennl_api_key=SecretStr("!secret jeroen_key"),
            prediction=SimpleNamespace(
                source="jeroennl",
                extension=prediction_extension,
                api=None,
            ),
        ),
    )


def timestamp(utc_datetime):
    return str(
        int(datetime.fromisoformat(utc_datetime).replace(tzinfo=UTC).timestamp())
    )


def test_config_accepts_jeroennl_and_resolves_api_key():
    config = PricingConfig(
        **{
            "source day ahead": "jeroennl",
            "jeroennl-api-key": "!secret jeroen_key",
            "prediction": {"source": "jeroennl", "extension": 48},
            "energy taxes consumption": {},
            "energy taxes production": {},
            "cost supplier consumption": {},
            "cost supplier production": {},
            "vat consumption": {},
            "vat production": {},
            "last invoice": "2026-01-01",
        }
    )

    assert config.source_day_ahead == "jeroennl"
    assert config.prediction.source == "jeroennl"
    assert config.jeroennl_api_key.resolve({"jeroen_key": "account-api-key"}) == (
        "account-api-key"
    )


def test_today_endpoint_json_becomes_bounded_price_rows(monkeypatch):
    """Mock today's endpoint with its JSON shape and check normalized DB rows."""
    monkeypatch.setattr(
        JeroenNlPrices, "_today", staticmethod(lambda: date(2026, 10, 13))
    )
    endpoint_json = [
        {
            "datum_nl": "2026-10-13 00:00:00",
            "datum_utc": "2026-10-12 21:45:00",
            "prijs_excl_belastingen": "0,100000",
        },
        {
            "datum_nl": "2026-10-13 00:00:00",
            "datum_utc": "2026-10-12 22:00:00",
            "prijs_excl_belastingen": "0,175000",
        },
        {
            "datum_nl": "2026-10-13 00:15:00",
            "datum_utc": "2026-10-12 22:15:00",
            "prijs_excl_belastingen": "0,169300",
        },
    ]
    requests = []

    def mock_get(url, *, params, timeout):
        requests.append((url, params, timeout))
        return JsonResponse(endpoint_json)

    monkeypatch.setattr(da_prices_jeroennl, "get", mock_get)
    database = FakeDatabase()
    prices = DaPrices(
        make_prices_config(),
        database,
        secrets={"jeroen_key": "account-api-key"},
    )

    prices.get_prices(
        "jeroennl",
        datetime(2026, 10, 13),
        datetime(2026, 10, 14),
    )

    assert requests == [
        (
            da_prices_jeroennl.JEROENNL_API_URL,
            {"key": "account-api-key", "period": "vandaag", "type": "json"},
            30,
        )
    ]
    assert database.saved[0][0] == "values"
    assert database.saved[0][1].to_dict("records") == [
        {"time": timestamp("2026-10-12 22:00:00"), "code": "da", "value": 0.175},
        {"time": timestamp("2026-10-12 22:15:00"), "code": "da", "value": 0.1693},
    ]


def test_prediction_endpoint_json_becomes_bounded_prognosis_rows(monkeypatch):
    """Mock the prediction endpoint and exercise the full save flow."""
    known_price_time = datetime(2026, 10, 13, 22, 0, tzinfo=UTC)
    endpoint_json = [
        {
            "datum_nl": "2026-10-14 00:00:00",
            "datum_utc": "2026-10-13 22:00:00",
            "prijs_excl_belastingen": "0,121139",
            "band_laag": "0,103386",
            "band_hoog": "0,180998",
            "voorspeld_op": "2026-10-13 05:00:00",
        },
        {
            "datum_nl": "2026-10-14 00:15:00",
            "datum_utc": "2026-10-13 22:15:00",
            "prijs_excl_belastingen": "0,115984",
            "band_laag": "0,099796",
            "band_hoog": "0,175843",
            "voorspeld_op": "2026-10-13 05:00:00",
        },
        {
            "datum_nl": "2026-10-15 00:00:00",
            "datum_utc": "2026-10-14 22:00:00",
            "prijs_excl_belastingen": "0,115511",
            "band_laag": "0,098657",
            "band_hoog": "0,175370",
            "voorspeld_op": "2026-10-13 05:00:00",
        },
    ]
    requests = []

    def mock_get(url, *, params, timeout):
        requests.append(params)
        return JsonResponse(endpoint_json)

    monkeypatch.setattr(da_prices_jeroennl, "get", mock_get)
    database = FakeDatabase(latest_price=known_price_time)
    prices = DaPrices(
        make_prices_config(prediction_extension=24),
        database,
        secrets={"jeroen_key": "account-api-key"},
    )

    prices.get_predicted_prices()

    assert requests == [
        {"key": "account-api-key", "period": "voorspelling", "type": "json"}
    ]
    assert database.saved[0][0] == "prognoses"
    assert database.saved[0][1].to_dict("records") == [
        {"time": timestamp("2026-10-13 22:15:00"), "code": "da", "value": 0.115984},
    ]


def test_historical_range_requests_year_endpoint_and_filters_bounds(monkeypatch):
    monkeypatch.setattr(
        JeroenNlPrices, "_today", staticmethod(lambda: date(2026, 10, 13))
    )
    endpoint_json = [
        {
            "datum_nl": "2024-01-01 00:00:00",
            "datum_utc": "2023-12-31 23:00:00",
            "prijs_excl_belastingen": "0,100000",
        },
        {
            "datum_nl": "2024-02-01 00:00:00",
            "datum_utc": "2024-01-31 23:00:00",
            "prijs_excl_belastingen": "0,200000",
        },
        {
            "datum_nl": "2024-03-01 00:00:00",
            "datum_utc": "2024-02-29 23:00:00",
            "prijs_excl_belastingen": "0,300000",
        },
    ]
    requests = []

    def mock_get(_url, *, params, timeout):
        requests.append(params)
        return JsonResponse(endpoint_json)

    monkeypatch.setattr(da_prices_jeroennl, "get", mock_get)
    prices = JeroenNlPrices("account-api-key")

    rows = prices.get_day_ahead_prices(datetime(2024, 1, 15), datetime(2024, 3, 1))

    assert requests == [
        {
            "key": "account-api-key",
            "period": "jaar",
            "type": "json",
            "year": 2024,
        }
    ]
    assert rows.to_dict("records") == [
        {"time": timestamp("2024-01-31 23:00:00"), "code": "da", "value": 0.2}
    ]


def test_tomorrow_range_does_not_request_today(monkeypatch):
    monkeypatch.setattr(
        JeroenNlPrices, "_today", staticmethod(lambda: date(2026, 10, 13))
    )
    requests = []

    def mock_get(_url, *, params, timeout):
        requests.append(params["period"])
        return JsonResponse(
            [
                {
                    "datum_nl": "2026-10-14 00:00:00",
                    "datum_utc": "2026-10-13 22:00:00",
                    "prijs_excl_belastingen": "0,175000",
                }
            ]
        )

    monkeypatch.setattr(da_prices_jeroennl, "get", mock_get)
    prices = JeroenNlPrices("account-api-key")

    rows = prices.get_day_ahead_prices(datetime(2026, 10, 14), datetime(2026, 10, 15))

    assert requests == ["morgen"]
    assert rows["value"].tolist() == [0.175]


def test_parser_treats_naive_utc_as_utc_and_converts_offsets():
    rows = JeroenNlPrices.parse_prices(
        [
            {
                "datum_utc": "2026-03-29 00:00:00",
                "prijs_excl_belastingen": "0,175000",
            },
            {
                "datum_utc": "2026-03-29T01:00:00+01:00",
                "prijs_excl_belastingen": "0,169300",
            },
        ]
    )

    assert rows["time"].tolist() == [
        timestamp("2026-03-29 00:00:00"),
        timestamp("2026-03-29 00:00:00"),
    ]
    assert rows["value"].tolist() == [0.175, 0.1693]


def test_empty_tomorrow_and_invalid_rows_are_handled(monkeypatch):
    monkeypatch.setattr(
        JeroenNlPrices, "_today", staticmethod(lambda: date(2026, 10, 13))
    )
    responses = {
        "vandaag": [
            {
                "datum_nl": "2026-10-13 00:00:00",
                "datum_utc": "2026-10-12 22:00:00",
                "prijs_excl_belastingen": "0,175000",
            },
            {
                "datum_nl": "invalid",
                "datum_utc": "not-a-date",
                "prijs_excl_belastingen": "bad",
            },
        ],
        "morgen": [],
    }

    monkeypatch.setattr(
        da_prices_jeroennl,
        "get",
        lambda _url, *, params, timeout: JsonResponse(responses[params["period"]]),
    )
    prices = JeroenNlPrices("account-api-key")

    rows = prices.get_day_ahead_prices(datetime(2026, 10, 13), datetime(2026, 10, 15))

    assert rows.to_dict("records") == [
        {"time": timestamp("2026-10-12 22:00:00"), "code": "da", "value": 0.175}
    ]


def test_zero_prediction_extension_does_not_call_endpoint(monkeypatch):
    def unexpected_request(*_args, **_kwargs):
        raise AssertionError("prediction endpoint should not be called")

    monkeypatch.setattr(da_prices_jeroennl, "get", unexpected_request)
    prices = DaPrices(
        make_prices_config(prediction_extension=0),
        FakeDatabase(latest_price=datetime.now(UTC)),
        secrets={"jeroen_key": "account-api-key"},
    )

    prices.get_predicted_prices()


def test_empty_prediction_range_returns_empty_normalized_frame(monkeypatch):
    monkeypatch.setattr(
        da_prices_jeroennl,
        "get",
        lambda *_args, **_kwargs: JsonResponse([]),
    )
    rows = JeroenNlPrices("account-api-key").get_predicted_prices(
        datetime(2026, 10, 13, tzinfo=UTC),
        datetime(2026, 10, 14, tzinfo=UTC),
    )

    assert rows.empty
    assert rows.columns.tolist() == ["time", "code", "value"]
