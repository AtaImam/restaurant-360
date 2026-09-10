from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from inventory.models import StockReservation, StockTransaction
from inventory.services import consume_order_reservations
from orders.models import Order


class Command(BaseCommand):
    help = (
        "Inspect historical PREPARING/READY/SERVED/COMPLETED inventory. "
        "Dry-run by default; --apply reconciles safe reservations atomically per order."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Apply validated inventory repairs.")
        parser.add_argument(
            "--order-id", action="append", type=int, dest="order_ids",
            help="Restrict to an order ID; repeat for multiple orders.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        requested = set(options["order_ids"] or [])
        orders = Order.objects.all()
        if requested:
            orders = orders.filter(pk__in=requested)
        else:
            orders = orders.filter(status__in=["PREPARING", "READY", "SERVED", "COMPLETED"])
        order_ids = list(orders.order_by("pk").values_list("pk", flat=True))
        failures = len(requested - set(order_ids))
        for missing_id in sorted(requested - set(order_ids)):
            self.stderr.write(f"Order #{missing_id}: SKIPPED — does not exist.")

        self.stdout.write("APPLY: inventory reconciliation" if apply else "DRY RUN: no inventory changes")
        checked = repaired = unchanged = 0
        for order_id in order_ids:
            try:
                with transaction.atomic():
                    order = Order.objects.select_for_update().get(pk=order_id)
                    if order.status not in {"PREPARING", "READY", "SERVED", "COMPLETED"}:
                        raise ValidationError(f"Status {order.status} is not eligible for historical reconciliation.")
                    active_ids = set(order.stock_reservations.filter(
                        status=StockReservation.Status.ACTIVE,
                    ).values_list("ingredient_id", flat=True))
                    existing_ids = set(order.stock_transactions.filter(
                        transaction_type=StockTransaction.TransactionType.CONSUMPTION,
                        ingredient_id__in=active_ids,
                    ).values_list("ingredient_id", flat=True))
                    count = consume_order_reservations(order, dry_run=not apply)
                    checked += 1
                    if count:
                        repaired += 1
                        verb = "reconciled" if apply else "would reconcile"
                        self.stdout.write(
                            f"Order #{order_id} ({order.status}): {verb} {count} reservation(s); "
                            f"{count - len(existing_ids)} new deduction(s), "
                            f"{len(existing_ids)} existing ledger match(es)."
                        )
                    else:
                        unchanged += 1
                        self.stdout.write(f"Order #{order_id} ({order.status}): already consistent; unchanged.")
            except (ValidationError, Order.DoesNotExist) as error:
                failures += 1
                details = "; ".join(error.messages) if isinstance(error, ValidationError) else str(error)
                self.stderr.write(f"Order #{order_id}: SKIPPED — {details}")

        self.stdout.write(
            f"Summary: {checked} validated, {repaired} "
            f"{'reconciled' if apply else 'repairable'}, {unchanged} unchanged, {failures} skipped."
        )
        if failures:
            raise CommandError(
                "Some orders require manual review; skipped orders were not modified. "
                "Other validated orders may have been reconciled when --apply was used."
            )
