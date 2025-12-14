from django import forms

from core.products.models import Category, Product


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name", "slug", "description", "is_active"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
        }


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = [
            "name",
            "slug",
            "description",
            "price",
            "stock",
            "is_active",
            "categories",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "categories": forms.SelectMultiple(attrs={"size": 6}),
        }