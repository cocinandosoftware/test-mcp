from django.shortcuts import render


def home(request):
    """Render the main landing page with the prompt form."""

    return render(request, "home/index.html")
