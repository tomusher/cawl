"""Browser-facing CLI login (the daemon side of `cawl login`).

The CLI opens /cli/login?callback=<loopback>&state=<nonce>. We require a logged-in
user (session/OIDC), show a confirm page, then mint an ApiToken for them and
redirect to the loopback callback with the token. Callbacks are restricted to
localhost so a token can only ever be handed to the user's own machine.
"""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode, urlparse

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from .auth import role_for_user
from .models import ApiToken


def _is_loopback(url: str) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return False
    return u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost") \
        and u.port is not None


@login_required
@require_http_methods(["GET", "POST"])
def cli_login(request):
    callback = request.GET.get("callback") or request.POST.get("callback", "")
    state = request.GET.get("state") or request.POST.get("state", "")
    # No callback => headless mode: display the token for copy-paste instead of
    # redirecting to a loopback the (possibly remote) browser can't reach.
    headless = not callback
    if callback and not _is_loopback(callback):
        return HttpResponseBadRequest("invalid or non-loopback callback")

    if request.method == "GET":
        # Confirm page (CSRF-protected POST authorizes the mint).
        return render(request, "cli_login.html", {
            "callback": callback, "state": state, "headless": headless,
            "user": request.user})

    role = role_for_user(request.user).value
    _, raw = ApiToken.mint(
        name=f"cli-login:{request.user.get_username()}",
        subject=request.user.get_username(), role=role, ttl=timedelta(days=90),
        created_by=request.user,
    )
    if headless:
        return render(request, "cli_login_code.html", {"token": raw})
    sep = "&" if "?" in callback else "?"
    return HttpResponseRedirect(callback + sep + urlencode({"token": raw, "state": state}))
