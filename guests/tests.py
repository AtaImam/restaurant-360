from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from inventory.models import BranchIngredientStock
from orders.models import Order
from orders.test_flow import OrderFlowFixture
from restaurant.models import Branch, Table
from users.models import User
from .models import OrderFeedback, Reservation


class GuestFeaturesTests(OrderFlowFixture, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.branch = Branch.objects.create(restaurant=cls.restaurant, name='Main', code='MAIN', is_main=True)
        cls.other_branch = Branch.objects.create(restaurant=cls.restaurant, name='City', code='CITY')
        cls.foreign_branch = Branch.objects.create(restaurant=cls.other_restaurant, name='Foreign', code='F', is_main=True)
        cls.table.branch = cls.branch
        cls.table.save()
        cls.other_table = Table.objects.create(restaurant=cls.restaurant, branch=cls.other_branch,
                                              table_number=2, qr_code='test-fixtures/table.png')
        for ingredient in (cls.chicken, cls.salt):
            BranchIngredientStock.objects.filter(branch=cls.branch, ingredient=ingredient).update(current_stock=ingredient.current_stock)
        cls.manager = User.objects.create_user(username='manager', role='manager', restaurant=cls.restaurant, branch=cls.branch)
        cls.waiter = User.objects.create_user(username='waiter', role='waiter', restaurant=cls.restaurant, branch=cls.branch)
        cls.foreign_owner = User.objects.create_user(username='foreign', role='owner', restaurant=cls.other_restaurant)

    def reserve_url(self, table=None):
        table = table or self.table
        return reverse('guests:reserve', args=[table.restaurant_id, table.pk])

    def reservation_data(self, **extra):
        return {'name': 'Guest', 'phone': '+880 1700000000',
                'date': str(timezone.localdate() + timedelta(days=1)), 'time': '19:30',
                'party_size': 4, 'note': 'Window seat please', **extra}

    def make_reservation(self, branch=None):
        branch = branch or self.branch
        return Reservation.objects.create(restaurant=branch.restaurant, branch=branch, **self.reservation_data())

    def invitation(self, status='SERVED'):
        order = self.create_order(status=status, reserve=False)
        return OrderFeedback.objects.create(order=order)

    def feedback_url(self, invitation):
        return reverse('guests:feedback', args=[invitation.token])

    def test_menu_links_to_anonymous_branch_reservation(self):
        response = self.client.get(reverse('customer_menu', args=[self.restaurant.pk, self.table.pk]))
        self.assertContains(response, self.reserve_url())
        self.assertContains(self.client.get(self.reserve_url()), 'Reserve a Table')
        users_before = User.objects.count()
        response = self.client.post(self.reserve_url(), self.reservation_data(branch=self.other_branch.pk, restaurant=self.other_restaurant.pk))
        self.assertRedirects(response, self.reserve_url() + '?submitted=1')
        reservation = Reservation.objects.get()
        self.assertEqual((reservation.restaurant_id, reservation.branch_id), (self.restaurant.pk, self.branch.pk))
        self.assertEqual(reservation.status, 'PENDING')
        self.assertEqual(reservation.party_size, 4)
        self.assertEqual(User.objects.count(), users_before)
        self.assertFalse(Order.objects.exists())
        self.table.refresh_from_db()
        self.assertEqual(self.table.status, Table.STATUS_AVAILABLE)

    def test_reservation_validation(self):
        for invalid in ({'name': ''}, {'phone': 'abc'}, {'party_size': 0}, {'party_size': '1.5'},
                        {'date': str(timezone.localdate() - timedelta(days=1))}, {'time': 'bad'},
                        {'date': 'bad'}, {'note': 'x' * 2001}):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.client.post(self.reserve_url(), self.reservation_data(**invalid)).status_code, 400)
        self.assertFalse(Reservation.objects.exists())

    def test_reservation_rejects_wrong_restaurant_and_inactive_branch_or_table(self):
        wrong = reverse('guests:reserve', args=[self.other_restaurant.pk, self.table.pk])
        self.assertEqual(self.client.post(wrong, self.reservation_data()).status_code, 404)
        Branch.objects.filter(pk=self.branch.pk).update(is_active=False)
        self.assertEqual(self.client.post(self.reserve_url(), self.reservation_data()).status_code, 404)
        Branch.objects.filter(pk=self.branch.pk).update(is_active=True)
        Table.objects.filter(pk=self.table.pk).update(is_active=False)
        self.assertEqual(self.client.post(self.reserve_url(), self.reservation_data()).status_code, 404)
        self.assertFalse(Reservation.objects.exists())

    def test_reservation_owner_and_manager_actions(self):
        for user in (self.owner, self.manager):
            self.client.force_login(user)
            reservation = self.make_reservation()
            url = reverse('guests:reservation_action', args=[reservation.pk])
            self.assertEqual(self.client.get(url).status_code, 405)
            self.assertEqual(self.client.post(url, {'action': 'complete'}).status_code, 400)
            for action, status in [('confirm', 'CONFIRMED'), ('complete', 'COMPLETED')]:
                self.assertRedirects(self.client.post(url, {'action': action}), reverse('guests:reservations'))
                reservation.refresh_from_db()
                self.assertEqual(reservation.status, status)
            self.assertEqual(self.client.post(url, {'action': 'cancel'}).status_code, 400)
            cancelled = self.make_reservation()
            self.client.post(reverse('guests:reservation_action', args=[cancelled.pk]), {'action': 'cancel'})
            cancelled.refresh_from_db()
            self.assertEqual(cancelled.status, 'CANCELLED')

    def test_reservation_lists_and_actions_are_branch_and_tenant_scoped(self):
        own = self.make_reservation()
        other = self.make_reservation(self.other_branch)
        foreign = self.make_reservation(self.foreign_branch)
        self.client.force_login(self.owner)
        url = reverse('guests:reservations')
        self.assertEqual(list(self.client.get(url).context['page_obj']), [own])
        for row in (other, foreign):
            self.assertEqual(self.client.post(reverse('guests:reservation_action', args=[row.pk]), {'action': 'confirm'}).status_code, 404)
        session = self.client.session
        session['active_branch_id'] = self.other_branch.pk
        session.save()
        self.assertEqual(list(self.client.get(url).context['page_obj']), [other])
        self.client.force_login(self.manager)
        self.assertEqual(list(self.client.get(url).context['page_obj']), [own])
        self.client.force_login(self.waiter)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(reverse('guests:reservation_action', args=[own.pk]), {'action': 'confirm'}).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_feedback_valid_ratings_optional_comment_and_single_submission(self):
        for rating in range(1, 6):
            invitation = self.invitation()
            url = self.feedback_url(invitation)
            self.assertContains(self.client.get(url), 'No account needed')
            self.assertRedirects(self.client.post(url, {'rating': rating}), url)
            invitation.refresh_from_db()
            self.assertEqual(invitation.rating, rating)
            self.assertIsNotNone(invitation.submitted_at)
            self.assertEqual(self.client.post(url, {'rating': 1, 'comment': 'overwrite'}).status_code, 409)
            invitation.refresh_from_db()
            self.assertEqual(invitation.rating, rating)
            self.assertEqual(invitation.comment, '')
            self.assertContains(self.client.get(url), 'Thank you!')

    def test_feedback_validation(self):
        invitation = self.invitation()
        for data in ({'rating': 0}, {'rating': 6}, {'rating': '1.5'}, {}, {'rating': 5, 'comment': 'x' * 2001}):
            self.assertEqual(self.client.post(self.feedback_url(invitation), data).status_code, 400)
        invitation.refresh_from_db()
        self.assertIsNone(invitation.rating)

    def test_feedback_status_gate_for_get_and_post(self):
        for status in ['NEW', 'ACCEPTED', 'PREPARING', 'READY', 'CANCELLED']:
            invitation = self.invitation(status)
            for method in [self.client.get, self.client.post]:
                self.assertEqual(method(self.feedback_url(invitation), {'rating': 5}).status_code, 403)
            invitation.refresh_from_db()
            self.assertIsNone(invitation.rating)
        invitation = self.invitation('COMPLETED')
        self.assertEqual(self.client.post(self.feedback_url(invitation), {'rating': 5}).status_code, 302)

    def test_feedback_database_constraints(self):
        invitation = self.invitation()
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrderFeedback.objects.create(order=invitation.order)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrderFeedback.objects.filter(pk=invitation.pk).update(rating=6)

    def test_tokens_are_random_and_order_ids_are_not_access_credentials(self):
        first, second = self.invitation(), self.invitation()
        self.assertEqual(len(first.token), 43)
        self.assertNotEqual(first.token, second.token)
        for token in (str(first.order_id), 'invalid', first.token[:-1]):
            url = reverse('guests:feedback', args=[token])
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(url, {'rating': 5}).status_code, 404)
        for name in ('order_success', 'order_status_api'):
            response = self.client.get(reverse(name, args=[first.order_id]))
            self.assertNotContains(response, first.token)
        self.assertNotIn('qr_feedback_orders', self.client.session)

    def test_qr_checkout_grants_link_only_to_placing_browser_after_serving(self):
        self.seed_cart()
        self.assertEqual(self.qr_checkout().status_code, 302)
        order = Order.objects.get()
        self.assertEqual(self.client.session['qr_feedback_orders'], [order.pk])
        api = reverse('order_status_api', args=[order.pk])
        self.assertIsNone(self.client.get(api).json()['feedback_url'])
        self.assertFalse(OrderFeedback.objects.exists())
        Order.objects.filter(pk=order.pk).update(status='SERVED')
        response = self.client.get(api)
        url = response.json()['feedback_url']
        self.assertTrue(url)
        self.assertIn('no-store', response['Cache-Control'])
        invitation = OrderFeedback.objects.get(order=order)
        self.assertContains(self.client.get(reverse('order_success', args=[order.pk])), invitation.token)
        stranger = Client()
        self.assertIsNone(stranger.get(api).json()['feedback_url'])
        self.assertNotContains(stranger.get(reverse('order_success', args=[order.pk])), invitation.token)
        Order.objects.filter(pk=order.pk).update(status='COMPLETED')
        self.assertEqual(self.client.get(api).json()['feedback_url'], url)
        self.assertEqual(self.client.post(url, {'rating': 5, 'comment': 'Excellent'}).status_code, 302)
        self.assertIsNone(self.client.get(api).json()['feedback_url'])

    def test_failed_qr_checkout_does_not_grant_feedback_access(self):
        self.seed_cart(items=[(self.item_a, 0)])
        self.assertEqual(self.qr_checkout().status_code, 400)
        self.assertNotIn('qr_feedback_orders', self.client.session)
        self.assertFalse(OrderFeedback.objects.exists())

    def test_manual_and_pos_orders_have_scoped_receipt_and_bill_qr(self):
        self.client.force_login(self.owner)
        manual = self.create_order(reserve=False)
        self.assertEqual(self.pos_checkout(items=[(self.item_a, 1)], payment_timing='PAY_NOW', payment_method='CASH').status_code, 200)
        pos = Order.objects.exclude(pk=manual.pk).get()
        self.assertEqual(pos.transactions.count(), 1)
        for order in (manual, pos):
            for endpoint in ('order_receipt', 'bill_preview'):
                response = self.client.get(reverse(endpoint, args=[order.pk]))
                self.assertContains(response, 'data:image/png;base64,')
                self.assertContains(response, 'Rate your visit')
                self.assertIn('no-store', response['Cache-Control'])
            invitation = OrderFeedback.objects.get(order=order)
            self.assertEqual(self.client.get(self.feedback_url(invitation)).status_code, 403)
            Order.objects.filter(pk=order.pk).update(status='SERVED')
            self.assertEqual(Client().post(self.feedback_url(invitation), {'rating': 4}).status_code, 302)
        self.assertEqual(pos.transactions.count(), 1)
        self.assertNotIn('qr_feedback_orders', self.client.session)

    def test_bills_do_not_leak_token_across_branch_or_restaurant(self):
        invitation = self.invitation()
        for user in (self.owner, self.foreign_owner):
            self.client.force_login(user)
            session = self.client.session
            session['active_branch_id'] = self.other_branch.pk
            session.save()
            for endpoint in ('order_receipt', 'bill_preview'):
                response = self.client.get(reverse(endpoint, args=[invitation.order_id]))
                self.assertNotContains(response, invitation.token)
                self.assertNotContains(response, 'data:image/png;base64,')

    def test_owner_feedback_summary_uses_only_active_branch_submissions(self):
        first, second = self.invitation(), self.invitation()
        self.client.post(self.feedback_url(first), {'rating': 2, 'comment': '<script>alert(1)</script>'})
        self.client.post(self.feedback_url(second), {'rating': 4})
        self.invitation()  # Unsubmitted invitation is not a response.
        other = self.invitation()
        Order.objects.filter(pk=other.order_id).update(branch=self.other_branch)
        self.client.post(self.feedback_url(other), {'rating': 5})
        foreign_order = Order.objects.create(restaurant=self.other_restaurant, branch=self.foreign_branch,
                                             order_type='TAKEAWAY', status='SERVED', total_amount=1)
        foreign = OrderFeedback.objects.create(order=foreign_order)
        self.client.post(self.feedback_url(foreign), {'rating': 1})
        self.client.force_login(self.owner)
        url = reverse('guests:feedback_list')
        response = self.client.get(url)
        self.assertEqual(response.context['average_rating'], 3)
        self.assertEqual(response.context['feedback_count'], 2)
        self.assertContains(response, f'Order #{first.order_id}')
        self.assertContains(response, '&lt;script&gt;')
        self.assertNotContains(response, first.token)
        session = self.client.session
        session['active_branch_id'] = self.other_branch.pk
        session.save()
        self.assertEqual(self.client.get(url).context['average_rating'], 5)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_guest_posts_require_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        invitation = self.invitation()
        self.assertEqual(csrf_client.post(self.reserve_url(), self.reservation_data()).status_code, 403)
        self.assertEqual(csrf_client.post(self.feedback_url(invitation), {'rating': 5}).status_code, 403)

    @override_settings(SITE_URL='http://192.168.1.120:8000')
    def test_receipt_feedback_qr_url_uses_site_url_and_exact_order(self):
        self.client.force_login(self.owner)
        order = self.create_order(status='SERVED', reserve=False)

        # Receipt uses SITE_URL and encodes exact order feedback URL
        for endpoint in ('order_receipt', 'bill_preview'):
            response = self.client.get(reverse(endpoint, args=[order.pk]))
            self.assertEqual(response.status_code, 200)
            invitation = OrderFeedback.objects.get(order=order)
            expected_url = f'http://192.168.1.120:8000{reverse("guests:feedback", args=[invitation.token])}'
            self.assertEqual(response.context['feedback_url'], expected_url)
            self.assertContains(response, expected_url)
            self.assertNotContains(response, 'http://localhost')
            self.assertNotContains(response, 'http://127.0.0.1')
            self.assertNotContains(response, 'http://testserver')

        # Dynamic update of SITE_URL immediately reflects on receipt (no stale URL)
        with override_settings(SITE_URL='https://orders.example.com'):
            resp2 = self.client.get(reverse('order_receipt', args=[order.pk]))
            invitation.refresh_from_db()
            expected_url_2 = f'https://orders.example.com{reverse("guests:feedback", args=[invitation.token])}'
            self.assertEqual(resp2.context['feedback_url'], expected_url_2)
            self.assertContains(resp2, expected_url_2)

    @override_settings(SITE_URL='http://192.168.1.120:8000', CSRF_TRUSTED_ORIGINS=['http://192.168.1.120:8000'])
    def test_lan_mobile_csrf_submission(self):
        invitation = self.invitation('SERVED')
        url = self.feedback_url(invitation)
        lan_client = Client(enforce_csrf_checks=True)

        # GET page to obtain CSRF cookie
        get_resp = lan_client.get(url, HTTP_HOST='192.168.1.120:8000')
        self.assertEqual(get_resp.status_code, 200)
        csrf_token = lan_client.cookies['csrftoken'].value

        # Untrusted origin is rejected with 403
        untrusted_resp = lan_client.post(
            url,
            {'rating': 5, 'comment': 'Blocked', 'csrfmiddlewaretoken': csrf_token},
            HTTP_HOST='192.168.1.120:8000',
            HTTP_ORIGIN='http://attacker.example.com',
        )
        self.assertEqual(untrusted_resp.status_code, 403)
        invitation.refresh_from_db()
        self.assertIsNone(invitation.rating)

        # Trusted origin matching CSRF_TRUSTED_ORIGINS over LAN succeeds
        trusted_resp = lan_client.post(
            url,
            {'rating': 5, 'comment': 'Delicious food and great atmosphere!', 'csrfmiddlewaretoken': csrf_token},
            HTTP_HOST='192.168.1.120:8000',
            HTTP_ORIGIN='http://192.168.1.120:8000',
        )
        self.assertRedirects(trusted_resp, url)
        invitation.refresh_from_db()
        self.assertEqual(invitation.rating, 5)
        self.assertEqual(invitation.comment, 'Delicious food and great atmosphere!')

    def test_served_and_completed_eligibility(self):
        # Orders not yet served are rejected from rating
        for status in ['NEW', 'ACCEPTED', 'PREPARING', 'READY', 'CANCELLED']:
            inv = self.invitation(status)
            url = self.feedback_url(inv)
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, {'rating': 4}).status_code, 403)
            inv.refresh_from_db()
            self.assertIsNone(inv.rating)

        # SERVED order allows viewing and rating
        served_inv = self.invitation('SERVED')
        served_url = self.feedback_url(served_inv)
        self.assertEqual(self.client.get(served_url).status_code, 200)
        self.assertContains(self.client.get(served_url), 'How was your experience?')
        post_served = self.client.post(served_url, {'rating': 4, 'comment': 'Fresh and timely'})
        self.assertRedirects(post_served, served_url)
        served_inv.refresh_from_db()
        self.assertEqual(served_inv.rating, 4)
        self.assertEqual(served_inv.comment, 'Fresh and timely')

        # COMPLETED order allows viewing and rating
        completed_inv = self.invitation('COMPLETED')
        completed_url = self.feedback_url(completed_inv)
        self.assertEqual(self.client.get(completed_url).status_code, 200)
        post_completed = self.client.post(completed_url, {'rating': 5, 'comment': 'Excellent service'})
        self.assertRedirects(post_completed, completed_url)
        completed_inv.refresh_from_db()
        self.assertEqual(completed_inv.rating, 5)
        self.assertEqual(completed_inv.comment, 'Excellent service')

    def test_duplicate_feedback_blocking_and_single_response_display(self):
        invitation = self.invitation('SERVED')
        url = self.feedback_url(invitation)

        # Submit initial rating
        first_post = self.client.post(url, {'rating': 5, 'comment': 'First authentic review'})
        self.assertRedirects(first_post, url)
        invitation.refresh_from_db()
        self.assertEqual(invitation.rating, 5)
        self.assertEqual(invitation.comment, 'First authentic review')

        # Viewing confirmation page prevents resubmission form from rendering
        conf_resp = self.client.get(url)
        self.assertEqual(conf_resp.status_code, 200)
        self.assertContains(conf_resp, 'Thank you! Your feedback has been received.')
        self.assertNotContains(conf_resp, '<form')
        self.assertNotContains(conf_resp, 'Submit Feedback')

        # Second POST returns 409 Conflict and does not modify existing feedback
        dup_resp = self.client.post(url, {'rating': 1, 'comment': 'Tampered comment'})
        self.assertEqual(dup_resp.status_code, 409)
        invitation.refresh_from_db()
        self.assertEqual(invitation.rating, 5)
        self.assertEqual(invitation.comment, 'First authentic review')

        # Owner feedback page receives the submitted review
        self.client.force_login(self.owner)
        owner_resp = self.client.get(reverse('guests:feedback_list'))
        self.assertEqual(owner_resp.status_code, 200)
        self.assertContains(owner_resp, f'Order #{invitation.order_id}')
        self.assertContains(owner_resp, 'First authentic review')
        self.assertNotContains(owner_resp, 'Tampered comment')

    @override_settings(SITE_URL='http://192.168.1.104:8000', CSRF_TRUSTED_ORIGINS=['http://192.168.1.104:8000'])
    def test_e2e_receipt_qr_feedback_flow_submission_and_duplicate_blocked(self):
        # 1. Staff manages order to SERVED status
        self.client.force_login(self.owner)
        order = self.create_order(status='SERVED', reserve=False)

        # 2. Receipt QR: Receipt view generates feedback QR and URL pointing to exact order
        receipt_resp = self.client.get(reverse('order_receipt', args=[order.pk]), HTTP_HOST='192.168.1.104:8000')
        self.assertEqual(receipt_resp.status_code, 200)
        feedback_url = receipt_resp.context.get('feedback_url')
        self.assertTrue(len(receipt_resp.context.get('feedback_qr', '')) > 0)
        self.assertContains(receipt_resp, 'data:image/png;base64,')
        self.assertContains(receipt_resp, feedback_url)

        invitation = OrderFeedback.objects.get(order=order)
        expected_path = reverse('guests:feedback', args=[invitation.token])
        self.assertTrue(feedback_url.endswith(expected_path))

        # 3. Customer opens feedback page via QR on phone over LAN (enforcing CSRF)
        customer_client = Client(enforce_csrf_checks=True)
        get_page_resp = customer_client.get(
            expected_path,
            HTTP_HOST='192.168.1.104:8000',
            HTTP_ORIGIN='http://192.168.1.104:8000',
        )
        self.assertEqual(get_page_resp.status_code, 200)
        self.assertContains(get_page_resp, 'How was your experience?')
        self.assertContains(get_page_resp, 'No account needed')
        self.assertContains(get_page_resp, 'csrfmiddlewaretoken')

        csrf_token = customer_client.cookies['csrftoken'].value

        # 4. Customer submits 1-5 rating + comment via phone/LAN POST
        comment_text = 'Delicious meal, fast kitchen service, will definitely return!'
        post_data = {
            'rating': 5,
            'comment': comment_text,
            'csrfmiddlewaretoken': csrf_token,
        }
        submit_resp = customer_client.post(
            expected_path,
            post_data,
            HTTP_HOST='192.168.1.104:8000',
            HTTP_ORIGIN='http://192.168.1.104:8000',
            HTTP_REFERER=f'http://192.168.1.104:8000{expected_path}',
        )
        self.assertRedirects(submit_resp, expected_path)

        # 5. Rating and comment are saved in database
        invitation.refresh_from_db()
        self.assertEqual(invitation.rating, 5)
        self.assertEqual(invitation.comment, comment_text)
        self.assertIsNotNone(invitation.submitted_at)

        # 6. Customer is shown success/thank-you page; submission form is blocked/removed
        success_page_resp = customer_client.get(
            expected_path,
            HTTP_HOST='192.168.1.104:8000',
        )
        self.assertEqual(success_page_resp.status_code, 200)
        self.assertContains(success_page_resp, 'Thank you! Your feedback has been received.')
        self.assertNotContains(success_page_resp, '<form')
        self.assertNotContains(success_page_resp, 'Submit Feedback')

        # 7. Duplicate submission is blocked (409 Conflict) and does not modify saved data
        dup_resp = customer_client.post(
            expected_path,
            {
                'rating': 1,
                'comment': 'Attempting duplicate overwrite',
                'csrfmiddlewaretoken': csrf_token,
            },
            HTTP_HOST='192.168.1.104:8000',
            HTTP_ORIGIN='http://192.168.1.104:8000',
            HTTP_REFERER=f'http://192.168.1.104:8000{expected_path}',
        )
        self.assertEqual(dup_resp.status_code, 409)
        invitation.refresh_from_db()
        self.assertEqual(invitation.rating, 5)
        self.assertEqual(invitation.comment, comment_text)

        # 8. Owner feedback page displays the saved rating and comment
        owner_resp = self.client.get(reverse('guests:feedback_list'))
        self.assertEqual(owner_resp.status_code, 200)
        self.assertContains(owner_resp, f'Order #{order.pk}')
        self.assertContains(owner_resp, comment_text)
        self.assertNotContains(owner_resp, 'Attempting duplicate overwrite')

        # 9. Also verify COMPLETED order full flow end-to-end
        completed_order = self.create_order(status='COMPLETED', reserve=False)
        comp_receipt = self.client.get(reverse('order_receipt', args=[completed_order.pk]), HTTP_HOST='192.168.1.104:8000')
        comp_inv = OrderFeedback.objects.get(order=completed_order)
        comp_path = reverse('guests:feedback', args=[comp_inv.token])
        self.assertTrue(comp_receipt.context['feedback_url'].endswith(comp_path))

        comp_client = Client(enforce_csrf_checks=True)
        comp_get = comp_client.get(comp_path, HTTP_HOST='192.168.1.104:8000', HTTP_ORIGIN='http://192.168.1.104:8000')
        self.assertEqual(comp_get.status_code, 200)
        comp_csrf = comp_client.cookies['csrftoken'].value

        comp_post = comp_client.post(
            comp_path,
            {'rating': 4, 'comment': 'Great takeout order', 'csrfmiddlewaretoken': comp_csrf},
            HTTP_HOST='192.168.1.104:8000',
            HTTP_ORIGIN='http://192.168.1.104:8000',
            HTTP_REFERER=f'http://192.168.1.104:8000{comp_path}',
        )
        self.assertRedirects(comp_post, comp_path)
        comp_inv.refresh_from_db()
        self.assertEqual(comp_inv.rating, 4)
        self.assertEqual(comp_inv.comment, 'Great takeout order')

