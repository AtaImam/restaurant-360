from decimal import Decimal
from io import BytesIO

import qrcode
from django.conf import settings
from django.core.files import File
from django.db import models, transaction


class Restaurant(models.Model):
    name = models.CharField(max_length=150)
    address = models.TextField()
    phone = models.CharField(max_length=20, blank=True)

    def __str__(self):
        return self.name


class Branch(models.Model):
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="branches",
    )
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=20)
    address = models.TextField(blank=True, default="")
    phone = models.CharField(max_length=30, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    is_main = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_main", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "name"],
                name="unique_branch_name_per_restaurant",
            ),
            models.UniqueConstraint(
                fields=["restaurant", "code"],
                name="unique_branch_code_per_restaurant",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.is_main and self.restaurant_id:
            Branch.objects.filter(restaurant_id=self.restaurant_id, is_main=True).exclude(pk=self.pk).update(is_main=False)
        is_new = self.pk is None
        super().save(*args, **kwargs)
        if is_new and self.restaurant_id:
            from inventory.models import BranchIngredientStock, Ingredient
            for ing in Ingredient.objects.filter(restaurant_id=self.restaurant_id):
                BranchIngredientStock.objects.get_or_create(
                    branch=self,
                    ingredient=ing,
                    defaults={
                        "current_stock": Decimal("0.000"),
                        "reserved_stock": Decimal("0.000"),
                        "min_stock_alert": ing.minimum_level or Decimal("0.000"),
                    },
                )

    def __str__(self):
        main_badge = " (Main)" if self.is_main else ""
        return f"{self.restaurant.name} - {self.name}{main_badge}"


class Floor(models.Model):
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="floors",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="floors",
    )
    name = models.CharField(max_length=100)
    floor_number = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["floor_number", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "name"],
                name="unique_floor_name_per_branch",
            ),
            models.UniqueConstraint(
                fields=["branch", "floor_number"],
                name="unique_floor_number_per_branch",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.branch_id and self.restaurant_id:
            main_b = self.restaurant.branches.filter(is_main=True).first() or self.restaurant.branches.first()
            if main_b:
                self.branch = main_b
        super().save(*args, **kwargs)

    def __str__(self):
        branch_str = f" [{self.branch.name}]" if self.branch else ""
        return f"{self.restaurant.name}{branch_str} - {self.name}"


class Table(models.Model):
    STATUS_AVAILABLE = "AVAILABLE"
    STATUS_OCCUPIED = "OCCUPIED"
    STATUS_RESERVED = "RESERVED"
    STATUS_WAITING = "WAITING"
    STATUS_SERVING = "SERVING"
    STATUS_CLEANING = "CLEANING"
    STATUS_OUT_OF_SERVICE = "OUT_OF_SERVICE"

    STATUS_CHOICES = [
        (STATUS_AVAILABLE, "Available"),
        (STATUS_OCCUPIED, "Occupied"),
        (STATUS_RESERVED, "Reserved"),
        (STATUS_WAITING, "Waiting"),
        (STATUS_SERVING, "Serving"),
        (STATUS_CLEANING, "Cleaning"),
        (STATUS_OUT_OF_SERVICE, "Out of Service"),
    ]

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="tables",
    )

    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="tables",
    )

    floor = models.ForeignKey(
        Floor,
        on_delete=models.PROTECT,
        related_name="tables",
        null=True,
        blank=True,
    )

    table_number = models.PositiveIntegerField()

    capacity = models.PositiveSmallIntegerField(default=4)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_AVAILABLE,
    )

    is_active = models.BooleanField(default=True)

    qr_code = models.ImageField(
        upload_to="qr_codes/",
        blank=True,
        null=True,
    )

    class Meta:
        ordering = ["floor__floor_number", "table_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "table_number"],
                name="unique_table_number_per_branch",
            ),
        ]

    def get_menu_url(self):
        from restaurant.qr import table_menu_url
        return table_menu_url(self)

    @property
    def safe_menu_url(self):
        try:
            return self.get_menu_url()
        except Exception:
            return ""

    def generate_qr_code(self):
        """Ensure a current, content-verified image; repeated calls reuse it."""
        from hashlib import sha256

        menu_url = self.get_menu_url()
        buffer = BytesIO()
        qrcode.make(menu_url).save(buffer, format="PNG")
        content = buffer.getvalue()
        stem = f"qr_codes/current/table_{self.pk}_{sha256(content).hexdigest()}"
        storage = self.qr_code.storage
        new_name = None
        try:
            with transaction.atomic():
                old_name = type(self).objects.select_for_update().get(pk=self.pk).qr_code.name
                if old_name and old_name.startswith(stem) and storage.exists(old_name):
                    with storage.open(old_name, "rb") as existing:
                        if existing.read() == content:
                            self.qr_code.name = old_name
                            return menu_url
                canonical_name = stem + ".png"
                reusable = False
                if storage.exists(canonical_name):
                    with storage.open(canonical_name, "rb") as existing:
                        reusable = existing.read() == content
                if reusable:
                    self.qr_code.name = canonical_name
                else:
                    buffer.seek(0)
                    # Storage saves a unique alternative if a corrupt target exists.
                    new_name = storage.save(canonical_name, File(buffer))
                    self.qr_code.name = new_name
                super().save(update_fields=["qr_code"])
                if old_name and old_name != self.qr_code.name:
                    def retire_old_image():
                        if not type(self).objects.filter(qr_code=old_name).exists():
                            storage.delete(old_name)
                    transaction.on_commit(retire_old_image)
        except Exception:
            if new_name:
                storage.delete(new_name)
            raise
        return menu_url

    @property
    def is_occupied(self):
        """Check if this table currently has seated guests or active service."""
        return self.status in {
            self.STATUS_OCCUPIED,
            self.STATUS_WAITING,
            self.STATUS_SERVING,
        }

    @property
    def active_session(self):
        """Return the currently open TableSession for this table, if any."""
        from orders.models import TableSession
        from orders.services import get_live_order_cutoff
        live_cutoff = get_live_order_cutoff()
        return self.sessions.filter(
            status=TableSession.STATUS_OPEN,
            opened_at__gte=live_cutoff,
        ).order_by("-opened_at").first()

    def get_or_create_active_session(self):
        """Retrieve the current open TableSession or create one."""
        from orders.models import TableSession
        session = self.active_session
        if not session:
            session = TableSession.objects.create(
                restaurant=self.restaurant,
                branch=self.branch,
                table=self,
                status=TableSession.STATUS_OPEN,
            )
        return session

    def mark_occupied(self, commit=True):
        """Transition an available table to occupied when a dine-in service begins."""
        if self.status == self.STATUS_AVAILABLE:
            self.status = self.STATUS_OCCUPIED
            if commit:
                self.save(update_fields=["status"])
        return self.get_or_create_active_session()

    def close_service(self, commit=True):
        """Close all open table sessions and make table available again."""
        self.status = self.STATUS_AVAILABLE
        if commit:
            self.save(update_fields=["status"])
        from django.utils import timezone
        from orders.models import TableSession
        self.sessions.filter(status=TableSession.STATUS_OPEN).update(
            status=TableSession.STATUS_CLOSED,
            closed_at=timezone.now(),
        )

    @property
    def has_unpaid_orders(self):
        """Check if the table's current active session has any unpaid orders."""
        if hasattr(self, "_has_unpaid_orders_override"):
            return self._has_unpaid_orders_override
        session = self.active_session
        if not session:
            return False
        return session.has_unpaid_orders

    @has_unpaid_orders.setter
    def has_unpaid_orders(self, value):
        self._has_unpaid_orders_override = value

    def save(self, *args, **kwargs):
        if not self.branch_id:
            if self.floor_id and self.floor.branch_id:
                self.branch_id = self.floor.branch_id
            elif self.restaurant_id:
                main_b = self.restaurant.branches.filter(is_main=True).first() or self.restaurant.branches.first()
                if main_b:
                    self.branch = main_b
        super().save(*args, **kwargs)

        if not self.qr_code and settings.SITE_URL:
            self.generate_qr_code()

    def __str__(self):
        floor_name = self.floor.name if self.floor else "No Floor"
        return (
            f"{self.restaurant.name} - "
            f"{floor_name} - Table {self.table_number}"
        )