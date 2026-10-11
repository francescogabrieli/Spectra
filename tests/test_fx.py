from __future__ import annotations

from pathlib import Path

import pytest

from spectra.db import BookmarkDB
from spectra.fx import (
    FXConversionError,
    clear_fx_rate_cache,
    convert_currency_result,
)


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


def _fake_client(monkeypatch, rates: dict[str, object], calls: list[tuple[str, str]]) -> None:
    class Client:
        def __init__(self, timeout: float) -> None:
            assert timeout > 0

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def get(self, _url: str, *, params: dict[str, str]) -> _Response:
            target = params["symbols"]
            calls.append((params["base"], target))
            return _Response({
                "base": params["base"],
                "date": "2026-02-22",
                "rates": {target: rates[target]},
            })

    import httpx

    monkeypatch.setattr(httpx, "Client", Client)


def test_rate_cache_includes_target_currency(monkeypatch) -> None:
    clear_fx_rate_cache()
    calls: list[tuple[str, str]] = []
    _fake_client(monkeypatch, {"EUR": "0.9", "GBP": "0.8"}, calls)

    eur = convert_currency_result("100", "USD", "EUR", "2026-02-22")
    gbp = convert_currency_result("100", "USD", "GBP", "2026-02-22")

    assert str(eur.amount) == "90.00"
    assert str(gbp.amount) == "80.00"
    assert calls == [("USD", "EUR"), ("USD", "GBP")]


def test_historical_rate_cache_is_persistent_and_works_offline(
    monkeypatch,
    tmp_path: Path,
) -> None:
    clear_fx_rate_cache()
    calls: list[tuple[str, str]] = []
    _fake_client(monkeypatch, {"EUR": "0.91"}, calls)
    path = tmp_path / "fx.db"

    with BookmarkDB(path) as db:
        first = convert_currency_result("10", "USD", "EUR", "2026-02-22", rate_store=db)
        assert str(first.amount) == "9.10"
        assert db.get_fx_rate("2026-02-22", "USD", "EUR")["provider"] == "frankfurter"

    clear_fx_rate_cache()

    def offline(*_args, **_kwargs):
        raise AssertionError("Cached historical rate should avoid a network request")

    import httpx

    monkeypatch.setattr(httpx, "Client", offline)
    with BookmarkDB(path) as db:
        repeat = convert_currency_result("10", "USD", "EUR", "2026-02-22", rate_store=db)

    assert str(repeat.amount) == "9.10"
    assert repeat.source == "sqlite-cache"
    assert calls == [("USD", "EUR")]


def test_invalid_persisted_rate_is_never_applied(tmp_path: Path) -> None:
    clear_fx_rate_cache()
    with BookmarkDB(tmp_path / "invalid-fx.db") as db:
        db.save_fx_rate("2026-02-22", "USD", "EUR", "-0.9", "2026-02-22", "test")
        with pytest.raises(FXConversionError, match="Stored FX rate is invalid"):
            convert_currency_result("100", "USD", "EUR", "2026-02-22", rate_store=db)


@pytest.mark.parametrize("bad_rate", ["0", "-1", "NaN", "Infinity", "not-a-rate"])
def test_invalid_provider_rates_fail_closed(monkeypatch, bad_rate: str) -> None:
    clear_fx_rate_cache()
    _fake_client(monkeypatch, {"EUR": bad_rate}, [])
    with pytest.raises(FXConversionError):
        convert_currency_result("100", "USD", "EUR", "2026-02-22")
