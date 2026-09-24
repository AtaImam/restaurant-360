from django.db import migrations


def freeze_existing_receipts(apps, schema_editor):
    Order = apps.get_model('orders', 'Order')
    db = schema_editor.connection.alias
    for order in Order.objects.using(db).select_related('restaurant', 'branch').iterator(chunk_size=500):
        if order.settings_snapshot:
            continue
        # Historical monetary amounts are authoritative. Rates were never stored.
        Order.objects.using(db).filter(pk=order.pk).update(settings_snapshot={
            'version': 1, 'currency': 'BDT', 'vat_percent': None, 'service_percent': None,
            'tax_policy': 'legacy_unknown', 'timezone': 'UTC',
            'receipt_name': order.restaurant.name,
            'branch_name': order.branch.name if order.branch_id else '',
            'receipt_logo_url': '', 'receipt_contact': '', 'receipt_heading': 'Thank you! 🙏',
            'receipt_footer': 'We hope to see you again soon.', 'receipt_show_branch': False,
            'tax_registration': '',
        })


class Migration(migrations.Migration):
    dependencies = [('orders', '0011_order_settings_snapshot')]
    operations = [migrations.RunPython(freeze_existing_receipts, migrations.RunPython.noop)]
