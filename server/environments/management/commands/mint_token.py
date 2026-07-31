"""Mint an API token for a programmatic caller (agent). Prints it once."""

from django.core.management.base import BaseCommand, CommandError

from cawl_core.naming import parse_ttl

from environments.models import ApiToken


class Command(BaseCommand):
    help = "Mint an API token. The plaintext is shown once and not recoverable."

    def add_arguments(self, parser):
        parser.add_argument("subject", help="principal id (owner) for envs")
        parser.add_argument("--name", default="", help="human label")
        parser.add_argument("--role", choices=["user", "admin"], default="user")
        parser.add_argument("--quota", type=int, default=None, help="max concurrent envs")
        parser.add_argument("--ttl", default=None, help="expiry, e.g. 90d (default: never)")
        parser.add_argument("--max-ttl", default="", dest="max_ttl",
                            help="cap the lifetime of environments this token "
                                 "creates, e.g. 4h for an agent (default: none)")
        parser.add_argument("--backend", default="",
                            help="force every environment this token creates onto "
                                 "this named backend — e.g. your VM-backed one "
                                 "for agents running untrusted code")

    def handle(self, *args, **o):
        try:
            ttl = parse_ttl(o["ttl"])
            parse_ttl(o["max_ttl"] or None)   # validate the spec now, not at up-time
        except ValueError as e:
            raise CommandError(str(e))
        tok, raw = ApiToken.mint(
            name=o["name"] or o["subject"], subject=o["subject"],
            role=o["role"], quota=o["quota"], ttl=ttl, max_ttl=o["max_ttl"],
            backend=o["backend"],
        )
        self.stdout.write(self.style.SUCCESS(f"token for {tok.subject} (role={tok.role}):"))
        self.stdout.write(raw)
        self.stdout.write(self.style.WARNING("store it now — it will not be shown again"))
