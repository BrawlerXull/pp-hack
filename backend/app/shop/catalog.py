from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    price: Decimal
    description: str


# The demo shop "Wick & Wax". The $3 tea-light pack is what card testers go for (cheapest item).
PRODUCTS: dict[str, Product] = {
    p.sku: p
    for p in [
        Product(
            "SKU-TEALIGHT-3", "Tea-light pack (12)", Decimal("3.00"), "Unscented tea lights, 4-hour burn."
        ),
        Product("SKU-PILLAR-18", "Amber pillar candle", Decimal("18.00"), "Hand-poured soy, amber & cedar."),
        Product("SKU-GIFTSET-42", "Evening gift set", Decimal("42.00"), "Three jar candles in a gift box."),
    ]
}

CHEAPEST_SKU = min(PRODUCTS.values(), key=lambda p: p.price).sku
