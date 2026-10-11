from __future__ import annotations

from spectra.ai import CategorisedTransaction
from spectra.reporter import generate_html_report


def test_preview_report_uses_currency_precision_and_escapes_bank_text(
    tmp_path,
    isolate_spectra_runtime,
) -> None:
    transaction = CategorisedTransaction(
        id="report-row",
        original_description="<script>bank text</script>",
        clean_name="<b>Shop</b>",
        category="Food",
        amount=-0.001,
        amount_decimal="-0.001",
        currency="KWD",
        date="2026-02-22",
    )

    report = generate_html_report([transaction], tmp_path / "report")
    html = report.read_text(encoding="utf-8")

    assert report.parent == tmp_path / "report"
    assert "-0.001 KWD" in html
    assert "&lt;script&gt;bank text&lt;/script&gt;" in html
    assert "&lt;b&gt;Shop&lt;/b&gt;" in html
    assert "<script>bank text</script>" not in html
    isolate_spectra_runtime.assert_called_once_with(report.as_uri())
