from django.contrib import admin

from .models import Category, Product


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "updated_at")
    list_filter = ("is_active", "updated_at")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name", "description")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "price", "stock", "is_active", "updated_at")
    list_filter = ("is_active", "updated_at")
    prepopulated_fields = {"slug": ("name",)}
    filter_horizontal = ("categories",)
    search_fields = ("name", "description")
