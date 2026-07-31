"""Regenerate Traefik route files for every live environment."""

from django.core.management.base import BaseCommand

from environments.services import build_ingress
from environments.store import DjangoStateStore


class Command(BaseCommand):
    help = "regenerate Traefik ingress configuration for all live environments"

    def handle(self, *args, **options):
        ingress = build_ingress()
        instances = DjangoStateStore().list()
        for instance in instances:
            ingress.sync(instance)
        self.stdout.write(f"synced ingress for {len(instances)} environment(s)")
