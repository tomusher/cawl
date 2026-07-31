"""Destroy expired environments. Run on a systemd timer (see deploy/)."""

from django.core.management.base import BaseCommand

from environments.services import build_control


class Command(BaseCommand):
    help = "Destroy all expired environment environments."

    def handle(self, *args, **options):
        reaped = build_control().reap()  # acts as the system (admin) principal
        self.stdout.write(self.style.SUCCESS(
            f"reaped {len(reaped)}: {' '.join(reaped)}".strip()))
