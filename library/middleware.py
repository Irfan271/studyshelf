from django.shortcuts import redirect

class SecurityHeadersMiddleware:
    def __init__(self, get_response): self.get_response = get_response
    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("X-Content-Type-Options", "nosniff")
        response.setdefault("Referrer-Policy", "same-origin")
        response.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' https://cdn.jsdelivr.net 'sha256-HcMhk11w+QF3oNTvnJ5amH4QpXxr8UFO+A9tbCLrN2Q='; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        return response

class StaffMFAMiddleware:
    EXEMPT = ("/accounts/login/", "/accounts/logout/", "/staff/mfa/", "/staff/password/change/")
    def __init__(self, get_response): self.get_response = get_response
    def __call__(self, request):
        user = getattr(request, "user", None)
        if user and user.is_authenticated and user.is_staff and not request.path.startswith(self.EXEMPT):
            if getattr(user, "must_change_password", False):
                return redirect("staff_password_change")
            if not user.mfa_enabled or not request.session.get("staff_mfa_verified"):
                return redirect("staff_mfa_setup" if not user.mfa_enabled else "staff_mfa_verify")
        return self.get_response(request)
