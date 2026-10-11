"""HTML Reporter for dry-runs to visualize categorized transactions in the browser."""

from __future__ import annotations

import logging
import webbrowser
from html import escape
from pathlib import Path
from typing import Any

from spectra.money import decimal_text

logger = logging.getLogger("spectra.reporter")

def generate_html_report(transactions: list[Any], output_dir: str | Path = "data") -> Path:
    """Generate an HTML report for dry-run transactions and open it in the browser."""
    output_path = Path(output_dir) / "dry_run_report.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    rows = []
    for t in transactions:
        amount_class = "positive" if t.amount > 0 else "negative"
        sign = "+" if t.amount > 0 else ""
        currency = str(getattr(t, "currency", "") or "EUR").upper()
        amount_text = decimal_text(
            getattr(t, "amount_decimal", None) or t.amount, currency,
        )
        
        # Tags formatting
        tag_html = ""
        if t.recurring == "Subscription":
            tag_html = '<span class="tag subscription">Subscription</span>'
        elif t.recurring == "Salary/Income":
            tag_html = '<span class="tag salary">Salary</span>'
            
        # FX String
        fx_html = ""
        if t.original_amount is not None:
            original_currency = str(t.original_currency or currency).upper()
            original_amount = decimal_text(
                getattr(t, "original_amount_decimal", None) or t.original_amount,
                original_currency,
            )
            fx_html = (
                f'<div class="fx-note">Converted from {escape(original_amount)} '
                f'{escape(original_currency)}</div>'
            )

        row = f"""
        <tr>
            <td class="mono">{escape(str(t.date))}</td>
            <td>
                <div style="font-weight: 500;">{escape(str(t.clean_name))} {tag_html}</div>
            </td>
            <td class="original-desc">{escape(str(t.original_description))}</td>
            <td><span class="tag">{escape(str(t.category))}</span></td>
            <td class="mono amount {amount_class}" style="text-align: right;">
                {sign}{escape(amount_text)} {escape(currency)}
                {fx_html}
            </td>
        </tr>
        """
        rows.append(row)

    rows_html = "\n".join(rows)

    html_template = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Spectra — Dry Run Report</title>
        <style>
            :root {{
                --bg: #111827;
                --surface: #1f2937;
                --text: #f3f4f6;
                --text-muted: #9ca3af;
                --border: #374151;
                --accent: #3b82f6;
                --green: #10b981;
                --red: #ef4444;
                --yellow: #f59e0b;
                --purple: #8b5cf6;
            }}
            body {{
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                background-color: var(--bg);
                color: var(--text);
                margin: 0;
                padding: 2rem;
                line-height: 1.5;
            }}
            .container {{
                max-width: 1200px;
                margin: 0 auto;
            }}
            header {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                margin-bottom: 2rem;
                padding-bottom: 1rem;
                border-bottom: 1px solid var(--border);
            }}
            h1 {{
                margin: 0;
                font-size: 1.5rem;
                font-weight: 600;
                display: flex;
                align-items: center;
                gap: 0.5rem;
            }}
            .badge {{
                background: var(--surface);
                padding: 0.25rem 0.75rem;
                border-radius: 9999px;
                font-size: 0.875rem;
                color: var(--text-muted);
                border: 1px solid var(--border);
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                background: var(--surface);
                border-radius: 8px;
                overflow: hidden;
            }}
            th, td {{
                padding: 1rem;
                text-align: left;
                border-bottom: 1px solid var(--border);
            }}
            th {{
                background: rgba(0,0,0,0.2);
                font-weight: 500;
                color: var(--text-muted);
                text-transform: uppercase;
                font-size: 0.75rem;
                letter-spacing: 0.05em;
            }}
            tr:last-child td {{
                border-bottom: none;
            }}
            .amount.positive {{ color: var(--green); }}
            .amount.negative {{ color: var(--text); }}
            .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 0.875rem; }}
            .original-desc {{ color: var(--text-muted); font-size: 0.875rem; }}
            
            .tag {{
                display: inline-block;
                padding: 0.125rem 0.5rem;
                border-radius: 4px;
                font-size: 0.75rem;
                font-weight: 500;
                background: var(--border);
            }}
            .tag.subscription {{ background: rgba(139, 92, 246, 0.2); color: #c4b5fd; border: 1px solid rgba(139, 92, 246, 0.5); }}
            .tag.salary {{ background: rgba(16, 185, 129, 0.2); color: #6ee7b7; border: 1px solid rgba(16, 185, 129, 0.5); }}
            .fx-note {{ font-size: 0.75rem; color: var(--text-muted); margin-top: 0.25rem; }}
        </style>
    </head>
    <body>
        <div class="container">
            <header>
                <h1>Spectra Dry-Run Report</h1>
                <div class="badge">{len(transactions)} Transactions</div>
            </header>
            
            <table>
                <thead>
                    <tr>
                        <th>Date</th>
                        <th>Clean Name</th>
                        <th>Original Description</th>
                        <th>Category</th>
                        <th style="text-align: right;">Amount</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>
        </div>
    </body>
    </html>
    """
    
    output_path.write_text(html_template, encoding="utf-8")
    logger.info("📄 HTML Report generated at %s", output_path)
    
    # Open in default browser
    webbrowser.open(f"file://{output_path.absolute()}")
    
    return output_path
