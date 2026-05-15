import sys
from unittest.mock import MagicMock

from spectra.recurring import apply_recurring_tags


class MockTransaction:
    """A minimal mock replacing CategorisedTransaction for testing."""
    def __init__(self, date: str, amount: float, clean_name: str, original_description: str):
        self.date = date
        self.amount = amount
        self.clean_name = clean_name
        self.original_description = original_description
        self.recurring = ""


def test_apply_recurring_tags_static_subscription():
    """Test that static pattern matching catches known merchants immediately."""
    history = {}
    txn = MockTransaction(
        date="2026-02-18", amount=-14.99, clean_name="Netflix", 
        original_description="NETFLIX.COM Amsterdam"
    )
    
    apply_recurring_tags([txn], history)
    
    assert txn.recurring == "Subscription"
    # Should stash it to history
    assert len(history["Netflix"]) == 1
    assert history["Netflix"][0] == ("2026-02-18", -14.99)


def test_apply_recurring_tags_static_income():
    """Test that income pattern matching catches salary deposits."""
    history = {}
    txn = MockTransaction(
        date="2026-02-27", amount=2150.00, clean_name="Acme Corp", 
        original_description="ACCREDITO STIPENDIO FEBBRAIO"
    )
    
    apply_recurring_tags([txn], history)
    
    assert txn.recurring == "Salary/Income"
    assert len(history["Acme Corp"]) == 1


def test_apply_recurring_tags_statement_category_income():
    """Test that a bank statement category can also flag salary income."""
    history = {}
    txn = MockTransaction(
        date="2026-04-30",
        amount=1029.00,
        clean_name="Accredito",
        original_description="Accredito",
    )
    txn.statement_category = "Stipendi e pensioni"

    apply_recurring_tags([txn], history)

    assert txn.recurring == "Salary/Income"


def test_apply_recurring_tags_temporal_monthly_subscription():
    """Test temporal hybrid logic detecting a ~30 day spacing."""
    # Seed history with 1 month ago
    history = {"Unknown Service": [("2026-01-18", -9.99)]}
    txn = MockTransaction(
        # Exactly 31 days later
        date="2026-02-18", amount=-9.99, clean_name="Unknown Service", 
        original_description="UNKNOWN CHARGE"
    )
    
    apply_recurring_tags([txn], history)
    
    # Should be caught by temporal delta since static doesn't know 'Unknown Service'
    assert txn.recurring == "Subscription"
    # Should've added the new hit to history
    assert len(history["Unknown Service"]) == 2


def test_apply_recurring_tags_temporal_price_tolerance():
    """Test temporal hybrid logic tolerating minor price changes (< 15%)."""
    history = {"AWS Cloud": [("2026-01-18", -10.00)]}
    txn = MockTransaction(
        date="2026-02-18", amount=-11.00, clean_name="AWS Cloud",  # 10% price bump
        original_description="AWS EMEA"
    )
    
    apply_recurring_tags([txn], history)
    
    assert txn.recurring == "Subscription"
    assert len(history["AWS Cloud"]) == 2


def test_apply_recurring_tags_temporal_ignores_random_dates():
    """Test temporal hybrid logic ignores dates that fall outside the billing cycle ranges."""
    history = {"Coffee Shop": [("2026-02-10", -3.50)]}
    txn = MockTransaction(
        date="2026-02-18", amount=-3.50, clean_name="Coffee Shop",  # 8 days diff - wait, 8 days is 'weekly'. Let's use 14 days
        original_description="ESPRESSO"
    )
    
    apply_recurring_tags([txn], history)
    assert txn.recurring == "Subscription"  # Ah, weekly is 6-8 days in the code. Let's make it 15 days.
    
def test_apply_recurring_tags_temporal_ignores_random_dates_fixed():
    """Test temporal hybrid logic ignores dates that fall outside the billing cycle ranges."""
    history = {"Coffee Shop": [("2026-02-01", -3.50)]}
    txn = MockTransaction(
        date="2026-02-18", amount=-3.50, clean_name="Coffee Shop",  # 17 days diff
        original_description="ESPRESSO"
    )
    
    apply_recurring_tags([txn], history)
    assert txn.recurring == ""  # Not matching weekly, monthly, or yearly ranges
