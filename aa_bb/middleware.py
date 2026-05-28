"""Middleware to capture request context for admin log attribution."""

from .request_context import clear_request_context, set_request_context


class AdminLogContextMiddleware:
    """Store the current request's actor and basic metadata for audit logging."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and not getattr(user, "is_authenticated", False):
            user = None

        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        ip_addr = (forwarded.split(",")[0].strip() if forwarded else "") or request.META.get("REMOTE_ADDR", "")
        meta = {
            "path": getattr(request, "path", ""),
            "method": getattr(request, "method", ""),
            "ip": ip_addr,
        }
        if user is not None:
            meta["actor_id"] = getattr(user, "pk", None)
            meta["actor_username"] = getattr(user, "get_username", lambda: None)()
            meta["actor_is_staff"] = getattr(user, "is_staff", False)
            meta["actor_is_superuser"] = getattr(user, "is_superuser", False)
        user_agent = request.META.get("HTTP_USER_AGENT", "")
        if user_agent:
            meta["user_agent"] = user_agent[:512]

        set_request_context(user, meta)
        try:
            return self.get_response(request)
        finally:
            clear_request_context()
