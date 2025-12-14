"""Product-related prompt command handlers."""

from __future__ import annotations

from typing import List

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.deletion import ProtectedError

from core.products.models import Product

from .common import PromptServiceError


class ProductCommandMixin:
    """Provide handlers and helpers for product operations."""

    # Product handlers --------------------------------------------------

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
        confirm_detail = f"Confirma la eliminacion del producto '{product.name}'."
        confirm_prompt = f"¿Deseas eliminar el producto '{product.name}'?"
        self._ensure_confirmation(
            action="delete_product",
            data=data,
            detail=confirm_detail,
            prompt=confirm_prompt,
        )
        info = f"Producto {product.name} (id={product.id}) eliminado."
        try:
            product.delete()
        except ProtectedError as exc:
            raise PromptServiceError(
                "No es posible eliminar el producto porque esta asociado a compras existentes."
            ) from exc
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
                f"{allowed_labels[metric]}: {product.name} (precio {self._format_currency(product.price)})"
            )

        if not lines:
            lines.append("No fue posible calcular las metricas solicitadas.")

        return {
            "detail": "Metricas de producto consultadas.",
            "answer": "\n".join(lines),
        }

    # Product helpers --------------------------------------------------

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

    def _select_product_by_metric(self, metric: str) -> Product | None:
        if metric == "max_price":
            return Product.objects.order_by("-price", "name").first()
        if metric == "min_price":
            return Product.objects.order_by("price", "name").first()
        return None


__all__ = ["ProductCommandMixin"]
