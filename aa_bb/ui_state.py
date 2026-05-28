# Standard Library
from functools import wraps

# Django
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import render

# AA BigBrother
from .models import BigBrotherConfig

BIG_BROTHER_INACTIVE_MESSAGE = "Big Brother is currently inactive."
CORP_BROTHER_INACTIVE_MESSAGE = "Corp Brother is currently inactive."


def _is_plugin_active():
    return BigBrotherConfig.get_solo().is_active


def require_active_page(template_name, message):
    """Render a disabled page when the plugin master switch is off."""

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not _is_plugin_active():
                return render(request, template_name, {"message": message})
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator


def require_active_json(message):
    """Reject JSON endpoints when the plugin master switch is off."""

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not _is_plugin_active():
                return JsonResponse({"error": message, "inactive": True}, status=403)
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator


def require_active_http(message):
    """Reject non-HTML UI endpoints when the plugin master switch is off."""

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if not _is_plugin_active():
                return HttpResponseForbidden(message)
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator
