"""Command interpreter backed by Groq LLM."""

from __future__ import annotations

import json
from typing import List

import requests
from django.conf import settings

from core.cart.models import Cart
from core.products.models import Category, Product

from .common import GROQ_CHAT_URL, LOG, PromptServiceError, extract_error_detail


def _strip_code_fence(content: str) -> str:
    """Remove optional Markdown code fences from the LLM response."""

    cleaned = content.strip()
    if cleaned.startswith("```"):
        if cleaned.startswith("```json"):
            cleaned = cleaned[len("```json") :].strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    return cleaned


class PromptCommandInterpreter:
    """Convert natural language instructions into structured commands."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = (api_key or getattr(settings, "GROQ_API_KEY", "")).strip()
        self.model = (model or getattr(settings, "GROQ_MODEL", "")).strip()

    def translate(self, message: str) -> List[dict]:
        prompt = (message or "").strip()
        if not prompt:
            return []

        if not self.api_key:
            raise PromptServiceError(
                "No se pudo interpretar el comando de forma automatica. Envialo como JSON o configura GROQ_API_KEY."
            )

        inventory_context = self._build_inventory_context()
        payload = {
            "model": self.model or "llama3-70b-8192",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Eres un asistente que transforma instrucciones en espanol en una lista JSON de comandos "
                        "para gestionar productos, categorias y compras. Devuelve exclusivamente un objeto JSON "
                        "con la forma {\"commands\": [...]} sin explicaciones adicionales. Las acciones validas son: "
                        "list_categories, create_category, update_category, delete_category, assign_category, assign_category_to_all_products, "
                        "unassign_category, list_products, create_product, update_product, delete_product, product_metrics, "
                        "list_purchases, create_purchase, delete_purchase, delete_purchases_by_product y purchase_metrics. Para cada comando incluye "
                        "en 'data' los campos minimos necesarios:\n"
                        "- create_category: siempre 'name' y opcionalmente 'slug'.\n"
                        "- update_category: identifica la categoria y permite cambiar 'name', 'slug', 'description' o 'is_active'.\n"
                        "- assign_category/assign_category_to_all_products/unassign_category: identifica la categoria "
                        "por 'category_id', 'category_slug' o 'category_name'.\n"
                        "- list_products: permite 'order_by' (price|name), 'direction' (asc|desc) y banderas como "
                        "'include_categories'.\n"
                        "- create_product: requiere 'name', 'price' y 'stock'.\n"
                        "- update_product/delete_product: identifica el producto con 'product_id', 'product_slug' o 'product_name'. "
                        "Para actualizacion puedes incluir campos como 'price', 'stock', 'is_active', 'categories', "
                        "'assign_categories' y 'remove_categories'.\n"
                        "- product_metrics: usa 'metrics' (lista con valores como 'max_price' o 'min_price').\n"
                        "- list_purchases: acepta 'order_by' (total_price|name) y 'direction'.\n"
                        "- create_purchase: proporciona 'items', cada uno con 'product_id' o 'product_slug' y 'quantity'.\n"
                        "- delete_purchase: identifica la compra mediante 'purchase_id'.\n"
                        "- delete_purchases_by_product: identifica el producto y elimina todas las compras relacionadas.\n"
                        "- purchase_metrics: usa 'metrics' (max_price|min_price|max_items|min_items).\n"
                        "Si la instruccion no corresponde a estas operaciones responde {\"commands\": []}."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Inventario actual:\n"
                        + inventory_context
                        + "\n\nInstruccion: "
                        + prompt
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
            LOG.exception("Groq interpreter request failed")
            raise PromptServiceError(
                "No se pudo interpretar el comando. Intentalo nuevamente o usa JSON."
            ) from exc

        if response.status_code >= 400:
            detail = extract_error_detail(response)
            LOG.error("Groq interpreter error %s: %s", response.status_code, detail)
            raise PromptServiceError(
                f"Error al interpretar el comando ({response.status_code}): {detail}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise PromptServiceError("Respuesta invalida del interprete LLM.") from exc

        choices = data.get("choices") or []
        if not choices:
            return []

        content = choices[0].get("message", {}).get("content", "").strip()
        if not content:
            return []

        content = _strip_code_fence(content)

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise PromptServiceError("La respuesta del interprete no es JSON valido.") from exc

        commands = parsed.get("commands") if isinstance(parsed, dict) else None
        if commands is None:
            return []
        if not isinstance(commands, list):
            raise PromptServiceError("El interprete devolvio un formato de comandos invalido.")

        normalized: List[dict] = []
        for entry in commands:
            if not isinstance(entry, dict):
                raise PromptServiceError("Cada comando debe ser un objeto JSON.")
            action = str(entry.get("action") or "").strip().lower()
            if not action:
                raise PromptServiceError("Falta la accion en uno de los comandos interpretados.")
            data_block = entry.get("data") or {}
            if not isinstance(data_block, dict):
                raise PromptServiceError("El bloque 'data' del comando debe ser un objeto JSON.")
            normalized.append({"action": action, "data": data_block})

        return normalized

    def _build_inventory_context(self) -> str:
        products = (
            Product.objects.all()
            .order_by("id")
            .prefetch_related("categories")
        )
        categories = (
            Category.objects.all()
            .order_by("id")
            .prefetch_related("products")
        )
        purchases = (
            Cart.objects.all()
            .prefetch_related("items__product")
            .order_by("-created_at")[:20]
        )

        product_lines: List[str] = [
            "Productos actuales:" if products else "Productos actuales: ninguno"
        ]
        for product in products:
            product_lines.append(
                (
                    f"- id={product.id}, nombre={product.name}, slug={product.slug}, categorias="
                    + (
                        ", ".join(
                            category.name for category in product.categories.all().order_by("name")
                        )
                        or "sin categorias"
                    )
                )
            )

        category_lines: List[str] = [
            "Categorias actuales:" if categories else "Categorias actuales: ninguna"
        ]
        for category in categories:
            category_lines.append(
                (
                    f"- id={category.id}, nombre={category.name}, slug={category.slug}, productos="
                    + (
                        ", ".join(
                            product.name for product in category.products.all().order_by("name")
                        )
                        or "sin productos"
                    )
                )
            )

        purchase_lines: List[str] = [
            "Compras recientes:" if purchases else "Compras recientes: ninguna"
        ]
        for purchase in purchases:
            item_count = sum(item.quantity for item in purchase.items.all())
            purchase_lines.append(
                (
                    f"- id={purchase.id}, total={purchase.total_price}, articulos={item_count}"
                )
            )

        return "\n".join(product_lines + [""] + category_lines + [""] + purchase_lines)
