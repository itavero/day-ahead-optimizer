from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd

from dao.lib import da_prices as da_prices_module
from dao.lib.da_prices import DaPrices
from dao.prog.config.models.base import SecretStr
from dao.prog.config.models.pricing import PricingConfig


def _validated_pricing_config(**overrides):
    data = {
        "energy taxes consumption": {},
        "energy taxes production": {},
        "cost supplier consumption": {},
        "cost supplier production": {},
        "vat consumption": {},
        "vat production": {},
        "last invoice": "2026-01-01",
        **overrides,
    }
    return PricingConfig(**data)


class _Response:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _Database:
    def __init__(self, last_known=None):
        self.last_known = last_known
        self.saved = []

    def get_time_border_record(self, code):
        return self.last_known

    def savedata(self, df, tablename="values"):
        self.saved.append((tablename, df.copy()))


def _prices_config(extension=48):
    return SimpleNamespace(
        interval="15min",
        prices=SimpleNamespace(
            jeroennl_api_key=SecretStr("!secret jeroen_key"),
            prediction=SimpleNamespace(
                source="jeroennl", extension=extension, api=None
            ),
        ),
    )


def _item(utc_time, price="0,175000", **extra):
    return {
        "datum_utc": utc_time,
        "prijs_excl_belastingen": price,
        **extra,
    }


def test_jeroennl_config_accepts_sources_and_resolves_secret():
    config = _validated_pricing_config(
        **{
            "source day ahead": "jeroennl",
            "jeroennl-api-key": "!secret jeroen_key",
            "prediction": {"source": "jeroennl", "extension": 48},
        }
    )

    assert config.source_day_ahead == "jeroennl"
    assert config.prediction.source == "jeroennl"
    assert (
        config.jeroennl_api_key.resolve({"jeroen_key": "account-api-key"})
        == "account-api-key"
    )


def test_jeroennl_prices_fetch_today_and_tomorrow_and_parse_utc(monkeypatch):
    api_responses = {
        "vandaag": [
            _item("2026-03-29 00:45:00"),
            _item("2026-03-29 01:00:00", "0,169300"),
        ],
        "morgen": [
            _item("2026-10-25 00:45:00"),
            _item("2026-10-25 01:00:00", "0,169300"),
        ],
    }
    calls = []

    def fake_get(url, *, params, timeout):
        calls.append((url, params, timeout))
        return _Response(api_responses[params["period"]])

    monkeypatch.setattr(da_prices_module, "get", fake_get)
    db = _Database()
    prices = DaPrices(
        _prices_config(),
        db,
        secrets={"jeroen_key": "account-api-key"},
    )

    prices.get_prices("jeroennl")

    assert [call[1]["period"] for call in calls] == ["vandaag", "morgen"]
    assert all(call[1]["key"] == "account-api-key" for call in calls)
    assert all(call[1]["type"] == "json" and call[2] == 30 for call in calls)
    saved = db.saved[0][1]
    assert saved["value"].tolist() == [0.175, 0.1693, 0.175, 0.1693]
    expected_times = [
        int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp())
        for value in (
            "2026-03-29 00:45:00",
            "2026-03-29 01:00:00",
            "2026-10-25 00:45:00",
            "2026-10-25 01:00:00",
        )
    ]
    actual_times = saved["time"].astype(int).tolist()
    assert actual_times == expected_times
    assert actual_times[1] - actual_times[0] == 900
    assert actual_times[3] - actual_times[2] == 900


def test_empty_tomorrow_and_invalid_partial_rows_are_ignored(monkeypatch):
    api_responses = {
        "vandaag": [
            _item("2026-10-13 00:00:00"),
            {"datum_utc": "not-a-date", "prijs_excl_belastingen": "bad"},
        ],
        "morgen": [],
    }
    monkeypatch.setattr(
        da_prices_module,
        "get",
        lambda _url, *, params, timeout: _Response(api_responses[params["period"]]),
    )
    prices = DaPrices(
        _prices_config(),
        _Database(),
        secrets={"jeroen_key": "account-api-key"},
    )

    df = prices._get_prices_jeroennl()

    assert len(df) == 1
    assert df.iloc[0]["value"] == 0.175


def test_jeroennl_predictions_exclude_known_prices_and_respect_extension(monkeypatch):
    known_at = datetime(2026, 10, 13, 22, 0, tzinfo=UTC)
    prediction_rows = [
        _item("2026-10-13 22:00:00", "0,200000"),
        _item("2026-10-13 22:15:00", "0,190000"),
        _item("2026-10-15 22:00:00", "0,180000"),
    ]
    calls = []

    def fake_get(url, *, params, timeout):
        calls.append(params)
        return _Response(prediction_rows)

    monkeypatch.setattr(da_prices_module, "get", fake_get)
    db = _Database(known_at)
    prices = DaPrices(
        _prices_config(extension=24),
        db,
        secrets={"jeroen_key": "account-api-key"},
    )

    prices.get_predicted_prices()

    assert calls == [
        {"key": "account-api-key", "period": "voorspelling", "type": "json"}
    ]
    assert len(db.saved) == 1
    assert db.saved[0][0] == "prognoses"
    prediction_df = db.saved[0][1]
    assert prediction_df["value"].tolist() == [0.19]
    assert pd.to_datetime(
        prediction_df.iloc[0]["time"], unit="s", utc=True
    ) == pd.Timestamp("2026-10-13T22:15:00Z")


def test_prediction_extension_zero_skips_api_call(monkeypatch):
    monkeypatch.setattr(
        da_prices_module,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unexpected request")
        ),
    )
    prices = DaPrices(
        _prices_config(extension=0),
        _Database(datetime.now(UTC)),
        secrets={"jeroen_key": "account-api-key"},
    )

    prices.get_predicted_prices()
