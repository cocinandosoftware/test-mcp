from __future__ import annotations

from decimal import Decimal

from django.db import models


class Cart(models.Model):
    """Shopping cart persisted after checkout."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    total_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"Carrito #{self.pk}"

    def recalculate_total(self) -> Decimal:
        total = sum((item.line_total for item in self.items.all()), Decimal("0.00"))
        self.total_price = total
        self.save(update_fields=["total_price", "updated_at"])
        return total


class CartItem(models.Model):
    """Line item persisted for each product purchased within a cart."""

    cart = models.ForeignKey(Cart, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("core_products.Product", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        ordering = ("id",)

    def __str__(self) -> str:
        return f"{self.product} x {self.quantity}"

    @property
    def line_total(self) -> Decimal:
        return self.unit_price * self.quantity
