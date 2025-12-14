"""Utilities to interact with the Groq LLM and manage products via commands."""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import List, Sequence

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils.text import slugify

from core.cart.models import Cart, CartItem
from core.products.models import Category, Product

LOG = logging.getLogger(__name__)

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
WRITE_ACTIONS = {
    "create_category",
    "delete_category",
    "assign_category",
    "assign_category_to_all_products",
    "unassign_category",
    "create_product",
    "update_product",
    "delete_product",
    "create_purchase",
    "delete_purchase",
}


class PromptServiceError(Exception):
    """Raised when the prompt service cannot produce an answer."""


def extract_error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text or "Respuesta vacia."

    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)
    if isinstance(error, list):
        return "; ".join(str(item) for item in error)
    if error:
        return str(error)
    return str(payload)


class PromptCommandInterpreter:
    """Convert natural language instructions into structured commands via Groq."""

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
                        "list_categories, create_category, delete_category, assign_category, assign_category_to_all_products, "
                        "unassign_category, list_products, create_product, update_product, delete_product, product_metrics, "
                        "list_purchases, create_purchase, delete_purchase y purchase_metrics. Para cada comando incluye "
                        "en 'data' los campos minimos necesarios:\n"
                        "- create_category: siempre 'name' y opcionalmente 'slug'.\n"
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

        if content.startswith("```"):
            content = content.strip()
            if content.startswith("```json"):
                content = content[len("```json"):].strip()
            if content.endswith("```"):
                content = content[:-3].strip()

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


class PromptCommandProcessor:
    """Parse and execute structured commands sent through the prompt endpoint."""

    def __init__(self, interpreter: PromptCommandInterpreter | None = None) -> None:
        self.interpreter = interpreter

    def process_if_command(self, raw_message: str) -> dict | None:
        text = (raw_message or "").strip()
        if not text:
            return None

        lowered = text.lower()
        if lowered in {"help", "ayuda"}:
            return {
                "detail": "Comandos disponibles.",
                "answer": self._build_help_message(),
            }

        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise PromptServiceError("El comando JSON es invalido.") from exc

            if not isinstance(payload, dict):
                raise PromptServiceError("El comando JSON debe ser un objeto.")

            if "commands" in payload:
                commands = payload.get("commands")
                if not isinstance(commands, list):
                    raise PromptServiceError(
                        "El campo 'commands' debe ser una lista de objetos."
                    )
                normalized = []
                for entry in commands:
                    if not isinstance(entry, dict):
                        raise PromptServiceError("Cada comando debe ser un objeto JSON.")
                    action = str(entry.get("action") or "").strip().lower()
                    if not action:
                        raise PromptServiceError(
                            "Cada comando debe especificar el campo 'action'."
                        )
                    data_block = entry.get("data") or {}
                    if not isinstance(data_block, dict):
                        raise PromptServiceError(
                            "El campo 'data' de cada comando debe ser un objeto."
                        )
                    normalized.append({"action": action, "data": data_block})
                if not normalized:
                    return {
                        "detail": "Sin comandos a ejecutar.",
                        "answer": "No se proporcionaron comandos en la lista.",
                    }
                return self._execute_sequence(normalized)

            action = str(payload.get("action") or "").strip().lower()
            if not action:
                raise PromptServiceError("El comando debe incluir el campo 'action'.")

            handler = self._get_handler(action)
            if handler is None:
                raise PromptServiceError(
                    f"Accion desconocida: {action}. Usa 'help' para ver las opciones."
                )

            data = payload.get("data") or {}
            if not isinstance(data, dict):
                raise PromptServiceError("El campo 'data' debe ser un objeto JSON.")

            return handler(data)

        if self.interpreter is None:
            return None

        commands = self.interpreter.translate(text)
        if not commands:
            return None

        has_writes = any(
            (entry.get("action") or "").strip().lower() in WRITE_ACTIONS
            for entry in commands
        )
        if not has_writes:
            return None

        return self._execute_sequence(commands)

    def _get_handler(self, action: str):
        return {
            "list_categories": self._handle_list_categories,
            "create_category": self._handle_create_category,
            "delete_category": self._handle_delete_category,
            "assign_category": self._handle_assign_category,
            "assign_category_to_all_products": self._handle_assign_category_to_all_products,
            "unassign_category": self._handle_unassign_category,
            "list_products": self._handle_list_products,
            "create_product": self._handle_create_product,
            "update_product": self._handle_update_product,
            "delete_product": self._handle_delete_product,
            "product_metrics": self._handle_product_metrics,
            "list_purchases": self._handle_list_purchases,
            "create_purchase": self._handle_create_purchase,
            "delete_purchase": self._handle_delete_purchase,
            "purchase_metrics": self._handle_purchase_metrics,
        }.get(action.strip().lower())

    def _execute_sequence(self, commands: List[dict]) -> dict:
        answers: List[str] = []
        details: List[str] = []

        for command in commands:
            action = command.get("action") or ""
            data = command.get("data") or {}
            if not isinstance(data, dict):
                raise PromptServiceError("Cada comando debe incluir un objeto 'data'.")

            handler = self._get_handler(str(action))
            if handler is None:
                raise PromptServiceError(f"Accion desconocida en la secuencia: {action}.")

            result = handler(data)
            details.append(result.get("detail") or "Comando ejecutado")
            answer_text = result.get("answer")
            if answer_text:
                answers.append(answer_text)

        return {
            "detail": "; ".join(details) or "Comandos ejecutados.",
            "answer": "\n\n".join(answers),
        }

    def _handle_list_categories(self, data: dict) -> dict:
        include_products = self._parse_bool(data.get("include_products"), default=True)
        categories = (
            Category.objects.all()
            .order_by("name")
            .prefetch_related("products")
        )
        if not categories:
            return {
                "detail": "Categorias consultadas.",
                "answer": "No hay categorias registradas.",
            }

        lines: List[str] = []
        for category in categories:
            status = "activa" if category.is_active else "inactiva"
            line = f"- id={category.id}, nombre={category.name}, slug={category.slug}, estado={status}"
            if category.description:
                line += f", descripcion={category.description.strip()}"
            if include_products:
                products = ", ".join(
                    product.name for product in category.products.all().order_by("name")
                )
                line += ", productos=" + (products if products else "sin productos")
            lines.append(line)

        return {
            "detail": "Categorias consultadas.",
            "answer": "\n".join(lines),
        }

    def _handle_create_category(self, data: dict) -> dict:
        name = self._extract_text_value(
            data,
            ["name", "category_name", "title", "label", "category"],
            required=True,
            field_label="name",
        )

        description = self._extract_text_value(
            data,
            ["description", "detail", "notes"],
            default="",
        )
        is_active = self._parse_bool(data.get("is_active"), default=True)
        raw_slug = self._extract_text_value(
            data,
            ["slug", "category_slug"],
            default=name,
        )
        slug = self._build_unique_slug(Category, raw_slug)

        category = Category(name=name, slug=slug, description=description, is_active=is_active)
        try:
            category.full_clean()
        except ValidationError as exc:
            raise PromptServiceError(self._format_validation_error(exc)) from exc

        category.save()

        return {
            "detail": "Categoria creada.",
            "answer": f"Categoria {category.name} (id={category.id}, slug={category.slug}) creada correctamente.",
        }

    def _handle_delete_category(self, data: dict) -> dict:
        category = self._resolve_category(data)
        info = f"Categoria {category.name} (id={category.id}) eliminada."
        category.delete()
        return {"detail": "Categoria eliminada.", "answer": info}

    def _handle_assign_category(self, data: dict) -> dict:
        product = self._resolve_product(data)
        category = self._resolve_category(data)
        product.categories.add(category)
        return {
            "detail": "Categoria asignada.",
            "answer": f"Categoria {category.name} asignada al producto {product.name}.",
        }

    def _handle_assign_category_to_all_products(self, data: dict) -> dict:
        category = self._resolve_category(data)
        products = list(Product.objects.all().order_by("id"))
        if not products:
            return {
                "detail": "Categoria asignada a todos.",
                "answer": "No hay productos para asignar a la categoria.",
            }

        for product in products:
            product.categories.add(category)

        return {
            "detail": "Categoria asignada a todos.",
            "answer": (
                f"Categoria {category.name} asignada a {len(products)} producto(s)."
            ),
        }

    def _handle_unassign_category(self, data: dict) -> dict:
        product = self._resolve_product(data)
        category = self._resolve_category(data)
        product.categories.remove(category)
        return {
            "detail": "Categoria desasignada.",
            "answer": f"Categoria {category.name} removida del producto {product.name}.",
        }

    def _handle_list_products(self, data: dict) -> dict:
        order_fields = self._extract_ordering(
            data,
            default_field="name",
            allowed_fields={
                "name": ("name", "price"),
                "nombre": ("name", "price"),
                "price": ("price", "name"),
                "precio": ("price", "name"),
            },
        )
        products = (
            Product.objects.all()
            .order_by(*order_fields)
            .prefetch_related("categories")
        )
        if not products:
            return {
                "detail": "Productos consultados.",
                "answer": "No hay productos registrados.",
            }

        lines: List[str] = []
        for product in products:
            categories = ", ".join(
                category.name for category in product.categories.all().order_by("name")
            ) or "sin categorias"
            status = "activo" if product.is_active else "inactivo"
            lines.append(
                (
                    f"- id={product.id}, nombre={product.name}, slug={product.slug}, precio={product.price}, "
                    f"stock={product.stock}, estado={status}, categorias={categories}"
                )
            )

        return {
            "detail": "Productos consultados.",
            "answer": "\n".join(lines),
        }

    def _handle_create_product(self, data: dict) -> dict:
        name = self._extract_text_value(
            data,
            ["name", "product_name", "title", "label"],
            required=True,
            field_label="name",
        )

        description = self._extract_text_value(
            data,
            ["description", "detail", "notes"],
            default="",
        )
        if "price" not in data:
            raise PromptServiceError(
                "Debe indicar el precio del producto para poder registrarlo."
            )
        if "stock" not in data:
            raise PromptServiceError(
                "Debe indicar el stock disponible del producto para poder registrarlo."
            )

        price = self._parse_decimal(data.get("price"), field="price")
        stock = self._parse_int(data.get("stock"), field="stock")
        if price < 0:
            raise PromptServiceError("El precio del producto no puede ser negativo.")
        if stock < 0:
            raise PromptServiceError("El stock del producto no puede ser negativo.")
        is_active = self._parse_bool(data.get("is_active"), default=True)
        raw_slug = self._extract_text_value(
            data,
            ["slug", "product_slug"],
            default=name,
        )
        slug = self._build_unique_slug(Product, raw_slug)

        categories = self._parse_categories_list(data.get("categories"))

        product = Product(
            name=name,
            slug=slug,
            description=description,
            price=price,
            stock=stock,
            is_active=is_active,
        )

        try:
            product.full_clean()
        except ValidationError as exc:
            raise PromptServiceError(self._format_validation_error(exc)) from exc

        with transaction.atomic():
            product.save()
            if categories:
                product.categories.set(categories)

        return {
            "detail": "Producto creado.",
            "answer": (
                f"Producto {product.name} (id={product.id}, slug={product.slug}) creado con {len(categories)} categorias."
            ),
        }

    def _handle_update_product(self, data: dict) -> dict:
        product = self._resolve_product(data)

        fields_updated: List[str] = []

        if any(key in data for key in ["name", "product_name", "title"]):
            name = self._extract_text_value(
                data,
                ["name", "product_name", "title"],
                required=True,
                field_label="name",
            )
            product.name = name
            fields_updated.append("nombre")

        if any(key in data for key in ["description", "detail", "notes"]):
            product.description = self._extract_text_value(
                data,
                ["description", "detail", "notes"],
                default="",
            )
            fields_updated.append("descripcion")

        if "price" in data:
            product.price = self._parse_decimal(data.get("price"), field="price")
            if product.price < 0:
                raise PromptServiceError("El precio del producto no puede ser negativo.")
            fields_updated.append("precio")

        if "stock" in data:
            product.stock = self._parse_int(data.get("stock"), field="stock")
            if product.stock < 0:
                raise PromptServiceError("El stock del producto no puede ser negativo.")
            fields_updated.append("stock")

        if "is_active" in data:
            product.is_active = self._parse_bool(data.get("is_active"))
            fields_updated.append("estado")

        if any(key in data for key in ["slug", "product_slug"]):
            raw_slug = self._extract_text_value(
                data,
                ["slug", "product_slug"],
                default=product.name,
            )
            product.slug = self._build_unique_slug(Product, raw_slug, current_id=product.id)
            fields_updated.append("slug")

        categories_input = data.get("categories") if "categories" in data else None
        categories = None
        if categories_input is not None:
            categories = self._parse_categories_list(categories_input)

        assign_input = data.get("assign_categories") if "assign_categories" in data else None
        assign_categories = None
        if assign_input is not None:
            assign_categories = self._parse_categories_list(assign_input)

        remove_input = data.get("remove_categories") if "remove_categories" in data else None
        remove_categories = None
        if remove_input is not None:
            remove_categories = self._parse_categories_list(remove_input)

        try:
            product.full_clean()
        except ValidationError as exc:
            raise PromptServiceError(self._format_validation_error(exc)) from exc

        with transaction.atomic():
            product.save()
            if categories is not None:
                product.categories.set(categories)
            if assign_categories:
                product.categories.add(*assign_categories)
            if remove_categories:
                for category in remove_categories:
                    product.categories.remove(category)

        product.refresh_from_db()

        parts = fields_updated if fields_updated else ["sin cambios en campos simples"]
        detail = "Producto actualizado."
        answer_lines = [
            f"Producto {product.name} (id={product.id}) actualizado.",
            "Campos: " + ", ".join(parts),
        ]
        if categories is not None:
            answer_lines.append(
                "Categorias asignadas en total: "
                + (", ".join(category.name for category in product.categories.all().order_by("name")) or "sin categorias")
            )
        if assign_categories:
            answer_lines.append(
                "Categorias anadidas: "
                + (", ".join(category.name for category in assign_categories) or "ninguna")
            )
        if remove_categories:
            answer_lines.append(
                "Categorias retiradas: "
                + (", ".join(category.name for category in remove_categories) or "ninguna")
            )

        return {
            "detail": detail,
            "answer": "\n".join(answer_lines),
        }

    def _handle_delete_product(self, data: dict) -> dict:
        product = self._resolve_product(data)
        if product.stock > 0:
            raise PromptServiceError(
                "No es posible eliminar el producto porque el stock es superior a 0."
            )
        info = f"Producto {product.name} (id={product.id}) eliminado."
        product.delete()
        return {"detail": "Producto eliminado.", "answer": info}

    def _handle_product_metrics(self, data: dict) -> dict:
        metrics = self._normalize_metric_list(data.get("metrics"), default=["max_price", "min_price"])
        allowed_labels = {
            "max_price": "Producto con el precio mas elevado",
            "min_price": "Producto con el precio mas reducido",
        }

        for metric in metrics:
            if metric not in allowed_labels:
                raise PromptServiceError(
                    f"La metrica solicitada '{metric}' no es valida para productos."
                )

        if not Product.objects.exists():
            return {
                "detail": "Metricas de producto consultadas.",
                "answer": "No hay productos registrados para calcular metricas.",
            }

        lines: List[str] = []
        for metric in metrics:
            product = self._select_product_by_metric(metric)
            if product is None:
                continue
            lines.append(
                (
                    f"{allowed_labels[metric]}: {product.name} (id={product.id}, precio={self._format_currency(product.price)}, "
                    f"stock={product.stock})."
                )
            )

        if not lines:
            lines.append("No fue posible calcular las metricas solicitadas.")

        return {
            "detail": "Metricas de producto consultadas.",
            "answer": "\n".join(lines),
        }

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

        purchases = (
            Cart.objects.all()
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

        with transaction.atomic():
            for item in purchase.items.all():
                product = item.product
                product.stock += item.quantity
                product.save(update_fields=["stock"])
            purchase.delete()

        return {
            "detail": "Compra eliminada.",
            "answer": f"La compra con identificador {purchase.id} se elimino y el stock se restablecio.",
        }

    def _handle_purchase_metrics(self, data: dict) -> dict:
        metrics = self._normalize_metric_list(data.get("metrics"), default=["max_price", "min_price"])
        allowed_labels = {
            "max_price": "Compra con el importe mas elevado",
            "min_price": "Compra con el importe mas reducido",
            "max_items": "Compra con la mayor cantidad de articulos",
            "min_items": "Compra con la menor cantidad de articulos",
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

    def _parse_bool(self, value, default: bool | None = None):
        if value is None:
            if default is None:
                raise PromptServiceError("Se esperaba un valor booleano.")
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "y", "si", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
        raise PromptServiceError(f"No se pudo interpretar el valor booleano: {value!r}.")

    def _parse_decimal(self, value, default=None, field: str = "price") -> Decimal:
        if value is None:
            if default is None:
                raise PromptServiceError(f"El campo '{field}' es obligatorio.")
            value = default
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError) as exc:
            raise PromptServiceError(f"Valor invalido para '{field}'.") from exc

    def _parse_int(self, value, default=None, field: str = "valor") -> int:
        if value is None:
            if default is None:
                raise PromptServiceError(f"El campo '{field}' es obligatorio.")
            value = default
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise PromptServiceError(f"Valor entero invalido para '{field}'.") from exc

    def _build_unique_slug(
        self, model, raw_slug: str, *, current_id: int | None = None
    ) -> str:
        base_slug = slugify(raw_slug) or "item"
        slug = base_slug
        suffix = 1
        queryset = model.objects.all()
        if current_id is not None:
            queryset = queryset.exclude(id=current_id)
        while queryset.filter(slug=slug).exists():
            suffix += 1
            slug = f"{base_slug}-{suffix}"
        return slug

    def _extract_ordering(
        self,
        data: dict,
        *,
        default_field: str,
        allowed_fields: dict[str, str | Sequence[str]],
        default_direction: str = "asc",
    ) -> List[str]:
        default_field_name = default_field.lstrip("+-")
        inferred_default_direction = (
            "desc" if default_field.startswith("-") else "asc"
        )
        direction_default = default_direction or inferred_default_direction

        raw_order = data.get("order_by") or data.get("order") or default_field_name
        order_key = str(raw_order or default_field_name).strip().lower()
        if not order_key:
            order_key = default_field_name

        raw_direction = (
            data.get("direction")
            or data.get("order_direction")
            or data.get("sort")
            or direction_default
        )
        direction_value = str(raw_direction or direction_default).strip().lower()
        if direction_value in {"ascendente", "ascending"}:
            direction_value = "asc"
        if direction_value in {"descendente", "descending"}:
            direction_value = "desc"
        if direction_value not in {"asc", "desc"}:
            raise PromptServiceError(
                "La direccion de orden proporcionada no es valida. Use 'asc' o 'desc'."
            )

        resolved = allowed_fields.get(order_key)
        if resolved is None:
            valid_keys = ", ".join(sorted(allowed_fields.keys()))
            raise PromptServiceError(
                f"El campo de orden '{order_key}' no es valido. Opciones permitidas: {valid_keys}."
            )

        if isinstance(resolved, (list, tuple)):
            fields = list(resolved)
        else:
            fields = [resolved]

        ordered_fields: List[str] = []
        for field in fields:
            field_name = field.lstrip("+-")
            if direction_value == "desc":
                ordered_fields.append(f"-{field_name}")
            else:
                ordered_fields.append(field_name)

        return ordered_fields

    def _normalize_metric_list(
        self,
        raw_metrics,
        *,
        default: Sequence[str],
    ) -> List[str]:
        if raw_metrics is None:
            return [metric.lower() for metric in default]
        if isinstance(raw_metrics, (list, tuple, set)):
            metrics = [str(item).strip().lower() for item in raw_metrics if str(item).strip()]
        else:
            metrics = [str(raw_metrics).strip().lower()]
        return metrics or [metric.lower() for metric in default]

    def _request_additional_data(self, message: str) -> dict:
        return {
            "detail": "Informacion adicional requerida.",
            "answer": message,
        }

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

    def _select_product_by_metric(self, metric: str) -> Product | None:
        if metric == "max_price":
            return Product.objects.order_by("-price", "name").first()
        if metric == "min_price":
            return Product.objects.order_by("price", "name").first()
        return None

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

    def _format_currency(self, value) -> str:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError):
            return str(value)
        normalized = amount.quantize(Decimal("0.01"))
        return f"{normalized} EUR"

    def _format_datetime(self, value) -> str:
        try:
            return value.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(value)

    def _parse_categories_list(self, value) -> List[Category]:
        if value is None:
            return []
        if isinstance(value, str):
            tokens = [token.strip() for token in value.split(",") if token.strip()]
        elif isinstance(value, Sequence):
            tokens = value
        else:
            raise PromptServiceError(
                "El campo 'categories' debe ser una cadena o una lista."
            )

        categories: List[Category] = []
        for token in tokens:
            if isinstance(token, dict):
                candidate = {}
                if "category_id" in token or "id" in token:
                    candidate["category_id"] = token.get("category_id") or token.get("id")
                if "category_slug" in token or "slug" in token:
                    candidate["category_slug"] = token.get("category_slug") or token.get("slug")
                if "category_name" in token or "name" in token:
                    candidate["category_name"] = token.get("category_name") or token.get("name")
                if not candidate:
                    raise PromptServiceError(
                        "No se pudo interpretar una categoria de la lista proporcionada."
                    )
                category = self._resolve_category(candidate)
            else:
                category = self._resolve_category_token(token)
            categories.append(category)
        return categories

    def _resolve_category(self, data: dict, prefix: str = "category") -> Category:
        identifier = (
            data.get(f"{prefix}_id")
            if f"{prefix}_id" in data
            else data.get(f"{prefix}_slug")
        )
        if identifier is not None:
            return self._resolve_category_token(identifier)

        if f"{prefix}_name" in data:
            name = (data.get(f"{prefix}_name") or "").strip()
            if not name:
                raise PromptServiceError("El 'name' de la categoria no puede estar vacio.")
            try:
                return Category.objects.get(name__iexact=name)
            except Category.DoesNotExist as exc:
                raise PromptServiceError(f"La categoria '{name}' no existe.") from exc

        raise PromptServiceError("No se proporciono informacion suficiente de la categoria.")

    def _resolve_category_token(self, token) -> Category:
        if isinstance(token, int):
            try:
                return Category.objects.get(id=token)
            except Category.DoesNotExist as exc:
                raise PromptServiceError(f"Categoria con id={token} no existe.") from exc

        text = str(token).strip()
        if not text:
            raise PromptServiceError("Identificador de categoria vacio.")

        if text.isdigit():
            try:
                return Category.objects.get(id=int(text))
            except Category.DoesNotExist as exc:
                raise PromptServiceError(f"Categoria con id={text} no existe.") from exc

        try:
            return Category.objects.get(slug=text)
        except Category.DoesNotExist:
            try:
                return Category.objects.get(name__iexact=text)
            except Category.DoesNotExist as exc:
                raise PromptServiceError(f"Categoria '{text}' no existe.") from exc

    def _resolve_product(self, data: dict, prefix: str = "product") -> Product:
        if f"{prefix}_id" in data:
            try:
                return Product.objects.get(id=data[f"{prefix}_id"])
            except Product.DoesNotExist as exc:
                raise PromptServiceError(
                    f"Producto con id={data[f'{prefix}_id']} no existe."
                ) from exc

        if f"{prefix}_slug" in data:
            slug = (data.get(f"{prefix}_slug") or "").strip()
            if not slug:
                raise PromptServiceError("El slug del producto no puede estar vacio.")
            try:
                return Product.objects.get(slug=slug)
            except Product.DoesNotExist as exc:
                raise PromptServiceError(f"Producto con slug '{slug}' no existe.") from exc

        if f"{prefix}_name" in data:
            name = (data.get(f"{prefix}_name") or "").strip()
            if not name:
                raise PromptServiceError("El nombre del producto no puede estar vacio.")
            matches = list(Product.objects.filter(name__iexact=name))
            if not matches:
                raise PromptServiceError(f"Producto '{name}' no existe.")
            if len(matches) > 1:
                raise PromptServiceError(
                    f"Existen varios productos con el nombre '{name}'. Usa el id o slug."
                )
            return matches[0]

        raise PromptServiceError("No se proporciono informacion suficiente del producto.")

    def _extract_text_value(
        self,
        data: dict,
        keys: List[str],
        *,
        default: str | None = None,
        required: bool = False,
        field_label: str = "valor",
    ) -> str:
        for key in keys:
            if key in data:
                value = data.get(key)
                if value is None:
                    continue
                text = str(value).strip()
                if text:
                    return text
        if default is not None:
            return default
        if required:
            raise PromptServiceError(
                f"El campo '{field_label}' es obligatorio para el comando."
            )
        return ""

    def _format_validation_error(self, exc: ValidationError) -> str:
        if hasattr(exc, "message_dict"):
            parts = []
            for field, messages in exc.message_dict.items():
                joined = ", ".join(messages)
                parts.append(f"{field}: {joined}")
            return "; ".join(parts)
        if exc.messages:
            return "; ".join(exc.messages)
        return "Datos invalidos."

    def _build_help_message(self) -> str:
        return (
            "Comandos JSON disponibles:\n"
            "- Listar categorias: {\"action\": \"list_categories\"}\n"
            "- Crear categoria: {\"action\": \"create_category\", \"data\": {\"name\": \"Snacks\"}}\n"
            "- Eliminar categoria por id: {\"action\": \"delete_category\", \"data\": {\"category_id\": 3}}\n"
            "- Asignar categoria: {\"action\": \"assign_category\", \"data\": {\"product_id\": 1, \"category_id\": 3}}\n"
            "- Asignar categoria a todos: {\"action\": \"assign_category_to_all_products\", \"data\": {\"category_id\": 3}}\n"
            "- Desasignar categoria: {\"action\": \"unassign_category\", \"data\": {\"product_id\": 1, \"category_id\": 3}}\n"
            "- Listar productos: {\"action\": \"list_products\"}\n"
            "- Crear producto: {\"action\": \"create_product\", \"data\": {\"name\": \"Cafe\", \"price\": \"9.99\", \"stock\": 10}}\n"
            "- Actualizar producto: {\"action\": \"update_product\", \"data\": {\"product_id\": 1, \"price\": \"12.50\"}}\n"
            "- Eliminar producto: {\"action\": \"delete_product\", \"data\": {\"product_id\": 1}}\n"
            "- Metricas de productos: {\"action\": \"product_metrics\", \"data\": {\"metrics\": [\"max_price\", \"min_price\"]}}\n"
            "- Listar compras: {\"action\": \"list_purchases\", \"data\": {\"order_by\": \"total\", \"direction\": \"desc\"}}\n"
            "- Crear compra: {\"action\": \"create_purchase\", \"data\": {\"items\": [{\"product_id\": 1, \"quantity\": 2}]}}\n"
            "- Eliminar compra: {\"action\": \"delete_purchase\", \"data\": {\"purchase_id\": 5}}\n"
            "- Metricas de compras: {\"action\": \"purchase_metrics\", \"data\": {\"metrics\": [\"max_price\", \"max_items\"]}}\n"
            "Escribe 'help' para volver a ver esta lista."
        )


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

        products_block = self._build_products_context()
        categories_block = self._build_categories_context()
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
