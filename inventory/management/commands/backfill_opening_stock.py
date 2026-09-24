from django.core.management.base import BaseCommand
from inventory.services import backfill_opening_stock_transactions


class Command(BaseCommand):
    help = (
        "Safely and idempotently creates OPENING_BALANCE stock transactions "
        "for existing ingredients with current_stock > 0 and no historical "
        "opening or purchase stock transaction. Does not alter current_stock."
    )

    def handle(self, *args, **options):
        created_txns = backfill_opening_stock_transactions()
        count = len(created_txns)
        if count == 0:
            self.stdout.write(
                self.style.SUCCESS(
                    "No ingredients needed opening balance backfill. Database is up to date."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully backfilled {count} opening-balance stock transactions."
                )
            )
