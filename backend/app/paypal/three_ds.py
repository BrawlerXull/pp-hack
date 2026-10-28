"""Whether a browser card order may be captured, from PayPal's 3-D Secure authentication result.

Card Fields run 3-D Secure in the browser, and the browser's word is not proof: we read the order
(GET /v2/checkout/orders/{id}) and decide from payment_source.card.authentication_result before capturing.
Pure, so the table below is unit-tested on its own.

- liability_shift YES or POSSIBLE: the bank verified the cardholder (or accepts liability). Capture.
- The guard required 3-D Secure (SCA_ALWAYS, an attack is on): anything else is refused. A bot cannot pass
  the bank's check, and during an attack an unverified card is exactly what we are stopping. A real shopper
  whose card is not enrolled can still pay with the PayPal button.
- 3-D Secure only when the bank asks (SCA_WHEN_REQUIRED, normal mode), following PayPal's recommended
  actions: no authentication result means 3-D Secure did not run, so the bank decides at capture;
  liability_shift NO with enrollment N (not enrolled), U (issuer unavailable) or B (bypassed) may continue,
  and so may a bare NO with no three_d_secure block at all (the bank was not asked); NO with enrollment Y
  (the buyer was challenged and did not pass), or UNKNOWN, is refused.
- An order still waiting for the payer (PAYER_ACTION_REQUIRED) never finished the bank's check: refused.
"""

from dataclasses import dataclass, fields

PASSED_LIABILITY = frozenset({"YES", "POSSIBLE"})
# Enrollment states for which PayPal recommends continuing without a liability shift.
CONTINUE_ENROLLMENT = frozenset({"N", "U", "B"})


@dataclass(frozen=True)
class CardInfo:
    """What PayPal tells us about the card on an order: never the number."""

    brand: str | None = None
    last4: str | None = None
    bin6: str | None = None
    expiry: str | None = None  # YYYY-MM
    liability_shift: str | None = None
    enrollment: str | None = None  # three_d_secure.enrollment_status
    auth_status: str | None = None  # three_d_secure.authentication_status

    @property
    def three_ds_ran(self) -> bool:
        return bool(self.liability_shift or self.enrollment or self.auth_status)


def parse_card(payment_source: object) -> CardInfo | None:
    card = payment_source.get("card") if isinstance(payment_source, dict) else None
    if not isinstance(card, dict):
        return None
    auth = card.get("authentication_result") or {}
    tds = auth.get("three_d_secure") or {}
    bin_ = str((card.get("bin_details") or {}).get("bin") or "")
    last4 = str(card.get("last_digits") or "")
    expiry = str(card.get("expiry") or "")
    info = CardInfo(
        brand=(str(card["brand"]).upper()[:20] if card.get("brand") else None),
        last4=last4[-4:] if len(last4) >= 4 and last4[-4:].isdigit() else None,
        bin6=bin_[:6] if len(bin_) >= 6 and bin_[:6].isdigit() else None,
        expiry=expiry[:7] if len(expiry) >= 7 else None,
        liability_shift=(str(auth["liability_shift"]).upper()[:10] if auth.get("liability_shift") else None),
        enrollment=(str(tds["enrollment_status"]).upper()[:2] if tds.get("enrollment_status") else None),
        auth_status=(
            str(tds["authentication_status"]).upper()[:2] if tds.get("authentication_status") else None
        ),
    )
    return info if info != CardInfo() else None


def merge(before: CardInfo | None, after: CardInfo | None) -> CardInfo | None:
    """The order read before the capture plus the capture's own answer (which wins where it says
    something): a capture response without authentication_result must not erase the bank's result."""
    if before is None or after is None:
        return after or before
    return CardInfo(**{f.name: getattr(after, f.name) or getattr(before, f.name) for f in fields(CardInfo)})


def capture_allowed(card: CardInfo | None, order_status: str | None, sca_required: bool) -> bool:
    """True when the order may be captured (see the module docstring for the table)."""
    ls = card.liability_shift if card else None
    if ls in PASSED_LIABILITY:
        return True
    if sca_required or order_status == "PAYER_ACTION_REQUIRED":
        return False
    if card is None or not card.three_ds_ran:
        return True
    if ls == "NO" and card.enrollment is None and card.auth_status is None:
        return True  # liability NO with no three_d_secure details: no check ran, the bank decides
    return ls == "NO" and card.enrollment in CONTINUE_ENROLLMENT
