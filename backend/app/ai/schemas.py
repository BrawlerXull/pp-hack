from typing import Literal

from pydantic import BaseModel, Field

from app.detection.features import FeatureKey

Classification = Literal["card_testing", "account_testing", "genuine_rush", "paypal_outage", "unclear"]
ATTACK_CLASSES = {"card_testing", "account_testing"}

RecommendedAction = Literal["SCA_ON", "BLOCK_FP", "HOLD_SKU", "REFUND_MATCHED", "NOTIFY", "NONE"]
FeatureName = Literal[tuple(k.value for k in FeatureKey)]  # type: ignore[valid-type]


class Evidence(BaseModel):
    feature: FeatureName  # type: ignore[valid-type]
    window_min: int = Field(description="Window the value comes from: 1, 5, 15 or 60")
    observed: float = Field(description="Value exactly as given in the input for this feature/window")
    baseline: float | None = Field(default=None, description="Normal value for comparison, if relevant")
    why: str = Field(description="One sentence, plain English, why this matters")


class Verdict(BaseModel):
    classification: Classification
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(description="One or two plain-English sentences for a non-technical shop owner")
    evidence: list[Evidence] = Field(description="3-6 strongest signals, citing input features only")
    recommended_actions: list[RecommendedAction]
    trend: Literal["escalating", "stable", "subsiding"] = "stable"
    uncertainty_notes: str = ""


class ReportDraft(BaseModel):
    alert: str = Field(description="Late-night phone notification, max 280 chars, uses {{placeholders}}")
    report_md: str = Field(description="One-page morning report in Markdown, uses {{placeholders}}")
    evidence_md: str = Field(
        description="Incident summary for PayPal support in Markdown, uses {{placeholders}}"
    )
