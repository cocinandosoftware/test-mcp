# Proyecto Django "Tienda Online"

Este proyecto demuestra una base de tienda en línea construida con Django 6 y SQLite. Incluye gestión completa de productos y categorías, un formulario de consultas que interactúa con la API de Groq y un endpoint JSON para consumir el catálogo desde integraciones externas.

## Requisitos previos

- Python 3.12 o superior instalado en el sistema.
- (Opcional) Herramientas de virtualización de entornos como `venv`.

## Puesta en marcha

1. Crear y activar el entorno virtual:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
2. Instalar las dependencias:
   ```bash
   pip install -r requirements.txt
   ```
3. Aplicar migraciones:
   ```bash
   python manage.py migrate
   ```
4. Cargar datos de ejemplo (opcional):
   ```bash
   python manage.py seed_products
   ```
5. Ejecutar el servidor de desarrollo:
   ```bash
   python manage.py runserver
   ```
6. Abrir el navegador en `http://127.0.0.1:8000/` para probar el formulario de prompts y realizar preguntas al asistente.
7. Consultar la API de productos en `http://127.0.0.1:8000/products/` para obtener el listado JSON de productos activos.
8. Gestionar productos en `http://127.0.0.1:8000/products/manage/` y categorías en `http://127.0.0.1:8000/products/categories/`.

## Ejecutar verificaciones

- Verificar la configuración del proyecto:
  ```bash
  python manage.py check
  ```

## Notas adicionales

- Si el puerto 8000 está ocupado, puedes arrancar el servidor en otro puerto:
  ```bash
  python manage.py runserver 0.0.0.0:8001
  ```
- El entorno virtual se encuentra en la carpeta `.venv`.
- El módulo `core.products` concentra los modelos y registros de administración de productos, mientras que `context.products` expone las vistas y URLs públicas.
- Las plantillas HTML globales residen en la carpeta `templates/`.
- El módulo `context.prompts` gestiona la API a la que el formulario de la pagina inicial envia los mensajes.
- Para habilitar el servicio LLM de Groq, crea un archivo `.env` (ya se incluye un ejemplo) con `GROQ_API_KEY` y, de manera opcional, `GROQ_MODEL` (por defecto `meta-llama/llama-4-scout-17b-16e-instruct`); el proyecto los carga automaticamente con `python-dotenv`.
- Desde `products/manage/` puedes gestionar un carrito de compras: añade productos con el botón "Añadir al carrito", revisa el panel desplegable, confirma la compra y consulta el histórico guardado en la base de datos.

### Comandos JSON en el prompt

Además de preguntas al asistente, el campo de texto de la página principal acepta comandos JSON para gestionar productos y categorías. Escribe `help` para obtener la lista completa. Algunos ejemplos:

```json
{"action": "list_categories"}
{"action": "create_category", "data": {"name": "Snacks"}}
{"action": "assign_category", "data": {"product_id": 1, "category_id": 2}}
{"action": "create_product", "data": {"name": "Cafe Colombiano", "price": "12.50", "stock": 20, "categories": ["bebidas"]}}
{"action": "update_product", "data": {"product_id": 1, "price": "18.90", "categories": [1, 3]}}
{"action": "delete_product", "data": {"product_id": 4}}
```

Los identificadores pueden ser `id`, `slug` o `name` (en el caso de categorías y productos, cuando no existan duplicados). Para actualizar asignaciones, el campo `categories` acepta una lista de identificadores o una cadena separada por comas.

También puedes escribir instrucciones en lenguaje natural (por ejemplo, “crea la categoría moda y asígnala a todos los productos”). El servidor utiliza Groq para traducir la petición a comandos JSON y ejecutarlos.
