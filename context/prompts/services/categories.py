"""Category-related prompt command handlers."""

from __future__ import annotations

from typing import List, Sequence

from core.products.models import Category, Product

from .common import PromptPendingAction, PromptServiceError


class CategoryCommandMixin:
    """Provide handlers and helpers for category operations."""

    # Category handlers -------------------------------------------------

    def _handle_list_categories(self, data: dict) -> dict:
        self._parse_bool(data.get("include_products"), default=True)
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
            lines.append(line)

        return {
            "detail": "Categorias consultadas.",
            "answer": "\n".join(lines),
        }

    def _handle_create_category(self, data: dict) -> dict:
        name_keys = ["name", "category_name", "title", "label", "category"]
        try:
            name = self._extract_text_value(
                data,
                name_keys,
                required=True,
                field_label="name",
            )
        except PromptServiceError as exc:
            pending_data = dict(data)
            pending_data.pop("confirm", None)
            pending_data.pop("confirmation", None)
            raise PromptPendingAction(
                "Falta el nombre de la categoria.",
                command={"action": "create_category", "data": pending_data},
                requirements=[
                    {
                        "field": "name",
                        "label": "Nombre de la categoria",
                        "prompt": "Indica el nombre de la categoria a crear.",
                    }
                ],
            ) from exc

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

        confirm_detail = f"Confirma la creacion de la categoria '{name}'."
        confirm_prompt = f"¿Deseas crear la categoria '{name}'?"
        self._ensure_confirmation(
            action="create_category",
            data=data,
            detail=confirm_detail,
            prompt=confirm_prompt,
        )

        category = Category(name=name, slug=slug, description=description, is_active=is_active)
        try:
            category.full_clean()
        except Exception as exc:  # ValidationError
            raise PromptServiceError(self._format_validation_error(exc)) from exc

        category.save()

        return {
            "detail": "Categoria creada.",
            "answer": f"Categoria {category.name} (id={category.id}, slug={category.slug}) creada correctamente.",
        }

    def _handle_update_category(self, data: dict) -> dict:
        category = self._resolve_category(data)

        updated_fields: List[str] = []
        django_fields: List[str] = []

        if any(key in data for key in ["name", "category_name", "title", "label"]):
            new_name = self._extract_text_value(
                data,
                ["name", "category_name", "title", "label"],
                required=True,
                field_label="name",
            )
            if new_name != category.name:
                category.name = new_name
                updated_fields.append("nombre")
                django_fields.append("name")

        if any(key in data for key in ["description", "detail", "notes"]):
            new_description = self._extract_text_value(
                data,
                ["description", "detail", "notes"],
                default="",
            )
            if new_description != (category.description or ""):
                category.description = new_description
                updated_fields.append("descripcion")
                django_fields.append("description")

        if "is_active" in data:
            new_status = self._parse_bool(data.get("is_active"))
            if new_status != category.is_active:
                category.is_active = new_status
                updated_fields.append("estado")
                django_fields.append("is_active")

        if any(key in data for key in ["slug", "category_slug"]):
            raw_slug = self._extract_text_value(
                data,
                ["slug", "category_slug"],
                required=True,
                field_label="slug",
            )
            new_slug = self._build_unique_slug(Category, raw_slug, current_id=category.id)
            if new_slug != category.slug:
                category.slug = new_slug
                updated_fields.append("slug")
                django_fields.append("slug")
        elif data.get("refresh_slug"):
            auto_slug = self._build_unique_slug(Category, category.name, current_id=category.id)
            if auto_slug != category.slug:
                category.slug = auto_slug
                updated_fields.append("slug")
                django_fields.append("slug")

        if not updated_fields:
            return {
                "detail": "Categoria sin cambios.",
                "answer": "No se detectaron cambios para aplicar en la categoria.",
            }

        try:
            category.full_clean()
        except Exception as exc:  # ValidationError
            raise PromptServiceError(self._format_validation_error(exc)) from exc

        category.save(update_fields=django_fields)

        return {
            "detail": "Categoria actualizada.",
            "answer": (
                f"Categoria {category.name} (id={category.id}) actualizada. Campos modificados: {', '.join(updated_fields)}."
            ),
        }

    def _handle_delete_category(self, data: dict) -> dict:
        category = self._resolve_category(data)

        confirm_detail = f"Confirma la eliminacion de la categoria '{category.name}'."
        confirm_prompt = f"¿Deseas eliminar la categoria '{category.name}'?"
        self._ensure_confirmation(
            action="delete_category",
            data=data,
            detail=confirm_detail,
            prompt=confirm_prompt,
        )

        category.delete()

        return {
            "detail": "Categoria eliminada.",
            "answer": f"Categoria {category.name} (id={category.id}) eliminada correctamente.",
        }

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

    # Category helpers -------------------------------------------------

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


__all__ = ["CategoryCommandMixin"]
