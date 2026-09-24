from io import StringIO
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from orders.models import Order
from orders.test_payment import PaymentFlowFixture
from restaurant.models import Branch, Restaurant, Table
from restaurant.qr import qr_base_url


class QRBaseURLTests(SimpleTestCase):
    def test_configured_origins(self):
        for base in ('https://orders.example.com', 'http://restaurant-demo.local:8000', 'http://192.168.1.20:8080'):
            with self.subTest(base=base), override_settings(SITE_URL=base + '/'):
                self.assertEqual(qr_base_url(), base)

    def test_no_inferred_or_unreachable_origin(self):
        for base in ('', 'http://localhost:8000', 'http://127.0.0.1:8000', 'http://[::1]:8000',
                     'http://0.0.0.0:8000', 'ftp://orders.example.com', 'orders.example.com',
                     'https://user:secret@orders.example.com', 'https://orders.example.com/menu',
                     'https://orders.example.com?x=1', 'https://orders.example.com/#x', 'http://orders.example.com:99999'):
            with self.subTest(base=base), override_settings(SITE_URL=base):
                with self.assertRaises(ImproperlyConfigured):
                    qr_base_url()


@override_settings(SITE_URL='https://orders.example.test', ALLOWED_HOSTS=['orders.example.test', 'testserver'])
class QRGenerationTests(TestCase):
    def setUp(self):
        self.media = TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        settings = override_settings(MEDIA_ROOT=self.media.name, BASE_DIR=self.media.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.restaurant = Restaurant.objects.create(name='QR Restaurant')
        self.branch = Branch.objects.create(restaurant=self.restaurant, name='Main', code='MAIN', is_main=True)
        self.table = Table.objects.create(restaurant=self.restaurant, branch=self.branch, table_number=1)

    def test_image_encodes_named_table_route(self):
        import qrcode
        with patch('restaurant.models.qrcode.make', wraps=qrcode.make) as make:
            url = self.table.generate_qr_code()
        expected = 'https://orders.example.test' + reverse('customer_menu', args=[self.restaurant.pk, self.table.pk])
        self.assertEqual(url, expected)
        make.assert_called_once_with(expected)
        with self.table.qr_code.open('rb') as image:
            self.assertEqual(image.read(8), b'\x89PNG\r\n\x1a\n')

    def test_regeneration_replaces_image_after_commit_only(self):
        old_name = self.table.qr_code.name
        storage = self.table.qr_code.storage
        with self.captureOnCommitCallbacks(execute=True), override_settings(SITE_URL='https://new.example.test/'):
            self.table.generate_qr_code()
            self.assertTrue(storage.exists(old_name))
        self.table.refresh_from_db()
        self.assertNotEqual(old_name, self.table.qr_code.name)
        self.assertTrue(storage.exists(self.table.qr_code.name))
        self.assertFalse(storage.exists(old_name))
        self.assertEqual(self.table.status, Table.STATUS_AVAILABLE)
        self.assertEqual(self.table.branch_id, self.branch.pk)

    def test_database_failure_preserves_old_image_and_removes_new_image(self):
        from django.db.models import Model
        old_name = self.table.qr_code.name
        storage = self.table.qr_code.storage
        before = storage.listdir('qr_codes/current')[1]
        with override_settings(SITE_URL='https://new.example.test'), patch.object(Model, 'save', side_effect=RuntimeError('database write failed')):
            with self.assertRaises(RuntimeError):
                self.table.generate_qr_code()
        self.table.refresh_from_db()
        self.assertEqual(self.table.qr_code.name, old_name)
        self.assertTrue(storage.exists(old_name))
        self.assertEqual(storage.listdir('qr_codes/current')[1], before)

    def test_invalid_configuration_preserves_existing_qr(self):
        old_name = self.table.qr_code.name
        with override_settings(SITE_URL='http://localhost:8000'), self.assertRaises(ImproperlyConfigured):
            self.table.generate_qr_code()
        self.table.refresh_from_db()
        self.assertEqual(self.table.qr_code.name, old_name)
        self.assertTrue(self.table.qr_code.storage.exists(old_name))

    def test_regeneration_preview_and_branch_scope(self):
        other_branch = Branch.objects.create(restaurant=self.restaurant, name='Other', code='OTHER')
        other = Table.objects.create(restaurant=self.restaurant, branch=other_branch, table_number=1)
        old_name = self.table.qr_code.name
        other_name = other.qr_code.name
        output = StringIO()
        call_command('regenerate_qr_codes', branch_id=self.branch.pk, dry_run=True, stdout=output)
        self.table.refresh_from_db()
        self.assertEqual(old_name, self.table.qr_code.name)
        self.assertIn(self.table.get_menu_url(), output.getvalue())
        self.assertNotIn(other.get_menu_url(), output.getvalue())
        call_command('regenerate_qr_codes', branch_id=self.branch.pk, stdout=StringIO())
        self.table.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(old_name, self.table.qr_code.name)
        self.assertEqual(other_name, other.qr_code.name)
        with override_settings(SITE_URL='http://localhost:8000'), self.assertRaises(CommandError):
            call_command('regenerate_qr_codes', stdout=StringIO())

    def test_idempotence_and_missing_or_corrupt_image_recovery(self):
        name = self.table.qr_code.name
        self.table.generate_qr_code()
        self.assertEqual(name, self.table.qr_code.name)
        storage = self.table.qr_code.storage
        storage.delete(name)
        self.table.generate_qr_code()
        self.assertEqual(name, self.table.qr_code.name)
        with storage.open(name, 'wb') as image:
            image.write(b'corrupt')
        with self.captureOnCommitCallbacks(execute=True):
            self.table.generate_qr_code()
        self.assertNotEqual(name, self.table.qr_code.name)
        with storage.open(self.table.qr_code.name, 'rb') as image:
            self.assertEqual(image.read(8), b'\x89PNG\r\n\x1a\n')
        repaired = self.table.qr_code.name
        self.table.generate_qr_code()
        self.assertEqual(repaired, self.table.qr_code.name)

    def test_reset_prunes_only_unreferenced_generated_files_and_is_idempotent(self):
        from pathlib import Path
        from django.core.files.base import ContentFile
        storage = self.table.qr_code.storage
        stale = storage.save('qr_codes/table_999_qr_old.png', ContentFile(b'old'))
        unrelated = storage.save('qr_codes/customer_upload.png', ContentFile(b'keep'))
        legacy_dir = Path(self.media.name) / 'qr_codes'
        legacy = legacy_dir / 'table_999_qr.png'
        legacy.write_bytes(b'old')
        name = self.table.qr_code.name
        call_command('regenerate_qr_codes', reset=True, dry_run=True, stdout=StringIO())
        self.assertTrue(storage.exists(stale))
        for _ in range(2):
            with self.captureOnCommitCallbacks(execute=True):
                call_command('regenerate_qr_codes', reset=True, stdout=StringIO())
            self.table.refresh_from_db()
            self.assertEqual(name, self.table.qr_code.name)
            self.assertTrue(storage.exists(name))
            self.assertTrue(storage.exists(unrelated))
            self.assertFalse(storage.exists(stale))
            self.assertFalse(legacy.exists())
        with self.assertRaises(CommandError):
            call_command('regenerate_qr_codes', reset=True, table_id=self.table.pk, stdout=StringIO())

    def test_owner_views_refresh_existing_stale_references(self):
        from django.contrib.auth import get_user_model
        owner = get_user_model().objects.create_user(username='qr-owner', role='owner', restaurant=self.restaurant)
        self.client.force_login(owner)
        current_name = self.table.qr_code.name
        for name, args in [('table_management', []), ('qr_view', [self.table.pk]), ('qr_print', [self.table.pk]), ('qr_download', [self.table.pk])]:
            with self.subTest(view=name):
                Table.objects.filter(pk=self.table.pk).update(qr_code='qr_codes/table_1_legacy.png')
                response = self.client.get(reverse('restaurant:' + name, args=args))
                self.assertEqual(response.status_code, 200)
                self.table.refresh_from_db()
                self.assertEqual(self.table.qr_code.name, current_name)
                self.assertTrue(self.table.qr_code.storage.exists(self.table.qr_code.name))
                response.close()

    @override_settings(SITE_URL='')
    def test_unconfigured_table_can_be_created_without_bad_qr(self):
        table = Table.objects.create(restaurant=self.restaurant, branch=self.branch, table_number=2)
        self.assertFalse(table.qr_code)
        with self.assertRaises(ImproperlyConfigured):
            table.generate_qr_code()


@override_settings(SITE_URL='https://orders.example.test', ALLOWED_HOSTS=['orders.example.test', 'testserver'])
class TableManagementQRFeatureTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.media = TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        settings = override_settings(MEDIA_ROOT=self.media.name, BASE_DIR=self.media.name)
        settings.enable()
        self.addCleanup(settings.disable)

        User = get_user_model()
        self.restaurant = Restaurant.objects.create(name='QR Test Bistro')
        self.branch1 = Branch.objects.create(restaurant=self.restaurant, name='Downtown', code='DT1', is_main=True)
        self.branch2 = Branch.objects.create(restaurant=self.restaurant, name='Uptown', code='UP1', is_main=False)

        self.table1 = Table.objects.create(
            restaurant=self.restaurant,
            branch=self.branch1,
            table_number=10,
            capacity=4,
        )
        self.table2 = Table.objects.create(
            restaurant=self.restaurant,
            branch=self.branch2,
            table_number=20,
            capacity=6,
        )

        self.owner = User.objects.create_user(
            username='bistro_owner',
            password='password123',
            role='owner',
            restaurant=self.restaurant,
        )
        self.manager_branch1 = User.objects.create_user(
            username='manager_b1',
            password='password123',
            role='manager',
            restaurant=self.restaurant,
            branch=self.branch1,
            is_active_staff=True,
        )

    def test_table_card_contains_all_qr_actions_and_view_modal(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('restaurant:table_management'))
        self.assertEqual(response.status_code, 200)

        # Every card must have View QR, Download QR, and Print QR
        self.assertContains(response, 'View QR')
        self.assertContains(response, 'Download QR')
        self.assertContains(response, 'Print QR')

        # Check action links for table 1
        download_url = reverse('restaurant:qr_download', args=[self.table1.pk])
        print_url = reverse('restaurant:qr_print', args=[self.table1.pk])
        fetch_url = reverse('restaurant:qr_view', args=[self.table1.pk])

        self.assertContains(response, f'href="{download_url}"')
        self.assertContains(response, f'href="{print_url}"')
        self.assertContains(response, f'data-fetch-url="{fetch_url}"')
        self.assertContains(response, f'data-table-number="{self.table1.table_number}"')
        self.assertContains(response, f'data-branch="{self.branch1.name}"')

        # Modal elements must exist in the page
        self.assertContains(response, 'id="viewQrModal"')
        self.assertContains(response, 'id="viewQrImage"')
        self.assertContains(response, 'id="viewQrTableNumber"')
        self.assertContains(response, 'id="viewQrBranchName"')
        self.assertContains(response, 'id="viewQrMenuUrl"')
        self.assertContains(response, 'id="viewQrDownloadBtn"')
        self.assertContains(response, 'id="viewQrPrintBtn"')
        self.assertContains(response, 'id="copyMenuUrlBtn"')

    def test_qr_view_html_and_json(self):
        self.client.force_login(self.owner)
        url = reverse('restaurant:qr_view', args=[self.table1.pk])

        # HTML response
        html_resp = self.client.get(url)
        self.assertEqual(html_resp.status_code, 200)
        self.assertContains(html_resp, f'Table {self.table1.table_number}')
        self.assertContains(html_resp, self.branch1.name)
        self.assertContains(html_resp, self.table1.get_menu_url())
        self.assertContains(html_resp, reverse('restaurant:qr_download', args=[self.table1.pk]))
        self.assertContains(html_resp, reverse('restaurant:qr_print', args=[self.table1.pk]))

        # JSON response
        json_resp = self.client.get(url, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(json_resp.status_code, 200)
        data = json_resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['table_number'], self.table1.table_number)
        self.assertEqual(data['branch'], self.branch1.name)
        self.assertEqual(data['menu_url'], self.table1.get_menu_url())
        self.assertTrue(data['qr_url'].endswith('.png'))
        self.assertEqual(data['download_url'], reverse('restaurant:qr_download', args=[self.table1.pk]))
        self.assertEqual(data['print_url'], reverse('restaurant:qr_print', args=[self.table1.pk]))

    def test_auto_fix_missing_and_corrupt_qr_before_view(self):
        self.client.force_login(self.owner)
        self.table1.generate_qr_code()
        original_name = self.table1.qr_code.name
        storage = self.table1.qr_code.storage
        self.assertTrue(storage.exists(original_name))

        # Corrupt the QR image
        with storage.open(original_name, 'wb') as img:
            img.write(b'corrupted_image_content')

        # Visiting qr_view auto-fixes the QR before responding
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.get(reverse('restaurant:qr_view', args=[self.table1.pk]))
            self.assertEqual(resp.status_code, 200)

        self.table1.refresh_from_db()
        with storage.open(self.table1.qr_code.name, 'rb') as repaired:
            self.assertEqual(repaired.read(8), b'\x89PNG\r\n\x1a\n')

        # Test missing file auto-recovery
        storage.delete(self.table1.qr_code.name)
        with self.captureOnCommitCallbacks(execute=True):
            json_resp = self.client.get(reverse('restaurant:qr_view', args=[self.table1.pk]), {'format': 'json'})
            self.assertEqual(json_resp.status_code, 200)
            self.assertTrue(json_resp.json()['success'])

        self.table1.refresh_from_db()
        self.assertTrue(storage.exists(self.table1.qr_code.name))

    def test_permissions_and_branch_scoping(self):
        # Manager of branch 1 can access table 1
        self.client.force_login(self.manager_branch1)
        resp1 = self.client.get(reverse('restaurant:qr_view', args=[self.table1.pk]))
        self.assertEqual(resp1.status_code, 200)
        resp_dl = self.client.get(reverse('restaurant:qr_download', args=[self.table1.pk]))
        self.assertEqual(resp_dl.status_code, 200)
        resp_pr = self.client.get(reverse('restaurant:qr_print', args=[self.table1.pk]))
        self.assertEqual(resp_pr.status_code, 200)

        # Manager of branch 1 CANNOT access table 2 of branch 2
        resp2 = self.client.get(reverse('restaurant:qr_view', args=[self.table2.pk]))
        self.assertEqual(resp2.status_code, 403)
        resp2_dl = self.client.get(reverse('restaurant:qr_download', args=[self.table2.pk]))
        self.assertEqual(resp2_dl.status_code, 403)
        resp2_pr = self.client.get(reverse('restaurant:qr_print', args=[self.table2.pk]))
        self.assertEqual(resp2_pr.status_code, 403)

        # In table_management, branch 1 manager only sees table 1
        mgmt_resp = self.client.get(reverse('restaurant:table_management'))
        self.assertEqual(mgmt_resp.status_code, 200)
        self.assertContains(mgmt_resp, f'Table {self.table1.table_number}')
        self.assertNotContains(mgmt_resp, f'Table {self.table2.table_number}')

    def test_edit_and_deactivate_remain_working(self):
        self.client.force_login(self.owner)

        # Edit table
        update_url = reverse('restaurant:table_update', args=[self.table1.pk])
        resp = self.client.post(update_url, {
            'table_number': 99,
            'capacity': 8,
            'status': Table.STATUS_AVAILABLE,
            'is_active': True,
        })
        self.assertEqual(resp.status_code, 302)
        self.table1.refresh_from_db()
        self.assertEqual(self.table1.table_number, 99)
        self.assertEqual(self.table1.capacity, 8)

        # Deactivate table
        toggle_url = reverse('restaurant:table_toggle', args=[self.table1.pk])
        resp = self.client.post(toggle_url)
        self.assertEqual(resp.status_code, 302)
        self.table1.refresh_from_db()
        self.assertFalse(self.table1.is_active)
        self.assertEqual(self.table1.status, Table.STATUS_OUT_OF_SERVICE)

        # Re-activate table
        resp = self.client.post(toggle_url)
        self.assertEqual(resp.status_code, 302)
        self.table1.refresh_from_db()
        self.assertTrue(self.table1.is_active)
        self.assertEqual(self.table1.status, Table.STATUS_AVAILABLE)


@override_settings(SITE_URL='https://orders.example.test', ALLOWED_HOSTS=['orders.example.test'])
class QRPhoneRoutingTests(PaymentFlowFixture, TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True, HTTP_HOST='orders.example.test',
                             HTTP_USER_AGENT='Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)')
        self.branch = Branch.objects.create(restaurant=self.restaurant, name='Phone branch', code='PHONE', is_main=True)
        self.table.branch = self.branch
        self.table.save()
        self.chicken.branch_stocks.filter(branch=self.branch).update(current_stock=2000)

    def test_anonymous_phone_host_menu_cart_checkout_preserves_route(self):
        path = urlsplit(self.table.get_menu_url()).path
        response = self.client.get(path, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['table'].pk, self.table.pk)
        self.assertEqual(response.context['table'].branch_id, self.branch.pk)
        token = self.client.cookies['csrftoken'].value
        response = self.client.post(reverse('add_to_cart', args=[self.restaurant.pk, self.table.pk, self.item.pk]),
                                    {'quantity': '1', 'csrfmiddlewaretoken': token}, secure=True,
                                    HTTP_ORIGIN='https://orders.example.test')
        self.assertEqual(response.status_code, 302)
        checkout = reverse('checkout', args=[self.restaurant.pk, self.table.pk])
        self.assertEqual(self.client.get(checkout, secure=True).status_code, 200)
        response = self.client.post(checkout, {'order_type': 'DINE_IN', 'payment_timing': 'PAY_LATER',
                                              'csrfmiddlewaretoken': token}, secure=True,
                                    HTTP_ORIGIN='https://orders.example.test')
        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(table=self.table)
        self.assertEqual(order.restaurant_id, self.restaurant.pk)
        self.assertEqual(order.branch_id, self.branch.pk)
        self.assertEqual(order.table_session.table_id, self.table.pk)
        self.assertEqual(order.payment_status, 'UNPAID')
        self.assertEqual(self.client.get(response.url, secure=True).status_code, 200)

    def test_wrong_restaurant_and_inactive_table_are_not_routable(self):
        other = Restaurant.objects.create(name='Other')
        self.assertEqual(self.client.get(reverse('customer_menu', args=[other.pk, self.table.pk])).status_code, 404)
        self.table.is_active = False
        self.table.save()
        self.assertEqual(self.client.get(urlsplit(self.table.get_menu_url()).path).status_code, 404)
