"""Admin UI — how admins inspect and control individual environments.

Destroy/extend actions go through the ControlPlane (as the system principal) so
they run the real teardown + ingress cleanup, not just a DB edit.
"""

from datetime import timedelta

from django.contrib import admin
from django.utils import timezone

from cawl_core.control import NotFound

from .models import (
    ApiToken, Exposure, Environment, EnvironmentEvent, EnvironmentGrant, Template,
    TemplateVersion,
)
from .services import build_control


class TemplateVersionInline(admin.TabularInline):
    model = TemplateVersion
    extra = 0
    can_delete = False
    readonly_fields = ("version", "params", "created_at", "created_by", "raw_yaml")
    ordering = ("-version",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Template)
class TemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "params", "version", "active", "updated_at")
    list_filter = ("active",)
    search_fields = ("name", "params")
    readonly_fields = ("version", "params", "created_at", "updated_at")
    inlines = [TemplateVersionInline]


class EnvironmentGrantInline(admin.TabularInline):
    """Who else may use this environment. Editable: adding a row here *is* granting
    access — the daemon reads this table when it decides to sign a certificate,
    so there's nothing to push to the box."""

    model = EnvironmentGrant
    extra = 0
    fields = ("principal", "granted_by", "granted_at")
    readonly_fields = ("granted_at",)


class ExposureInline(admin.TabularInline):
    """Ports exported to the web. Editing ``access`` here *is* changing who may
    view it — forward-auth reads this table on every request. Route files are
    only re-rendered by the ControlPlane, though, so add/remove exposures via
    the API (`cawl expose`), not here."""

    model = Exposure
    extra = 0
    fields = ("name", "port", "access", "created_by", "created_at")
    readonly_fields = ("name", "port", "created_by", "created_at")

    def has_add_permission(self, request, obj=None):
        return False


class EnvironmentEventInline(admin.TabularInline):
    model = EnvironmentEvent
    extra = 0
    can_delete = False
    readonly_fields = ("at", "kind", "actor", "from_status", "to_status", "detail")
    ordering = ("-at",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Environment)
class EnvironmentAdmin(admin.ModelAdmin):
    list_display = ("id", "template", "backend", "owner", "status", "args",
                    "created_at", "expires_at")
    list_filter = ("status", "template")
    search_fields = ("id", "owner", "args")
    readonly_fields = ("id", "template", "template_version", "created_at",
                       "destroyed_at", "vm_ip", "url")
    inlines = [EnvironmentGrantInline, ExposureInline, EnvironmentEventInline]
    actions = ["destroy_selected", "extend_24h"]

    @admin.action(description="Destroy selected environments (real teardown)")
    def destroy_selected(self, request, queryset):
        control = build_control()
        n = 0
        for sb in queryset:
            try:
                control.destroy(sb.id, _system())
                n += 1
            except NotFound:
                pass
        self.message_user(request, f"destroyed {n}")

    @admin.action(description="Extend TTL by 24h")
    def extend_24h(self, request, queryset):
        for sb in queryset.exclude(expires_at__isnull=True):
            sb.expires_at += timedelta(hours=24)
            sb.save(update_fields=["expires_at"])
            EnvironmentEvent.objects.create(
                environment=sb, kind="status", detail="ttl extended +24h",
                actor=request.user.get_username())
        self.message_user(request, "extended")


@admin.register(EnvironmentEvent)
class EnvironmentEventAdmin(admin.ModelAdmin):
    list_display = ("at", "environment", "kind", "from_status", "to_status", "actor")
    list_filter = ("kind",)
    search_fields = ("environment__id", "actor")


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):
    list_display = ("name", "subject", "role", "quota", "max_ttl", "backend", "prefix",
                    "created_at", "expires_at", "revoked_at", "last_used_at")
    list_filter = ("role",)
    search_fields = ("name", "subject", "prefix")
    readonly_fields = ("key_hash", "prefix", "created_at", "last_used_at")
    actions = ["revoke"]

    @admin.action(description="Revoke selected tokens")
    def revoke(self, request, queryset):
        n = queryset.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
        self.message_user(request, f"revoked {n}")


def _system():
    from cawl_core.auth import SYSTEM
    return SYSTEM
