"""Security policy for the control origin, including static and error responses."""

from flask import request

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'; frame-src 'none'; worker-src 'none'"
)


def control_response(response):
    response.headers.update(
        {
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
            "Content-Security-Policy": CSP,
        }
    )
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    return response
