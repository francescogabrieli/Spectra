"""ML classifier for local mode — TF-IDF + Logistic Regression, bootstrapped with seed data.

The classifier is always active: it starts with built-in seed examples that encode
domain knowledge (common merchants and their categories), and improves as user
corrections and transaction history accumulate.  User data is weighted higher than
seed data so the model progressively personalises.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import numpy as np

from spectra.ai import CategorySuggestion

logger = logging.getLogger("spectra.ml")


@dataclass(frozen=True)
class TrainingExample:
    raw_description: str
    clean_name: str
    category: str
    label_source: str
    sample_weight: float


@dataclass(frozen=True)
class PredictionResult:
    category: str
    confidence: float
    margin: float
    suggestions: list[CategorySuggestion]


_SEED_WEIGHT = 1.0
_TX_HISTORY_WEIGHT = 1.0
_MERCHANT_MEMORY_WEIGHT = 4.0
_USER_OVERRIDE_WEIGHT = 10.0

_SOURCE_WEIGHTS = {
    "seed": _SEED_WEIGHT,
    "tx_history": _TX_HISTORY_WEIGHT,
    "merchant_memory": _MERCHANT_MEMORY_WEIGHT,
    "user_override": _USER_OVERRIDE_WEIGHT,
}

_SOURCE_PRIORITIES = {
    "seed": 0,
    "tx_history": 1,
    "merchant_memory": 2,
    "user_override": 3,
}

# ── Seed knowledge ──────────────────────────────────────────────
# Each tuple is (description_example, category).  These bootstrap the model
# so it works from day-0 without any user history.

_SEED_MERCHANTS: list[tuple[list[str], str]] = [
    # ── Digital Subscriptions ──────────────────────────────────
    (["Netflix", "NETFLIX.COM", "ADDEBITO SDD NETFLIX.COM", "Netflix subscription"], "Digital Subscriptions"),
    (["Spotify", "SPOTIFY AB", "ADDEBITO SDD SPOTIFY AB", "Spotify Premium"], "Digital Subscriptions"),
    (["Apple", "APPLE.COM/BILL", "Apple Music", "Apple One", "iTunes"], "Digital Subscriptions"),
    (["Disney+", "DISNEYPLUS", "Disney Plus"], "Digital Subscriptions"),
    (["Amazon Prime", "AMAZON PRIME", "AMZN PRIME"], "Digital Subscriptions"),
    (["YouTube Premium", "YOUTUBE PREMIUM"], "Digital Subscriptions"),
    (["ChatGPT", "OPENAI", "OpenAI subscription"], "Digital Subscriptions"),
    (["GitHub", "GITHUB.COM"], "Digital Subscriptions"),
    (["Dropbox", "DROPBOX.COM"], "Digital Subscriptions"),
    (["Google One", "GOOGLE STORAGE", "GOOGLE WORKSPACE", "GOOGLE CLOUD"], "Digital Subscriptions"),
    (["iCloud", "ICLOUD STORAGE"], "Digital Subscriptions"),
    (["Adobe", "ADOBE CREATIVE CLOUD", "ADOBE.COM"], "Digital Subscriptions"),
    (["Microsoft 365", "OFFICE 365"], "Digital Subscriptions"),
    (["Notion", "NOTION.SO"], "Digital Subscriptions"),
    (["Slack", "SLACK TECHNOLOGIES"], "Digital Subscriptions"),
    (["Zoom", "ZOOM.US"], "Digital Subscriptions"),
    (["LinkedIn Premium", "LINKEDIN PREMIUM"], "Digital Subscriptions"),
    (["DAZN", "DAZN SUBSCRIPTION"], "Digital Subscriptions"),
    (["Paramount+", "PARAMOUNT PLUS"], "Digital Subscriptions"),
    (["Sky", "SKY ITALIA", "SKY TV"], "Digital Subscriptions"),
    (["NordVPN", "NORDVPN.COM", "ExpressVPN", "ProtonVPN", "Surfshark"], "Digital Subscriptions"),
    (["AWS", "AMAZON WEB SERVICES", "Heroku", "DigitalOcean", "Vercel", "Netlify", "Cloudflare"], "Digital Subscriptions"),
    (["1Password", "Bitwarden", "LastPass"], "Digital Subscriptions"),
    (["Twitch", "TWITCH.TV"], "Digital Subscriptions"),
    (["Claude AI", "ANTHROPIC"], "Digital Subscriptions"),
    (["Midjourney", "MIDJOURNEY.COM"], "Digital Subscriptions"),
    (["Setapp", "SETAPP.COM"], "Digital Subscriptions"),
    (["Apple TV+", "TV.APPLE.COM"], "Digital Subscriptions"),
    (["Porkbun", "Namecheap", "GoDaddy", "Hover.com", "Gandi.net", "Registro.it"], "Digital Subscriptions"),

    # ── Transport ─────────────────────────────────────────────
    (["Uber", "UBER TRIP", "UBER BV", "HELP.UBER.COM"], "Transport"),
    (["Bolt", "BOLT.EU", "BOLT RIDE"], "Transport"),
    (["Lyft", "LYFT RIDE"], "Transport"),
    (["Trenitalia", "TRENITALIA SPA"], "Transport"),
    (["Italo Treno", "ITALO NTV"], "Transport"),
    (["FlixBus", "FLIXBUS.IT"], "Transport"),
    (["ATM Milano", "TPER", "GTT TORINO"], "Transport"),
    (["TfL Travel Charge", "TFL TRAVEL CHARGE LONDON", "MTA", "RATP", "SNCF", "RENFE", "SBB"], "Transport"),
    (["ENI STATION", "Q8", "AGIP", "IP STATION", "Shell", "BP", "TotalEnergies"], "Transport"),
    (["Autostrade", "TELEPASS", "VIACARD"], "Transport"),
    (["Taxi", "RADIOTAXI", "TAXIFY"], "Transport"),
    (["Lime scooter", "Bird scooter", "Tier scooter", "FreeNow"], "Transport"),

    # ── Travel ────────────────────────────────────────────────
    (["Ryanair", "RYANAIR LTD", "RYANAIR FR"], "Travel"),
    (["EasyJet", "EASYJET PLC"], "Travel"),
    (["Vueling", "WizzAir", "Lufthansa", "ITA Airways", "Alitalia"], "Travel"),
    (["Turkish Airlines", "KLM", "Air France", "British Airways", "Iberia", "TAP"], "Travel"),
    (["Booking.com", "BOOKING.COM AMSTERDAM", "BOOKING COM"], "Travel"),
    (["Airbnb", "AIRBNB.COM"], "Travel"),
    (["Expedia", "Hotels.com", "Trivago", "LastMinute"], "Travel"),
    (["Hotel", "Albergo", "B&B", "Bed and Breakfast", "Hostel"], "Travel"),
    (["Resort", "Motel", "Lodge", "Camping"], "Travel"),
    (["Hertz", "Avis", "Europcar", "Sixt", "Enterprise Rent", "Maggiore"], "Travel"),
    (["Aeroporto", "Airport"], "Travel"),
    (["Tirrenia", "Grimaldi Lines", "GNV", "Ferry"], "Travel"),
    (["Frecciarossa", "Frecciargento", "Frecciabianca"], "Travel"),

    # ── Entertainment ─────────────────────────────────────────
    (["Cinema", "UCI Cinema", "The Space Cinema"], "Entertainment"),
    (["Stadio", "Stadium", "Biglietti"], "Entertainment"),
    (["Concerto", "Concert", "Teatro", "Opera", "Museo"], "Entertainment"),
    (["TicketOne", "Ticketmaster", "Vivaticket", "Eventbrite"], "Entertainment"),
    (["Steam", "PlayStation", "Xbox", "Nintendo", "Epic Games", "PSN"], "Entertainment"),
    (["Gardaland", "Mirabilandia", "Disneyland", "Parco divertimenti"], "Entertainment"),

    # ── Groceries ─────────────────────────────────────────────
    (["Esselunga", "ESSELUNGA SPA", "POS ESSELUNGA"], "Groceries"),
    (["Coop", "COOP ITALIA"], "Groceries"),
    (["Conad", "CONAD SUPERMERCATO"], "Groceries"),
    (["Lidl", "LIDL ITALIA", "POS LIDL"], "Groceries"),
    (["Aldi", "ALDI SUD"], "Groceries"),
    (["Eurospin", "EUROSPIN SPA"], "Groceries"),
    (["PAM", "PAM SUPERMERCATO"], "Groceries"),
    (["Penny Market", "PENNY MARKET"], "Groceries"),
    (["Tesco", "Sainsbury", "ASDA", "Waitrose", "Morrisons", "Marks Spencer"], "Groceries"),
    (["Rewe", "Edeka", "Kaufland", "Netto", "Migros", "Denner"], "Groceries"),
    (["Auchan", "Leclerc", "Intermarche", "Monoprix", "Carrefour"], "Groceries"),
    (["Mercadona", "Pingo Doce", "Continente"], "Groceries"),
    (["Walmart", "Target", "Kroger", "Whole Foods", "Trader Joe", "7-Eleven"], "Groceries"),
    (["Supermercato", "Supermarket", "Grocery", "Alimentari"], "Groceries"),

    # ── Food & Dining ─────────────────────────────────────────
    (["Uber Eats", "UBER EATS DELIVERY", "UBEREATS"], "Food & Dining"),
    (["Deliveroo", "DELIVEROO.COM"], "Food & Dining"),
    (["Glovo", "GLOVO APP"], "Food & Dining"),
    (["Just Eat", "JUST EAT", "JUSTEAT"], "Food & Dining"),
    (["DoorDash", "Grubhub", "Postmates"], "Food & Dining"),
    (["Starbucks", "STARBUCKS COFFEE", "POS STARBUCKS"], "Food & Dining"),
    (["McDonalds", "MC DONALDS", "MCDONALDS"], "Food & Dining"),
    (["Burger King", "BURGER KING"], "Food & Dining"),
    (["KFC", "KENTUCKY FRIED CHICKEN"], "Food & Dining"),
    (["Subway", "SUBWAY SANDWICH"], "Food & Dining"),
    (["Ristorante", "Trattoria", "Pizzeria", "Osteria", "Enoteca", "Restaurant"], "Food & Dining"),
    (["Bar Caffè", "Caffè Roma", "Cafeteria", "Pasticceria", "Costa Coffee", "Dunkin"], "Food & Dining"),
    (["Sushi", "Sushiko", "Ramen", "Udon"], "Food & Dining"),
    (["Poke bowl", "Pokè house"], "Food & Dining"),
    (["Bakery", "Panetteria", "Forno", "Boulangerie", "Pret a Manger"], "Food & Dining"),
    (["Gelateria", "Gelato shop"], "Food & Dining"),
    (["Domino", "Papa Johns", "Pizza Hut"], "Food & Dining"),
    (["Autogrill", "AUTOGRILL SPA"], "Food & Dining"),
    (["Wolt", "WOLT.COM"], "Food & Dining"),

    # ── Shopping ──────────────────────────────────────────────
    (["Amazon", "AMAZON EU SARL", "AMZN MKTP", "AMAZON.IT", "AMAZON MARKETPLACE"], "Shopping"),
    (["AliExpress", "Temu", "Shein", "ASOS", "Zalando"], "Shopping"),
    (["IKEA", "IKEA ITALIA RETAIL", "POS IKEA"], "Shopping"),
    (["Zara", "H&M", "Uniqlo", "Decathlon", "Primark", "Muji"], "Shopping"),
    (["MediaWorld", "Unieuro", "Euronics", "Best Buy"], "Shopping"),
    (["Sephora", "Kiko", "MAC Cosmetics"], "Shopping"),
    (["Etsy", "ETSY.COM"], "Shopping"),
    (["Vinted", "VINTED.COM"], "Shopping"),
    (["eBay", "EBAY.COM"], "Shopping"),
    (["Leroy Merlin", "OBI", "Brico", "Home Depot"], "Shopping"),
    (["PayPal purchase", "PAYPAL PAYMENT"], "Shopping"),

    # ── Health ────────────────────────────────────────────────
    (["Farmacia", "Pharmacy", "Pharmacie", "Apotheke", "CVS", "Walgreens"], "Health"),
    (["Rossmann", "DM Drogerie"], "Health"),
    (["Dottore", "Medico", "Clinica", "Ospedale", "Hospital"], "Health"),
    (["Dentista", "Odontoiatra", "Dental clinic"], "Health"),
    (["Psicologo", "Psicologa", "Terapista", "Fisioterapista"], "Health"),
    (["Ottica", "Visita oculistica", "Optician"], "Health"),

    # ── Health & Fitness ──────────────────────────────────────
    (["Palestra", "Gym", "Fitness club", "Wellness center", "CrossFit", "Pilates", "Yoga"], "Health & Fitness"),

    # ── Insurance ─────────────────────────────────────────────
    (["Assicurazione", "Insurance", "AXA", "Allianz", "Generali", "Zurich", "UnipolSai"], "Insurance"),
    (["RC Auto", "Polizza auto", "Premio assicurativo", "Direct Line", "BUPA"], "Insurance"),

    # ── Utilities ─────────────────────────────────────────────
    (["Vodafone", "VODAFONE ITALIA"], "Utilities"),
    (["TIM", "TIM TELECOM"], "Utilities"),
    (["Wind Tre", "WINDTRE SPA"], "Utilities"),
    (["Iliad", "ILIAD ITALIA"], "Utilities"),
    (["Fastweb", "FASTWEB SPA"], "Utilities"),
    (["Enel", "ENEL ENERGIA"], "Utilities"),
    (["A2A", "IREN", "HERA", "ACEA"], "Utilities"),
    (["Bolletta", "Utenza", "Gas luce", "Electricity", "Water bill"], "Utilities"),
    (["Affitto", "Canone locazione", "Rent", "Noleggio appartamento"], "Utilities"),
    (["Condominio", "Spese condominiali"], "Utilities"),

    # ── Cash ──────────────────────────────────────────────────
    (["Versamento contanti", "Deposito contanti", "Cash deposit"], "Cash Deposit"),
    (["Prelievo", "Prelievo Bancomat", "ATM Cash", "Cash withdrawal", "Prelievo con carta"], "Cash Withdrawal"),

    # ── Taxes ─────────────────────────────────────────────────
    (["F24", "Agenzia Entrate", "Tasse", "Tributi", "IMU", "TARI", "Tax"], "Taxes"),
    (["Comune di", "Regione", "Provincia di", "ASL", "Council"], "Taxes"),
    (["Bollo auto", "PRA", "DVLA"], "Taxes"),

    # ── Education ─────────────────────────────────────────────
    (["Università", "Politecnico", "Accademia", "Corso di", "College", "School"], "Education"),
    (["Udemy", "Coursera", "Skillshare", "Duolingo", "Busuu"], "Education"),
    (["Libreria Feltrinelli", "Mondadori", "Libri", "Waterstones", "Barnes Noble"], "Education"),

    # ── Income & Transfers ────────────────────────────────────
    (["Stipendio", "STIPENDIO MESE", "Salary", "Payroll", "Retribuzione",
      "ACCREDITO STIPENDIO", "ACCREDITO RETRIBUZIONE", "ACCREDITO SALARIO",
      "Accredito competenze", "Accredito emolumenti", "Bonifico stipendio",
      "Emolumenti", "Competenze mensili", "Retribuzione mensile",
      "Gehalt", "Lohn", "Gehaltseingang",           # German
      "Salaire", "Virement salaire",                # French
      "Nómina", "Salario",                          # Spanish
      "Salário", "Ordenado"], "Salary"),             # Portuguese
    (["Pensione", "Pension", "ACCREDITO PENSIONE", "INPS pensione",
      "Rente", "Retraite", "Jubilación", "Pensão"], "Pension"),
    (["Bonifico ricevuto", "Accredito bonifico", "Bonifico in entrata",
      "ACCREDITO BONIFICO", "Accredito da",
      "Incoming transfer", "Überweisung eingegangen",
      "Virement reçu", "Transferencia recibida"], "Transfer In"),
    (["Rimborso", "Refund", "Cashback",
      "Remboursement", "Reembolso", "Erstattung"], "Reimbursement"),
    (["Revolut top-up", "REVOLUT TOP UP"], "Transfer"),
]

# Banking prefixes used to augment seed data with realistic raw descriptions.
_BANKING_PREFIXES = [
    # Italian
    "",
    "POS ",
    "POS 1234 ",
    "ADDEBITO SDD ",
    "ADDEBITO DIRETTO ",
    "PAGAMENTO ",
    "PAGAMENTO SU POS ",
    "PAGAMENTO SU POS ESTERO ",
    # English / UK
    "CARD PAYMENT ",
    "CARD PAYMENT TO ",
    "DIRECT DEBIT ",
    "CONTACTLESS ",
    # German
    "Kartenzahlung ",
    "Lastschrift ",
    # French
    "Paiement CB ",
    "Prélèvement SEPA ",
    # Spanish
    "Pago con tarjeta ",
]


def build_seed_data() -> list[tuple[str, str]]:
    """Expand _SEED_MERCHANTS into a flat list of (description, category) pairs."""
    data: list[tuple[str, str]] = []
    for examples, category in _SEED_MERCHANTS:
        for example in examples:
            data.append((example, category))
            # Add prefixed variants for the first/main example of each group
            if example == examples[0]:
                for prefix in _BANKING_PREFIXES:
                    if prefix:
                        data.append((f"{prefix}{example}", category))
                        data.append((f"{prefix}{example.upper()}", category))
    return data


def _extract_clean_name(text: str) -> str:
    from spectra.local_categorizer import _extract_merchant_name

    return _extract_merchant_name(text) if text else ""


def _normalize_feature_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).strip()


def _source_priority(label_source: str) -> int:
    return _SOURCE_PRIORITIES.get(str(label_source or ""), -1)


def _weight_for_source(label_source: str) -> float:
    return _SOURCE_WEIGHTS.get(str(label_source or ""), _TX_HISTORY_WEIGHT)


def _make_training_example(
    *,
    raw_description: str,
    clean_name: str,
    category: str,
    label_source: str,
) -> TrainingExample | None:
    normalized_source = str(label_source or "tx_history").strip().lower()
    normalized_category = str(category or "").strip()
    raw = str(raw_description or "").strip()
    merchant = str(clean_name or "").strip()
    if raw:
        merchant = _extract_clean_name(raw)
    elif merchant:
        merchant = _extract_clean_name(merchant)

    if not normalized_category or not (raw or merchant):
        return None

    return TrainingExample(
        raw_description=raw,
        clean_name=merchant,
        category=normalized_category,
        label_source=normalized_source,
        sample_weight=_weight_for_source(normalized_source),
    )


def _training_key(example: TrainingExample) -> str:
    base = example.raw_description or example.clean_name
    return _normalize_feature_text(base)


def build_training_examples(
    training_data: list[dict[str, str]] | list[tuple[str, str]] | None = None,
) -> list[TrainingExample]:
    """Resolve seed and user data into de-duplicated weighted training examples."""
    resolved: dict[str, TrainingExample] = {}

    def consider(example: TrainingExample | None) -> None:
        if example is None:
            return
        key = _training_key(example)
        if not key:
            return
        existing = resolved.get(key)
        if existing is None or _source_priority(example.label_source) > _source_priority(existing.label_source):
            resolved[key] = example

    for raw_description, category in build_seed_data():
        consider(
            _make_training_example(
                raw_description=raw_description,
                clean_name="",
                category=category,
                label_source="seed",
            )
        )

    for item in training_data or []:
        if isinstance(item, tuple):
            raw_description, category = item
            consider(
                _make_training_example(
                    raw_description=str(raw_description or ""),
                    clean_name="",
                    category=str(category or ""),
                    label_source="user_override",
                )
            )
            continue

        consider(
            _make_training_example(
                raw_description=str(item.get("raw_description", "")),
                clean_name=str(item.get("clean_name", "")),
                category=str(item.get("category", "")),
                label_source=str(item.get("label_source", "tx_history")),
            )
        )

    return list(resolved.values())


def _feature_row(raw_description: str, clean_name: str) -> dict[str, str]:
    normalized_clean_name = _normalize_feature_text(clean_name)
    combined_text = " ".join(part for part in [raw_description.strip(), clean_name.strip()] if part).strip()
    if not combined_text:
        combined_text = clean_name.strip() or raw_description.strip()
    return {
        "combined_text": combined_text,
        "clean_name_normalized": normalized_clean_name or _normalize_feature_text(combined_text),
    }


def _select_combined_text(rows: list[dict[str, str]]) -> list[str]:
    return [row["combined_text"] for row in rows]


def _select_clean_name_text(rows: list[dict[str, str]]) -> list[str]:
    return [row["clean_name_normalized"] for row in rows]


def train_classifier(
    training_data: list[dict[str, str]] | list[tuple[str, str]] | None = None,
) -> Any | None:
    """Train a weighted TF-IDF + LogisticRegression local classifier."""
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import FeatureUnion, Pipeline
        from sklearn.preprocessing import FunctionTransformer
    except ImportError:
        logger.info("scikit-learn not installed — ML classifier disabled. Install with: pip install scikit-learn")
        return None

    training_examples = build_training_examples(training_data)
    feature_rows = [
        _feature_row(example.raw_description, example.clean_name)
        for example in training_examples
    ]
    categories: list[str] = []
    weights: list[float] = []
    source_counts: dict[str, int] = {}
    for example in training_examples:
        categories.append(example.category)
        weights.append(example.sample_weight)
        source_counts[example.label_source] = source_counts.get(example.label_source, 0) + 1

    unique_cats = set(categories)
    if len(unique_cats) < 2:
        logger.info("Only 1 category in combined data — ML classifier not useful")
        return None

    pipeline = Pipeline([
        ("features", FeatureUnion([
            ("word_tfidf", Pipeline([
                ("selector", FunctionTransformer(_select_combined_text, validate=False)),
                ("tfidf", TfidfVectorizer(
                    max_features=6000,
                    ngram_range=(1, 2),
                    sublinear_tf=True,
                    strip_accents="unicode",
                    lowercase=True,
                )),
            ])),
            ("char_tfidf", Pipeline([
                ("selector", FunctionTransformer(_select_clean_name_text, validate=False)),
                ("tfidf", TfidfVectorizer(
                    analyzer="char_wb",
                    ngram_range=(3, 5),
                    sublinear_tf=True,
                    lowercase=True,
                )),
            ])),
        ])),
        ("clf", LogisticRegression(
            max_iter=1000,
            C=1.0,
            class_weight="balanced",
        )),
    ])

    pipeline.fit(feature_rows, categories, clf__sample_weight=np.array(weights))

    logger.info(
        "ML classifier trained: %d examples (%s), %d categories",
        len(training_examples),
        ", ".join(f"{source}={count}" for source, count in sorted(source_counts.items())),
        len(unique_cats),
    )
    return pipeline


def predict_details(
    classifier: Any,
    description: str,
    *,
    clean_name: str | None = None,
    limit: int = 3,
) -> PredictionResult:
    """Predict category plus ranked suggestions for a raw bank description."""
    resolved_clean_name = clean_name or _extract_clean_name(description)
    feature_row = _feature_row(description, resolved_clean_name)
    proba = classifier.predict_proba([feature_row])[0]
    ordered = np.argsort(proba)[::-1]
    top_indices = ordered[: max(1, limit)]
    suggestions = [
        CategorySuggestion(
            category=str(classifier.classes_[idx]),
            score=round(float(proba[idx]), 4),
        )
        for idx in top_indices
    ]
    top_confidence = suggestions[0].score
    second_confidence = suggestions[1].score if len(suggestions) > 1 else 0.0
    return PredictionResult(
        category=suggestions[0].category,
        confidence=top_confidence,
        margin=round(top_confidence - second_confidence, 4),
        suggestions=suggestions,
    )


def predict(classifier: Any, description: str) -> tuple[str, float]:
    """Predict category for a raw banking description.

    Returns
    -------
    (category, confidence) — confidence is the max class probability.
    """
    result = predict_details(classifier, description)
    return result.category, result.confidence
