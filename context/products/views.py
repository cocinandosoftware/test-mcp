import json
from decimal import Decimal
from typing import Dict, List

from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from core.cart.models import Cart, CartItem
from core.products.models import Category, Product

from .forms import CategoryForm, ProductForm


SESSION_CART_KEY = "shopping_cart"
RECENT_PURCHASES_LIMIT = 10


def _get_session_cart(request) -> Dict[str, Dict[str, str]]:
    cart = request.session.get(SESSION_CART_KEY)
    if not isinstance(cart, dict):
        return {}
    return cart


def _save_session_cart(request, cart: Dict[str, Dict[str, str]]) -> None:
    request.session[SESSION_CART_KEY] = cart
    request.session.modified = True


def _serialize_cart(cart: Dict[str, Dict[str, str]]) -> Dict[str, object]:
    items: List[Dict[str, object]] = []
    total = Decimal("0.00")
    count = 0
    for product_id, data in cart.items():
        try:
            quantity = int(data.get("quantity", 0))
        except (ValueError, TypeError):
            quantity = 0
        price = Decimal(str(data.get("unit_price", "0")))
        line_total = price * quantity
        total += line_total
        count += quantity
        items.append(
            {
                "product_id": int(product_id),
                "name": data.get("name", ""),
                "quantity": quantity,
                "unit_price": f"{price:.2f}",
                "line_total": f"{line_total:.2f}",
            }
        )

    items.sort(key=lambda item: item["name"].lower())
    return {
        "items": items,
        "total": f"{total:.2f}",
        "count": count,
    }


def _serialize_purchase(cart: Cart) -> Dict[str, object]:
    items = []
    for item in cart.items.all():
        line_total = item.unit_price * item.quantity
        items.append(
            {
                "product": item.product.name,
                "product_id": item.product_id,
                "quantity": item.quantity,
                "unit_price": f"{item.unit_price:.2f}",
                "line_total": f"{line_total:.2f}",
            }
        )
    return {
        "id": cart.id,
        "created_at": cart.created_at.isoformat(),
        "total_price": f"{cart.total_price:.2f}",
        "items": items,
    }


def _recent_purchases(limit: int = RECENT_PURCHASES_LIMIT) -> List[Dict[str, object]]:
    carts = (
        Cart.objects.all()
        .prefetch_related("items__product")
        .order_by("-created_at")[:limit]
    )
    return [_serialize_purchase(cart) for cart in carts]


def product_list(request):
    """Return a lightweight JSON list of active products."""

    products = (
        Product.objects.filter(is_active=True)
        .order_by("name")
        .values("id", "name", "slug", "price", "stock")
    )
    return JsonResponse({"products": list(products)})


def product_manage_list(request):
    """Display all products for management and quick access to category admin."""

    products = Product.objects.all().select_related().prefetch_related("categories").order_by("name")
    categories = Category.objects.all().order_by("name")
    purchases_data = _recent_purchases()
    initial_cart = _serialize_cart(_get_session_cart(request))
    return render(
        request,
        "products/manage_list.html",
        {
            "products": products,
            "categories": categories,
            "purchases_data": purchases_data,
            "initial_cart": initial_cart,
        },
    )


def product_create(request):
    if request.method == "POST":
        form = ProductForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Producto creado correctamente.")
            return redirect(reverse("products:product-manage"))
    else:
        form = ProductForm()
    return render(
        request,
        "products/product_form.html",
        {
            "form": form,
            "title": "Crear producto",
        },
    )


def product_update(request, pk: int):
    product = get_object_or_404(Product, pk=pk)
    if request.method == "POST":
        form = ProductForm(request.POST, instance=product)
        if form.is_valid():
            form.save()
            messages.success(request, "Producto actualizado correctamente.")
            return redirect(reverse("products:product-manage"))
    else:
        form = ProductForm(instance=product)
    return render(
        request,
        "products/product_form.html",
        {
            "form": form,
            "title": "Editar producto",
        },
    )


def product_delete(request, pk: int):
    product = get_object_or_404(Product, pk=pk)
    if request.method == "POST":
        product.delete()
        messages.success(request, "Producto eliminado correctamente.")
        return redirect(reverse("products:product-manage"))
    return render(
        request,
        "products/product_confirm_delete.html",
        {
            "product": product,
        },
    )


def category_list(request):
    categories = Category.objects.all().order_by("name")
    return render(
        request,
        "products/category_list.html",
        {
            "categories": categories,
        },
    )


def category_create(request):
    if request.method == "POST":
        form = CategoryForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Categoria creada correctamente.")
            return redirect(reverse("products:category-list"))
    else:
        form = CategoryForm()
    return render(
        request,
        "products/category_form.html",
        {
            "form": form,
            "title": "Crear categoria",
        },
    )


def category_update(request, pk: int):
    category = get_object_or_404(Category, pk=pk)
    if request.method == "POST":
        form = CategoryForm(request.POST, instance=category)
        if form.is_valid():
            form.save()
            messages.success(request, "Categoria actualizada correctamente.")
            return redirect(reverse("products:category-list"))
    else:
        form = CategoryForm(instance=category)
    return render(
        request,
        "products/category_form.html",
        {
            "form": form,
            "title": "Editar categoria",
        },
    )


def category_delete(request, pk: int):
    category = get_object_or_404(Category, pk=pk)
    if request.method == "POST":
        category.delete()
        messages.success(request, "Categoria eliminada correctamente.")
        return redirect(reverse("products:category-list"))
    return render(
        request,
        "products/category_confirm_delete.html",
        {
            "category": category,
        },
    )


@require_GET
def cart_detail(request):
    cart = _serialize_cart(_get_session_cart(request))
    return JsonResponse({"status": "ok", "cart": cart})


@require_POST
def cart_add(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, AttributeError):
        return JsonResponse({"error": "JSON invalido."}, status=400)

    product_id = payload.get("product_id")
    quantity = payload.get("quantity", 1)

    try:
        product_id = int(product_id)
    except (ValueError, TypeError):
        return JsonResponse({"error": "Identificador de producto invalido."}, status=400)

    try:
        quantity = int(quantity)
    except (ValueError, TypeError):
        quantity = 1

    if quantity <= 0:
        return JsonResponse({"error": "La cantidad debe ser mayor a cero."}, status=400)

    product = get_object_or_404(Product, pk=product_id)

    cart = _get_session_cart(request)
    key = str(product.id)
    stored = cart.get(key, {"quantity": 0, "name": product.name, "unit_price": str(product.price)})
    previous_quantity = int(stored.get("quantity", 0))
    stored.update(
        {
            "quantity": previous_quantity + quantity,
            "name": product.name,
            "unit_price": str(product.price),
        }
    )
    cart[key] = stored
    _save_session_cart(request, cart)

    return JsonResponse(
        {
            "status": "ok",
            "detail": f"{product.name} agregado al carrito.",
            "cart": _serialize_cart(cart),
        }
    )


@require_POST
def cart_clear(request):
    _save_session_cart(request, {})
    return JsonResponse(
        {
            "status": "ok",
            "detail": "Carrito vaciado.",
            "cart": _serialize_cart({}),
        }
    )


@require_POST
def cart_checkout(request):
    cart_data = _get_session_cart(request)
    serialized = _serialize_cart(cart_data)

    if serialized["count"] <= 0:
        return JsonResponse({"error": "El carrito esta vacio."}, status=400)

    product_ids = [item["product_id"] for item in serialized["items"]]
    products = {
        product.id: product for product in Product.objects.filter(id__in=product_ids)
    }

    if len(products) != len(product_ids):
        return JsonResponse(
            {"error": "Alguno de los productos ya no esta disponible."}, status=400
        )

    with transaction.atomic():
        cart_record = Cart.objects.create(total_price=Decimal(serialized["total"]))
        cart_items: List[CartItem] = []
        for item in serialized["items"]:
            product = products[item["product_id"]]
            cart_items.append(
                CartItem(
                    cart=cart_record,
                    product=product,
                    quantity=item["quantity"],
                    unit_price=Decimal(item["unit_price"]),
                )
            )
        CartItem.objects.bulk_create(cart_items)

    _save_session_cart(request, {})

    purchases = _recent_purchases()
    return JsonResponse(
        {
            "status": "ok",
            "detail": "Compra registrada correctamente.",
            "purchase": _serialize_purchase(cart_record),
            "cart": _serialize_cart({}),
            "purchases": purchases,
        }
    )


@require_GET
def purchase_list(request):
    return JsonResponse(
        {
            "status": "ok",
            "purchases": _recent_purchases(),
        }
    )


@require_POST
def purchase_delete(request, pk: int):
    purchase = get_object_or_404(Cart, pk=pk)
    purchase.delete()
    return JsonResponse(
        {
            "status": "ok",
            "detail": "Compra eliminada correctamente.",
            "purchases": _recent_purchases(),
        }
    )
