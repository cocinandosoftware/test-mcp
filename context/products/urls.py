from django.urls import path

from . import views

app_name = "products"

urlpatterns = [
    path("", views.product_list, name="product-list"),
    path("manage/", views.product_manage_list, name="product-manage"),
    path("manage/create/", views.product_create, name="product-create"),
    path("manage/<int:pk>/edit/", views.product_update, name="product-update"),
    path("manage/<int:pk>/delete/", views.product_delete, name="product-delete"),
    path("categories/", views.category_list, name="category-list"),
    path("categories/create/", views.category_create, name="category-create"),
    path("categories/<int:pk>/edit/", views.category_update, name="category-update"),
    path("categories/<int:pk>/delete/", views.category_delete, name="category-delete"),
    path("cart/", views.cart_detail, name="cart-detail"),
    path("cart/add/", views.cart_add, name="cart-add"),
    path("cart/clear/", views.cart_clear, name="cart-clear"),
    path("cart/checkout/", views.cart_checkout, name="cart-checkout"),
    path("purchases/", views.purchase_list, name="purchase-list"),
    path("purchases/<int:pk>/delete/", views.purchase_delete, name="purchase-delete"),
]
