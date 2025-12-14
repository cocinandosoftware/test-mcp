from django.urls import path

from . import views

app_name = "prompts"

urlpatterns = [
    path("submit/", views.submit_prompt, name="submit"),
]
