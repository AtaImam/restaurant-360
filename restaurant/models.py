import qrcode
from io import BytesIO

from django.db import models
from django.core.files import File
class Restaurant(models.Model):
    name = models.CharField(max_length=150)
    address = models.TextField()
    phone = models.CharField(max_length=20, blank=True)

    def __str__(self):
        return self.name


class Table(models.Model):
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.CASCADE,
        related_name='tables'
    )

    table_number = models.PositiveIntegerField()

    is_active = models.BooleanField(default=True)

    qr_code = models.ImageField(
        upload_to='qr_codes/',
        blank=True,
        null=True
    )

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

        if not self.qr_code:
            menu_url = f"http://172.20.10.5:8000/menu/{self.restaurant.id}/table/{self.id}/"

            qr = qrcode.make(menu_url)

            buffer = BytesIO()
            qr.save(buffer, format="PNG")

            file_name = f"table_{self.id}_qr.png"

            self.qr_code.save(
                file_name,
                File(buffer),
                save=False
            )

            super().save(update_fields=["qr_code"])

    def __str__(self):
        return f"{self.restaurant.name} - Table {self.table_number}"