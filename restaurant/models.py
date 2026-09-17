from io import BytesIO

import qrcode
from django.core.files import File
from django.db import models


class Restaurant(models.Model):
    name = models.CharField(max_length=150)
    address = models.TextField()
    phone = models.CharField(max_length=20, blank=True)

    def __str__(self):
        return self.name


class Floor(models.Model):
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name="floors",
    )
    name = models.CharField(max_length=100)
    floor_number = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["floor_number", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "name"],
                name="unique_floor_name_per_restaurant",
            ),
            models.UniqueConstraint(
                fields=["restaurant", "floor_number"],
                name="unique_floor_number_per_restaurant",
            ),
        ]

    def __str__(self):
        return f"{self.restaurant.name} - {self.name}"


class Table(models.Model):
    STATUS_AVAILABLE = "AVAILABLE"
    STATUS_RESERVED = "RESERVED"
    STATUS_WAITING = "WAITING"
    STATUS_SERVING = "SERVING"
    STATUS_CLEANING = "CLEANING"
    STATUS_OUT_OF_SERVICE = "OUT_OF_SERVICE"

    STATUS_CHOICES = [
        (STATUS_AVAILABLE, "Available"),
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
                fields=["restaurant", "table_number"],
                name="unique_table_number_per_restaurant",
            ),
        ]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

        if not self.qr_code:
            menu_url = (
                f"http://172.20.10.5:8000/menu/"
                f"{self.restaurant_id}/table/{self.id}/"
            )

            qr = qrcode.make(menu_url)

            buffer = BytesIO()
            qr.save(buffer, format="PNG")
            buffer.seek(0)

            file_name = f"table_{self.id}_qr.png"

            self.qr_code.save(
                file_name,
                File(buffer),
                save=False,
            )

            super().save(update_fields=["qr_code"])

    def __str__(self):
        floor_name = self.floor.name if self.floor else "No Floor"
        return (
            f"{self.restaurant.name} - "
            f"{floor_name} - Table {self.table_number}"
        )