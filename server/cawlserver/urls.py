from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from environments.api import api
from environments.cli_auth import cli_login
from environments import webauth

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", api.urls),
    path("cli/login", cli_login, name="cli-login"),
    # Browser access to exposures: Traefik's forward-auth subrequest, and the
    # login pages served on the auth host.
    path("auth/forward", webauth.forward_auth, name="forward-auth"),
    path("auth/sso", webauth.sso, name="web-sso"),
    path("auth/login", webauth.login_submit, name="web-login"),
    path("auth/magic", webauth.magic, name="web-magic"),
    # Reserved path on every *exposed* host (Traefik routes /.cawl/* here, not
    # to the VM): the code-for-cookie handoff.
    path(".cawl/callback", webauth.callback, name="web-callback"),
    path(".cawl/logout", webauth.logout_view, name="web-logout"),
]

if settings.OIDC_ENABLED:
    urlpatterns.append(path("oidc/", include("mozilla_django_oidc.urls")))
