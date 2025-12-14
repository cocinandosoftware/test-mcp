"""Purchase-related prompt command handlers."""

from __future__ import annotations

from decimal import Decimal
from typing import List, Sequence

from django.db import transaction
from django.db.models import Sum

from core.cart.models import Cart, CartItem
from core.products.models import Product

from .common import PromptServiceError


class PurchaseCommandMixin:
    """Provide handlers and helpers for purchase operations."""

    # Purchase handlers ------------------------------------------------

    def _handle_list_purchases(self, data: dict) -> dict:
        order_fields = self._extract_ordering(
            data,
            default_field="-created_at",
            allowed_fields={
                "precio": ("total_price", "id"),
                "price": ("total_price", "id"),
                "total": ("total_price", "id"),
                "total_price": ("total_price", "id"),
                "nombre": ("id", "created_at"),
                "name": ("id", "created_at"),
                "id": ("id", "created_at"),
                "fecha": ("created_at", "id"),
                "created_at": ("created_at", "id"),
            },
            default_direction="desc",
        )

        purchases_qs = self._apply_purchase_filters(Cart.objects.all(), data)

        purchases = (
            purchases_qs
            .annotate(total_items=Sum("items__quantity"))
            .prefetch_related("items__product")
            .order_by(*order_fields)
        )

        if not purchases:
            return {
                "detail": "Compras consultadas.",
                "answer": "No hay compras registradas actualmente.",
            }

        lines: List[str] = [self._format_purchase_summary(purchase) for purchase in purchases]

        return {
            "detail": "Compras consultadas.",
            "answer": "\n".join(lines),
        }

    def _handle_create_purchase(self, data: dict) -> dict:
        items_data = data.get("items")
        if not items_data:
            return self._request_additional_data(
                "Para registrar la compra necesito que indique los productos y las cantidades correspondientes."
            )

        items = self._parse_purchase_items(items_data)
        if not items:
            return self._request_additional_data(
                "Para registrar la compra debe especificar al menos un producto con su cantidad."
            )

        for product, quantity in items:
            if quantity <= 0:
                raise PromptServiceError(
                    f"La cantidad indicada para el producto {product.name} debe ser mayor que cero."
                )
            if product.stock < quantity:
                raise PromptServiceError(
                    f"El producto {product.name} no dispone de stock suficiente para {quantity} unidad(es)."
                )

        with transaction.atomic():
            cart = Cart.objects.create(total_price=Decimal("0.00"))
            total_price = Decimal("0.00")
            for product, quantity in items:
                line_total = product.price * quantity
                CartItem.objects.create(
                    cart=cart,
                    product=product,
                    quantity=quantity,
                    unit_price=product.price,
                )
                total_price += line_total
                product.stock -= quantity
                product.save(update_fields=["stock"])

            cart.total_price = total_price
            cart.save(update_fields=["total_price"])

        cart.refresh_from_db()
        total_items = sum(item.quantity for item in cart.items.all())
        summary = self._format_purchase_summary(cart, total_items_override=total_items)

        return {
            "detail": "Compra registrada.",
            "answer": "La compra se registro correctamente. " + summary,
        }

    def _handle_delete_purchase(self, data: dict) -> dict:
        purchase = self._resolve_purchase(data)

        confirm_detail = f"Confirma la eliminacion de la compra #{purchase.id}."
        confirm_prompt = f"¿Deseas eliminar la compra #{purchase.id}?"
        self._ensure_confirmation(
            action="delete_purchase",
            data=data,
            detail=confirm_detail,
            prompt=confirm_prompt,
        )

        self._delete_purchase_instance(purchase)

        return {
            "detail": "Compra eliminada.",
            "answer": f"La compra con identificador {purchase.id} se elimino y el stock se restablecio.",
        }

    def _handle_delete_purchases_by_product(self, data: dict) -> dict:
        product = self._resolve_product(data)

        purchases = list(
            Cart.objects.filter(items__product=product)
            .distinct()
            .prefetch_related("items__product")
        )

        if not purchases:
            return {
                "detail": "Sin compras asociadas.",
                "answer": f"No existen compras con el producto {product.name}.",
            }

        confirm_detail = (
            f"Confirma la eliminacion de {len(purchases)} compra(s) asociadas al producto '{product.name}'."
        )
        confirm_prompt = (
            f"¿Deseas eliminar {len(purchases)} compra(s) relacionadas con '{product.name}'?"
        )
        self._ensure_confirmation(
            action="delete_purchases_by_product",
            data=data,
            detail=confirm_detail,
            prompt=confirm_prompt,
        )

        with transaction.atomic():
            for purchase in purchases:
                self._delete_purchase_instance(purchase, use_transaction=False)

        return {
            "detail": "Compras eliminadas.",
            "answer": (
                f"Se eliminaron {len(purchases)} compra(s) asociadas al producto {product.name} "
                "y se restablecio el stock correspondiente."
            ),
        }

    def _handle_purchase_metrics(self, data: dict) -> dict:
        metrics = self._normalize_metric_list(data.get("metrics"), default=["max_price", "min_price"])
        allowed_labels = {
            "max_price": "Compra con el importe mas elevado",
            "min_price": "Compra con el importe mas reducido",
            "max_items": "Compra con mayor numero de articulos",
            "min_items": "Compra con menor numero de articulos",
        }

        for metric in metrics:
            if metric not in allowed_labels:
                raise PromptServiceError(
                    f"La metrica solicitada '{metric}' no es valida para compras."
                )

        purchases_qs = Cart.objects.annotate(total_items=Sum("items__quantity"))
        if not purchases_qs.exists():
            return {
                "detail": "Metricas de compra consultadas.",
                "answer": "No hay compras registradas para calcular metricas.",
            }

        lines: List[str] = []
        for metric in metrics:
            purchase = self._select_purchase_by_metric(metric)
            if purchase is None:
                continue
            total_items = getattr(purchase, "total_items", None)
            if total_items is None:
                total_items = sum(item.quantity for item in purchase.items.all())
            lines.append(
                (
                    f"{allowed_labels[metric]}: Compra #{purchase.id} con total {self._format_currency(purchase.total_price)} "
                    f"y {int(total_items)} articulo(s)."
                )
            )

        if not lines:
            lines.append("No fue posible calcular las metricas solicitadas.")

        return {
            "detail": "Metricas de compra consultadas.",
            "answer": "\n".join(lines),
        }

    # Purchase helpers -------------------------------------------------

    def _apply_purchase_filters(self, queryset, data: dict):
        start_raw = (
            data.get("start_date")
            or data.get("fecha_inicio")
            or data.get("from_date")
            or data.get("date_from")
        )
        end_raw = (
            data.get("end_date")
            or data.get("fecha_fin")
            or data.get("to_date")
            or data.get("date_to")
        )
        start_dt = None
        if start_raw not in (None, ""):
            start_dt = self._parse_datetime_boundary(start_raw, field="start_date")

        end_dt = None
        if end_raw not in (None, ""):
            end_dt = self._parse_datetime_boundary(end_raw, field="end_date", is_end=True)

        if start_dt and end_dt and start_dt > end_dt:
            raise PromptServiceError(
                "El rango de fechas es invalido: la fecha inicial es posterior a la final."
            )

        if start_dt:
            queryset = queryset.filter(created_at__gte=start_dt)
        if end_dt:
            queryset = queryset.filter(created_at__lte=end_dt)

        product_keys = ("product_id", "product_slug", "product_name")
        if any(key in data and str(data.get(key) or "").strip() for key in product_keys):
            product = self._resolve_product(data)
            queryset = queryset.filter(items__product=product)

        min_price_raw = (
            data.get("min_price")
            or data.get("precio_minimo")
            or data.get("min_total")
        )
        min_price = None
        if min_price_raw not in (None, ""):
            min_price = self._parse_decimal(min_price_raw, field="min_price")

        max_price_raw = (
            data.get("max_price")
            or data.get("precio_maximo")
            or data.get("max_total")
        )
        max_price = None
        if max_price_raw not in (None, ""):
            max_price = self._parse_decimal(max_price_raw, field="max_price")

        if min_price is not None and max_price is not None and min_price > max_price:
            raise PromptServiceError(
                "El rango de precios es invalido: el minimo supera al maximo."
            )

        if min_price is not None:
            queryset = queryset.filter(total_price__gte=min_price)
        if max_price is not None:
            queryset = queryset.filter(total_price__lte=max_price)

        return queryset

    def _delete_purchase_instance(self, purchase: Cart, *, use_transaction: bool = True) -> None:
        def _perform_delete():
            for item in purchase.items.all():
                product = item.product
                product.stock += item.quantity
                product.save(update_fields=["stock"])
            purchase.delete()

        if use_transaction:
            with transaction.atomic():
                _perform_delete()
        else:
            _perform_delete()

    def _parse_purchase_items(self, value) -> List[tuple[Product, int]]:
        if isinstance(value, str) or not isinstance(value, Sequence):
            raise PromptServiceError(
                "Los articulos de la compra deben proporcionarse como una lista de elementos."
            )

        parsed: List[tuple[Product, int]] = []
        for entry in value:
            if isinstance(entry, dict):
                quantity = self._parse_int(entry.get("quantity"), field="quantity")
                product_data = {}
                if entry.get("product_id") is not None:
                    product_data["product_id"] = entry.get("product_id")
                if entry.get("id") is not None and "product_id" not in product_data:
                    product_data["product_id"] = entry.get("id")
                if entry.get("product_slug") is not None:
                    product_data["product_slug"] = entry.get("product_slug")
                if entry.get("slug") is not None and "product_slug" not in product_data:
                    product_data["product_slug"] = entry.get("slug")
                if entry.get("product_name") is not None:
                    product_data["product_name"] = entry.get("product_name")
                if entry.get("name") is not None and "product_name" not in product_data:
                    product_data["product_name"] = entry.get("name")
                if not product_data:
                    raise PromptServiceError(
                        "Uno de los articulos no incluye un identificador de producto valido."
                    )
                product = self._resolve_product(product_data)
            elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                product = self._resolve_product({"product_id": entry[0]})
                quantity = self._parse_int(entry[1], field="quantity")
            else:
                raise PromptServiceError(
                    "No se pudo interpretar uno de los articulos de la compra."
                )

            parsed.append((product, quantity))

        return parsed

    def _resolve_purchase(self, data: dict) -> Cart:
        identifier = data.get("purchase_id") or data.get("id")
        if identifier is None:
            raise PromptServiceError(
                "Debe indicar el identificador numerico de la compra que desea gestionar."
            )
        try:
            purchase_id = int(identifier)
        except (TypeError, ValueError) as exc:
            raise PromptServiceError("El identificador de la compra debe ser un numero entero.") from exc

        try:
            return Cart.objects.prefetch_related("items__product").get(id=purchase_id)
        except Cart.DoesNotExist as exc:
            raise PromptServiceError(f"No existe una compra con id={purchase_id}.") from exc

    def _select_purchase_by_metric(self, metric: str) -> Cart | None:
        qs = Cart.objects.annotate(total_items=Sum("items__quantity")).prefetch_related("items__product")
        if metric == "max_price":
            return qs.order_by("-total_price", "-created_at").first()
        if metric == "min_price":
            return qs.order_by("total_price", "created_at").first()
        if metric == "max_items":
            return qs.order_by("-total_items", "-total_price").first()
        if metric == "min_items":
            return qs.order_by("total_items", "total_price").first()
        return None

    def _format_purchase_summary(self, purchase: Cart, *, total_items_override: int | None = None) -> str:
        total_items = total_items_override
        if total_items is None:
            total_items = getattr(purchase, "total_items", None)
        if total_items is None:
            total_items = sum(item.quantity for item in purchase.items.all())

        item_descriptions = [
            f"{item.product.name} x{item.quantity} ({self._format_currency(item.line_total)})"
            for item in purchase.items.all()
        ]
        items_text = ", ".join(item_descriptions) if item_descriptions else "sin productos registrados"

        created_display = self._format_datetime(purchase.created_at)
        return (
            f"Compra #{purchase.id} del {created_display} con un total de {self._format_currency(purchase.total_price)} "
            f"y {int(total_items)} articulo(s). Detalle: {items_text}."
        )


__all__ = ["PurchaseCommandMixin"]
