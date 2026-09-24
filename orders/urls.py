from django.urls import path

from . import views

urlpatterns = [
    # Bill preview — accessible to customers (QR) and POS staff.
    path(
        "<int:order_id>/bill/",
        views.bill_preview,
        name="bill_preview",
    ),

    # JSON payment action — POSTed by JS on the POS and bill-preview page.
    path(
        "<int:order_id>/pay/",
        views.pay_order,
        name="pay_order",
    ),

    # Print-friendly receipt shown after successful payment.
    path(
        "<int:order_id>/receipt/",
        views.order_receipt,
        name="order_receipt",
    ),
]
