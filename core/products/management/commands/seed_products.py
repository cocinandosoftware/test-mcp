from django.core.management.base import BaseCommand
from django.db import transaction

from core.products.models import Category, Product


CATEGORIES_SEED = [
    {
        "name": "Ropa",
        "slug": "ropa",
        "description": "Prendas de vestir para uso diario.",
        "is_active": True,
    },
    {
        "name": "Calzado",
        "slug": "calzado",
        "description": "Calzado urbano y deportivo.",
        "is_active": True,
    },
    {
        "name": "Accesorios",
        "slug": "accesorios",
        "description": "Complementos y accesorios para el dia a dia.",
        "is_active": True,
    },
]


PRODUCTS_SEED = [
    {
        "name": "Camiseta basica algodon",
        "slug": "camiseta-basica-algodon",
        "description": "Camiseta de manga corta 100% algodon organico.",
        "price": "19.90",
        "stock": 120,
        "is_active": True,
    },
    {
        "name": "Pantalon chino beige",
        "slug": "pantalon-chino-beige",
        "description": "Pantalon chino entallado con bolsillos laterales.",
        "price": "39.95",
        "stock": 80,
        "is_active": True,
    },
    {
        "name": "Zapatillas urbanas",
        "slug": "zapatillas-urbanas",
        "description": "Calzado urbano con suela de goma antideslizante.",
        "price": "59.90",
        "stock": 45,
        "is_active": True,
    },
    {
        "name": "Mochila impermeable",
        "slug": "mochila-impermeable",
        "description": "Mochila de 20L con compartimento para portatil.",
        "price": "49.50",
        "stock": 30,
        "is_active": True,
    },
]

PRODUCT_CATEGORY_MAP = {
    "camiseta-basica-algodon": ["ropa"],
    "pantalon-chino-beige": ["ropa"],
    "zapatillas-urbanas": ["calzado"],
    "mochila-impermeable": ["accesorios"],
}


class Command(BaseCommand):
    help = "Carga datos de ejemplo para productos"

    def handle(self, *args, **options):
        categories_created = 0
        products_created = 0
        category_lookup = {}

        for category_data in CATEGORIES_SEED:
            category, was_created = Category.objects.update_or_create(
                slug=category_data["slug"], defaults=category_data
            )
            category_lookup[category.slug] = category
            categories_created += int(was_created)

        with transaction.atomic():
            for product_data in PRODUCTS_SEED:
                product, was_created = Product.objects.update_or_create(
                    slug=product_data["slug"], defaults=product_data
                )
                products_created += int(was_created)
                category_slugs = PRODUCT_CATEGORY_MAP.get(product.slug, [])
                selected = [category_lookup[slug] for slug in category_slugs if slug in category_lookup]
                if selected:
                    product.categories.set(selected)
                else:
                    product.categories.clear()
        self.stdout.write(
            self.style.SUCCESS(
                "Datos de ejemplo cargados. Nuevas categorias: "
                f"{categories_created}. Nuevos productos: {products_created}."
            )
        )
