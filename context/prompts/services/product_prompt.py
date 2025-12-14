"""LLM-backed helper for answering catalog questions."""

from __future__ import annotations

from decimal import Decimal
from typing import List

import requests
from django.conf import settings

from core.cart.models import Cart
from core.products.models import Category, Product

from .common import GROQ_CHAT_URL, LOG, PromptServiceError, extract_error_detail


class ProductPromptService:
    """Send product related questions to the Groq LLM."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = (api_key or getattr(settings, "GROQ_API_KEY", "")).strip()
        self.model = (model or getattr(settings, "GROQ_MODEL", "")).strip()

    def answer_question(self, question: str) -> str:
        question = (question or "").strip()
        if not question:
            raise PromptServiceError("El mensaje no puede estar vacio.")

        if not self.api_key:
            raise PromptServiceError(
                "Servicio LLM no configurado. Define la variable de entorno GROQ_API_KEY."
            )

        lower_question = question.lower()
        products_block = self._build_products_context()
        categories_block = self._build_categories_context()
        purchases_block, purchases_count = self._build_purchases_context()

        if purchases_count == 0 and any(
            token in lower_question for token in ["compra", "compras", "pedido", "pedidos", "venta", "ventas"]
        ):
            return "No hay compras registradas actualmente."
        payload = {
            "model": self.model or "llama3-70b-8192",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Eres un asistente experto en el catalogo de productos de una tienda. "
                        "Para cada consulta debes: (1) identificar exactamente la informacion solicitada, "
                        "(2) revisar el catalogo proporcionado, (3) realizar los calculos necesarios (maximos, "
                        "minimos, promedios, conteos, filtros, ordenaciones) y (4) responder de forma directa y "
                        "concreta en espanol neutro. No enumeres todo el catalogo salvo que el usuario lo pida "
                        "explicitamente; limita la respuesta a los datos relevantes. Si la pregunta no esta "
                        "relacionada con los productos disponibles, indica que no puedes responder."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Catalogo de productos:\n"
                        + products_block
                        + "\n\nCategorias disponibles:\n"
                        + categories_block
                        + "\n\nCompras registradas:\n"
                        + purchases_block
                        + "\n\nPregunta: "
                        + question
                    ),
                },
            ],
            "temperature": 0.1,
            "max_tokens": 256,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                GROQ_CHAT_URL,
                json=payload,
                headers=headers,
                timeout=30,
            )
        except requests.RequestException as exc:
            LOG.exception("Groq request failed")
            raise PromptServiceError("No se pudo contactar con el servicio LLM.") from exc

        if response.status_code >= 400:
            detail = extract_error_detail(response)
            LOG.error("Groq error %s: %s", response.status_code, detail)
            raise PromptServiceError(
                f"Servicio LLM respondio con error ({response.status_code}): {detail}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise PromptServiceError("Respuesta invalida del servicio LLM.") from exc

        choices = data.get("choices") or []
        if not choices:
            raise PromptServiceError("Respuesta vacia del servicio LLM.")

        content = choices[0].get("message", {}).get("content", "").strip()
        if not content:
            raise PromptServiceError("El servicio LLM no devolvio informacion util.")

        return content

    def _build_products_context(self) -> str:
        products = (
            Product.objects.all()
            .order_by("price", "name")
            .prefetch_related("categories")
        )
        lines: List[str] = []
        for product in products:
            categories = ", ".join(
                category.name for category in product.categories.all().order_by("name")
            ) or "sin categorias"
            lines.append(
                (
                    "- Nombre: {name}; Precio: {price}; Stock: {stock}; Activo: {active}; "
                    "Categorias: {categories}"
                ).format(
                    name=product.name,
                    price=product.price,
                    stock=product.stock,
                    active="si" if product.is_active else "no",
                    categories=categories,
                )
            )
        if not lines:
            return "No hay productos disponibles."
        return "\n".join(lines)

    def _build_categories_context(self) -> str:
        categories = (
            Category.objects.all()
            .order_by("name")
            .prefetch_related("products")
        )
        if not categories:
            return "No hay categorias registradas."

        lines: List[str] = []
        for category in categories:
            product_names = ", ".join(
                product.name for product in category.products.all().order_by("name")
            ) or "sin productos"
            status = "activa" if category.is_active else "inactiva"
            lines.append(
                (
                    "- Nombre: {name}; Slug: {slug}; Estado: {status}; Productos: {products}".format(
                        name=category.name,
                        slug=category.slug,
                        status=status,
                        products=product_names,
                    )
                )
            )
        return "\n".join(lines)

    def _build_purchases_context(self) -> tuple[str, int]:
        purchases = (
            Cart.objects.all()
            .prefetch_related("items__product")
            .order_by("-created_at")
        )
        if not purchases:
            return "No hay compras registradas.", 0

        lines: List[str] = []
        for purchase in purchases[:20]:
            item_descriptions = [
                f"{item.product.name} x{item.quantity} ({self._format_currency(item.unit_price * item.quantity)})"
                for item in purchase.items.all()
            ]
            items_text = ", ".join(item_descriptions) if item_descriptions else "sin productos"
            lines.append(
                (
                    f"Compra #{purchase.id} del {purchase.created_at:%Y-%m-%d %H:%M} "
                    f"por {self._format_currency(purchase.total_price)}: {items_text}"
                )
            )

        return "\n".join(lines), purchases.count()

    def _format_currency(self, value) -> str:
        try:
            amount = Decimal(str(value))
        except Exception:
            return str(value)
        normalized = amount.quantize(Decimal("0.01"))
        return f"{normalized} EUR"
