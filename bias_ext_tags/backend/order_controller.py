from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from ninja import Body

from bias_core.extensions.platform import api_error
from bias_core.extensions.platform import require_staff
from bias_core.extensions.platform import resolve_authenticated_user
from bias_ext_tags.backend.services import TagService


def order_tags_api_route(request, payload: dict = Body(...)):
    user = resolve_authenticated_user(request)
    if user is not None and getattr(user, "is_authenticated", False):
        request.auth = user

    denied = require_staff(request)
    if denied:
        return denied
    if not isinstance(payload, dict) or "order" not in payload:
        return HttpResponse(status=422)

    try:
        TagService.order_tags(payload.get("order"), request.auth)
        return HttpResponse(status=204)
    except PermissionDenied as exc:
        return api_error(str(exc), status=403)
    except ValueError as exc:
        return api_error(str(exc), status=400)
