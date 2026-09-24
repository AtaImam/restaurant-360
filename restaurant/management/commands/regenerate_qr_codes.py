"""Refresh table QR images, optionally resetting all stale storage."""
import re
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from restaurant.models import Table
from restaurant.qr import qr_base_url


GENERATED_QR = re.compile(r"table_\d+(?:_[A-Za-z0-9]+)*\.png\Z")


def stale_candidates(storage):
    """Only recognize generated table images, never arbitrary media."""
    names = []
    for directory in ("qr_codes", "qr_codes/current"):
        try:
            _, files = storage.listdir(directory)
        except FileNotFoundError:
            continue
        names.extend(f"{directory}/{name}" for name in files if GENERATED_QR.fullmatch(name))
    return names


class Command(BaseCommand):
    help = "Refresh QR codes from SITE_URL; --reset also removes unreferenced generated images."

    def add_arguments(self, parser):
        parser.add_argument("--restaurant-id", type=int)
        parser.add_argument("--branch-id", type=int)
        parser.add_argument("--table-id", type=int)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--reset", action="store_true", help="Rebuild all tables and prune stale generated QR files, including the legacy directory.")

    def handle(self, *args, **options):
        try:
            qr_base_url()
        except ImproperlyConfigured as error:
            raise CommandError(str(error)) from error
        if options["reset"] and any(options.get(field) is not None for field in ("restaurant_id", "branch_id", "table_id")):
            raise CommandError("--reset operates on all tables; omit table/branch/restaurant filters.")
        tables = Table.objects.all().order_by("pk")
        for field in ("restaurant_id", "branch_id", "table_id"):
            if options.get(field) is not None:
                tables = tables.filter(**{"pk" if field == "table_id" else field: options[field]})
        storage = Table._meta.get_field("qr_code").storage
        candidates = stale_candidates(storage) if options["reset"] else []
        legacy_dir = Path(settings.BASE_DIR) / "qr_codes"
        legacy = [p for p in legacy_dir.glob("table_*.png") if GENERATED_QR.fullmatch(p.name) and p.is_file() and not p.is_symlink()] if options["reset"] else []
        if options["dry_run"]:
            for table in tables:
                self.stdout.write(f"Table #{table.pk}: {table.get_menu_url()}")
            self.stdout.write(f"Preview only; {len(candidates) + len(legacy)} generated files will be checked for stale references.")
            return
        # Lock existing rows through publication and cleanup. No old file is removed
        # until every table's new reference has been successfully committed.
        with transaction.atomic():
            locked_tables = list(tables.select_for_update())
            for table in locked_tables:
                url = table.generate_qr_code()
                self.stdout.write(f"Table #{table.pk} / restaurant #{table.restaurant_id} / branch #{table.branch_id}: {table.qr_code.name} -> {url}")
            def prune():
                references = set(Table.objects.values_list("qr_code", flat=True))
                removed = 0
                for name in candidates:
                    if name not in references and storage.exists(name):
                        storage.delete(name)
                        removed += 1
                # Avoid deleting physical files if a custom MEDIA_ROOT makes the
                # historical directory overlap storage's current references.
                referenced_paths = set()
                for name in references:
                    if name:
                        try:
                            referenced_paths.add(Path(storage.path(name)).resolve())
                        except NotImplementedError:
                            pass
                for path in legacy:
                    if path.exists() and path.resolve() not in referenced_paths:
                        path.unlink()
                        removed += 1
                self.stdout.write(f"Removed {removed} stale generated QR files.")
            if options["reset"]:
                transaction.on_commit(prune)
        self.stdout.write(self.style.SUCCESS(f"Verified current QR images for {len(locked_tables)} tables."))
