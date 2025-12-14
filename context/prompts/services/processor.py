"""Command processor that executes structured prompt instructions."""

from __future__ import annotations

import json
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, List, Sequence

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.text import slugify
from django.utils.dateparse import parse_date, parse_datetime

from .common import (
    PromptActionCancelled,
    PromptPendingAction,
    PromptServiceError,
)
from .categories import CategoryCommandMixin
from .products import ProductCommandMixin
from .purchases import PurchaseCommandMixin

if TYPE_CHECKING:  # pragma: no cover - imported only for type checking
    from .interpreter import PromptCommandInterpreter


WRITE_ACTIONS = {
    "create_category",
    "delete_category",
    "update_category",
    "assign_category",
    "assign_category_to_all_products",
    "unassign_category",
    "create_product",
    "update_product",
    "delete_product",
    "create_purchase",
    "delete_purchase",
    "delete_purchases_by_product",
}


class PromptCommandProcessor(
    CategoryCommandMixin,
    ProductCommandMixin,
    PurchaseCommandMixin,
):
    """Parse and execute structured commands sent through the prompt endpoint."""

    def __init__(self, interpreter: "PromptCommandInterpreter" | None = None) -> None:
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

                normalized: List[dict] = []
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
            "update_category": self._handle_update_category,
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
            "delete_purchases_by_product": self._handle_delete_purchases_by_product,
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
                raise PromptServiceError(
                    f"Accion desconocida en la secuencia: {action}."
                )

            result = handler(data)
            details.append(result.get("detail") or "Comando ejecutado")
            answer_text = result.get("answer")
            if answer_text:
                answers.append(answer_text)

        return {
            "detail": "; ".join(details) or "Comandos ejecutados.",
            "answer": "\n\n".join(answers),
        }

    # Shared helpers ---------------------------------------------------

    def _ensure_confirmation(
        self,
        *,
        action: str,
        data: dict,
        detail: str,
        prompt: str,
    ) -> None:
        confirm_value = data.get("confirm")
        if confirm_value is None and "confirmation" in data:
            confirm_value = data.get("confirmation")

        if confirm_value is None:
            pending_data = dict(data)
            pending_data.pop("confirm", None)
            pending_data.pop("confirmation", None)
            raise PromptPendingAction(
                detail,
                command={"action": action, "data": pending_data},
                confirmation_message=prompt,
            )

        try:
            confirmed = self._parse_bool(confirm_value)
        except PromptServiceError as exc:
            pending_data = dict(data)
            pending_data.pop("confirm", None)
            pending_data.pop("confirmation", None)
            raise PromptPendingAction(
                detail,
                command={"action": action, "data": pending_data},
                confirmation_message=prompt,
            ) from exc

        if not confirmed:
            raise PromptActionCancelled("La operacion fue cancelada por el usuario.")

        data["confirm"] = True
        data.pop("confirmation", None)

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
        raise PromptServiceError(
            f"No se pudo interpretar el valor booleano: {value!r}."
        )

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
            metrics = [
                str(item).strip().lower()
                for item in raw_metrics
                if str(item).strip()
            ]
        else:
            metrics = [str(raw_metrics).strip().lower()]
        return metrics or [metric.lower() for metric in default]

    def _request_additional_data(self, message: str) -> dict:
        return {
            "detail": "Informacion adicional requerida.",
            "answer": message,
        }

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
        except Exception:  # pragma: no cover - defensive fallback
            return str(value)

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
            '- Listar categorias: {"action": "list_categories"}\n'
            '- Crear categoria: {"action": "create_category", "data": {"name": "Snacks"}}\n'
            '- Actualizar categoria: {"action": "update_category", "data": {"category_slug": "snacks", "name": "Bebidas"}}\n'
            '- Eliminar categoria por id: {"action": "delete_category", "data": {"category_id": 3}}\n'
            '- Asignar categoria: {"action": "assign_category", "data": {"product_id": 1, "category_id": 3}}\n'
            '- Asignar categoria a todos: {"action": "assign_category_to_all_products", "data": {"category_id": 3}}\n'
            '- Desasignar categoria: {"action": "unassign_category", "data": {"product_id": 1, "category_id": 3}}\n'
            '- Listar productos: {"action": "list_products"}\n'
            '- Crear producto: {"action": "create_product", "data": {"name": "Cafe", "price": "9.99", "stock": 10}}\n'
            '- Actualizar producto: {"action": "update_product", "data": {"product_id": 1, "price": "12.50"}}\n'
            '- Eliminar producto: {"action": "delete_product", "data": {"product_id": 1}}\n'
            '- Metricas de productos: {"action": "product_metrics", "data": {"metrics": ["max_price", "min_price"]}}\n'
            '- Listar compras: {"action": "list_purchases", "data": {"order_by": "total", "direction": "desc", "start_date": "2024-01-01", "end_date": "2024-01-31", "min_price": "10.00", "product_slug": "cafe"}}\n'
            '- Crear compra: {"action": "create_purchase", "data": {"items": [{"product_id": 1, "quantity": 2}]}}\n'
            '- Eliminar compra: {"action": "delete_purchase", "data": {"purchase_id": 5, "confirm": true}}\n'
            '- Eliminar compras por producto: {"action": "delete_purchases_by_product", "data": {"product_slug": "cafe", "confirm": true}}\n'
            '- Metricas de compras: {"action": "purchase_metrics", "data": {"metrics": ["max_price", "max_items"]}}\n'
            "Escribe 'help' para volver a ver esta lista."
        )
    
    def _parse_datetime_boundary(
        self,
        value,
        *,
        field: str,
        is_end: bool = False,
    ) -> datetime:
        if isinstance(value, datetime):
            dt = value
        else:
            text = str(value or "").strip()
            if not text:
                raise PromptServiceError(
                    f"El campo '{field}' debe incluir una fecha valida."
                )

            parsed_dt = parse_datetime(text)
            if parsed_dt is None:
                parsed_date = parse_date(text)
                if parsed_date is None:
                    raise PromptServiceError(
                        f"No se pudo interpretar la fecha indicada en '{field}'. Usa el formato YYYY-MM-DD."
                    )
                boundary = time.max.replace(microsecond=0) if is_end else time.min
                dt = datetime.combine(parsed_date, boundary)
            else:
                dt = parsed_dt

        if dt.tzinfo is None:
            dt = timezone.make_aware(dt, timezone.get_current_timezone())

        return dt
