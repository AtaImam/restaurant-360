"""Tests for Menu + Recipe + Inventory end-to-end workflow."""

from decimal import Decimal
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from inventory.models import (
    Ingredient,
    IngredientCategory,
    IngredientPriceHistory,
    PurchaseOrder,
    PurchaseOrderItem,
    Recipe,
    RecipeIngredient,
    StockCount,
    StockReservation,
    StockTransaction,
    Supplier,
    WasteRecord,
)
from django.core.management import call_command
from inventory.services import (
    backfill_opening_stock_transactions,
    calculate_item_available_portions,
    record_opening_stock,
    record_stock_count_audit,
    record_stock_purchase,
    record_stock_waste,
    reserve_stock_for_order,
)
from menu.models import Category, MenuItem, SetMenuComponent
from orders.test_flow import OrderFlowFixture


class MenuRecipeInventoryWorkflowTests(OrderFlowFixture, TestCase):

    def test_recipe_estimated_food_cost_respects_yield_quantity(self):
        """Recipe cost must divide total batch cost by yield_quantity (cost per portion)."""
        self.chicken.current_pack_price = Decimal("500.00")  # 500 for 1000g -> 0.50/g
        self.chicken.save()
        self.salt.current_pack_price = Decimal("20.00")      # 20 for 100g -> 0.20/g
        self.salt.save()

        # Recipe A has 200g chicken (100.00) + 4g salt (0.80) = 100.80 total batch cost
        recipe = Recipe.objects.get(menu_item=self.item_a)
        recipe.yield_quantity = Decimal("1")
        recipe.save()
        self.assertEqual(recipe.estimated_food_cost, Decimal("100.80"))

        # If batch yields 2 portions, cost per portion must be 100.80 / 2 = 50.40
        recipe.yield_quantity = Decimal("2")
        recipe.save()
        self.assertEqual(recipe.estimated_food_cost, Decimal("50.40"))

    def test_calculate_item_available_portions_normal_and_set_menu(self):
        """Available portions must reflect available stock (physical - reservations)."""
        # self.chicken stock=1000, self.salt stock=100
        # self.item_a has yield=2, chicken=200g, salt=4g -> per portion: 100g chicken, 2g salt.
        portions = calculate_item_available_portions(self.item_a)
        # chicken allows 1000/100 = 10, salt allows 100/2 = 50 -> min is 10
        self.assertEqual(portions, 10)

        # Set menu consists of item_a x 1.5 + item_b x 2
        # item_b requires 50g chicken, 1g salt.
        # Set menu total per portion: 1.5 * 100 + 2 * 50 = 250g chicken, 1.5 * 2 + 2 * 1 = 5g salt.
        # Chicken: 1000 / 250 = 4 portions.
        # Salt: 100 / 5 = 20 portions.
        # Min is 4 portions.
        set_portions = calculate_item_available_portions(self.set_menu)
        self.assertEqual(set_portions, 4)

        # Reserving stock for an order reduces available portions
        order = self.create_order(items=[(self.item_a, 5)])  # reserves 500g chicken
        portions_after = calculate_item_available_portions(self.item_a)
        # remaining available chicken: 1000 - 500 = 500g -> 5 portions
        self.assertEqual(portions_after, 5)

    def test_customer_menu_and_add_to_cart_sold_out_enforcement(self):
        """When stock is insufficient (< 1 portion), Customer Menu shows Sold Out and add_to_cart is blocked."""
        # Set chicken stock to 50g (item_a requires 100g per portion)
        Ingredient.objects.filter(pk=self.chicken.pk).update(current_stock=50)

        menu_url = reverse("customer_menu", args=[self.restaurant.pk, self.table.pk])
        response = self.client.get(menu_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sold Out")

        # Attempting to add sold-out item to cart is rejected
        add_url = reverse("add_to_cart", args=[self.restaurant.pk, self.table.pk, self.item_a.pk])
        add_resp = self.client.post(add_url, {"return_to": "menu"}, follow=True)
        self.assertEqual(add_resp.status_code, 200)
        self.assertContains(add_resp, "is currently sold out")

        # Cart session must remain empty
        cart_key = f"cart_{self.restaurant.pk}_{self.table.pk}"
        self.assertEqual(self.client.session.get(cart_key, {}), {})

    def test_menu_item_management_edit_toggle_delete(self):
        """Managers can edit, toggle availability, and delete menu items."""
        self.client.force_login(self.owner)

        # Edit item
        edit_url = reverse("menu_item_edit", args=[self.item_a.pk])
        edit_resp = self.client.post(edit_url, {
            "name": "Updated Burger",
            "category": self.category.pk,
            "price": "199.99",
            "is_available": "on",
            "item_type": "NORMAL",
            "serves": "1",
        })
        self.assertEqual(edit_resp.status_code, 302)
        self.item_a.refresh_from_db()
        self.assertEqual(self.item_a.name, "Updated Burger")
        self.assertEqual(self.item_a.price, Decimal("199.99"))

        # Toggle availability
        toggle_url = reverse("menu_item_toggle_availability", args=[self.item_a.pk])
        self.client.post(toggle_url)
        self.item_a.refresh_from_db()
        self.assertFalse(self.item_a.is_available)

        self.client.post(toggle_url)
        self.item_a.refresh_from_db()
        self.assertTrue(self.item_a.is_available)

        # Attempt to delete item_a (which is protected by SetMenuComponent) -> should fail gracefully
        del_url = reverse("menu_item_delete", args=[self.item_a.pk])
        del_resp = self.client.post(del_url)
        self.assertEqual(del_resp.status_code, 302)
        self.assertTrue(MenuItem.objects.filter(pk=self.item_a.pk).exists())

        # Create a standalone item and delete it successfully
        standalone_item = MenuItem.objects.create(
            category=self.category,
            name="Delete Me Item",
            price=Decimal("50.00"),
            is_available=True,
        )
        del_standalone_url = reverse("menu_item_delete", args=[standalone_item.pk])
        del_resp2 = self.client.post(del_standalone_url)
        self.assertEqual(del_resp2.status_code, 302)
        self.assertFalse(MenuItem.objects.filter(pk=standalone_item.pk).exists())

    def test_supplier_crud_and_toggle(self):
        """Supplier creation, editing, and activation toggle."""
        self.client.force_login(self.owner)

        # Create
        create_url = reverse("supplier_create")
        resp = self.client.post(create_url, {
            "restaurant": self.restaurant.pk,
            "name": "Apex Poultry",
            "contact_name": "Karim",
            "phone": "01711111111",
            "email": "karim@apex.com",
            "address": "Tejgaon, Dhaka",
        })
        self.assertEqual(resp.status_code, 302)
        supplier = Supplier.objects.get(name="Apex Poultry")
        self.assertTrue(supplier.is_active)
        self.assertEqual(supplier.contact_name, "Karim")

        # Edit
        edit_url = reverse("supplier_edit", args=[supplier.pk])
        self.client.post(edit_url, {
            "restaurant": self.restaurant.pk,
            "name": "Apex Poultry Ltd",
            "contact_name": "Karim Rahman",
            "phone": "01711111111",
            "email": "karim@apex.com",
            "address": "Tejgaon, Dhaka",
        })
        supplier.refresh_from_db()
        self.assertEqual(supplier.name, "Apex Poultry Ltd")
        self.assertEqual(supplier.contact_name, "Karim Rahman")

        # Toggle active
        toggle_url = reverse("supplier_toggle", args=[supplier.pk])
        self.client.post(toggle_url)
        supplier.refresh_from_db()
        self.assertFalse(supplier.is_active)

    def test_purchase_order_receiving_updates_stock_price_and_transactions(self):
        """Receiving a purchase order atomically adds stock, creates PURCHASE transaction, and updates pack price."""
        supplier = Supplier.objects.create(
            restaurant=self.restaurant,
            name="Spice World",
        )
        initial_stock = self.chicken.current_stock  # 1000g (pack size 1000g)
        self.assertEqual(initial_stock, Decimal("1000"))

        # Create PO with 2 packs of chicken at 550.00 each
        po = PurchaseOrder.objects.create(
            restaurant=self.restaurant,
            supplier=supplier,
            invoice_number="INV-001",
            purchase_date="2026-09-18",
            status=PurchaseOrder.Status.DRAFT,
            total_amount=Decimal("1100.00"),
        )
        PurchaseOrderItem.objects.create(
            purchase_order=po,
            ingredient=self.chicken,
            pack_quantity=Decimal("2"),
            pack_price=Decimal("550.00"),
            total_price=Decimal("1100.00"),
        )

        # Receive via service
        record_stock_purchase(po, user=self.owner)

        po.refresh_from_db()
        self.assertEqual(po.status, PurchaseOrder.Status.RECEIVED)
        self.assertIsNotNone(po.received_at)
        self.assertEqual(po.received_by, self.owner)

        # Stock increased: 1000 + (2 packs * 1000g) = 3000g
        self.chicken.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, Decimal("3000"))
        # Pack price updated
        self.assertEqual(self.chicken.current_pack_price, Decimal("550.00"))

        # Transaction created
        tx = StockTransaction.objects.filter(
            ingredient=self.chicken,
            transaction_type=StockTransaction.TransactionType.PURCHASE,
            note__icontains=f"#{po.id}",
        ).first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.quantity, Decimal("2000"))

        # Price history recorded
        ph = IngredientPriceHistory.objects.filter(
            ingredient=self.chicken,
            pack_price=Decimal("550.00"),
        ).first()
        self.assertIsNotNone(ph)

        # Cannot receive twice
        with self.assertRaises(ValidationError):
            record_stock_purchase(po, user=self.owner)

    def test_record_stock_waste_safely_deducts_and_guards_reservations(self):
        """Wastage safely deducts stock, but cannot drop stock below active order reservations."""
        initial_stock = self.chicken.current_stock  # 1000g

        # Normal waste: deduct 200g
        waste = record_stock_waste(
            ingredient=self.chicken,
            quantity=Decimal("200"),
            reason=WasteRecord.Reason.SPOILAGE,
            note="Spilled on floor",
            user=self.owner,
        )
        self.chicken.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, Decimal("800"))
        self.assertEqual(waste.quantity, Decimal("200"))
        tx = StockTransaction.objects.filter(
            ingredient=self.chicken, transaction_type=StockTransaction.TransactionType.WASTE
        ).first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.quantity, Decimal("200"))

        # Reserve 700g for an order (requires 7 portions of item_a)
        order = self.create_order(items=[(self.item_a, 7)])  # reserves 700g
        # Remaining free stock is 800 - 700 = 100g

        # Trying to waste 150g must be rejected because it breaches active reservations
        with self.assertRaises(ValidationError) as cm:
            record_stock_waste(
                ingredient=self.chicken,
                quantity=Decimal("150"),
                reason=WasteRecord.Reason.EXPIRED,
            )
        self.assertIn("active reservations", str(cm.exception))
        # Stock untouched
        self.chicken.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, Decimal("800"))

    def test_record_stock_count_audit_variance_and_adjustment(self):
        """Stock count audit calculates variance and creates ADJUSTMENT_IN or ADJUSTMENT_OUT."""
        # Current stock is 1000g. Physical count finds 1200g (surplus +200g).
        count1 = record_stock_count_audit(
            ingredient=self.chicken,
            actual_quantity=Decimal("1200"),
            note="Found extra unopened pack",
            user=self.owner,
        )
        self.chicken.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, Decimal("1200"))
        self.assertEqual(count1.variance, Decimal("200.000"))
        tx1 = StockTransaction.objects.filter(
            ingredient=self.chicken, transaction_type=StockTransaction.TransactionType.ADJUSTMENT_IN
        ).first()
        self.assertIsNotNone(tx1)
        self.assertEqual(tx1.quantity, Decimal("200"))

        # Next count finds 1150g (shortage -50g).
        count2 = record_stock_count_audit(
            ingredient=self.chicken,
            actual_quantity=Decimal("1150"),
            note="Minor evaporation/trimming",
            user=self.owner,
        )
        self.chicken.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, Decimal("1150"))
        self.assertEqual(count2.variance, Decimal("-50.000"))
        tx2 = StockTransaction.objects.filter(
            ingredient=self.chicken, transaction_type=StockTransaction.TransactionType.ADJUSTMENT_OUT
        ).first()
        self.assertIsNotNone(tx2)
        self.assertEqual(tx2.quantity, Decimal("50"))

    def test_stock_transactions_ledger_filtering(self):
        """Stock transactions ledger page displays transactions and respects filter query params."""
        self.client.force_login(self.owner)

        # Generate sample transactions
        record_stock_waste(
            ingredient=self.chicken,
            quantity=Decimal("50"),
            reason=WasteRecord.Reason.EXPIRED,
            user=self.owner,
        )
        record_stock_count_audit(
            ingredient=self.salt,
            actual_quantity=Decimal("150"),
            user=self.owner,
        )

        url = reverse("stock_transaction_list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Chicken")
        self.assertContains(resp, "Salt")

        # Filter by ingredient
        resp_filtered = self.client.get(url, {"ingredient": self.chicken.pk})
        self.assertEqual(resp_filtered.status_code, 200)
        self.assertContains(resp_filtered, "Chicken")

        # Filter by transaction type
        resp_type = self.client.get(url, {"type": "WASTE"})
        self.assertEqual(resp_type.status_code, 200)
        self.assertContains(resp_type, "Waste")

    def test_opening_balance_does_not_change_current_stock(self):
        """
        Regression test:
        OPENING_BALANCE transaction records historical baseline without altering current_stock.
        """
        initial_stock = self.chicken.current_stock
        self.assertGreater(initial_stock, Decimal("0"))

        txn = record_opening_stock(
            ingredient=self.chicken,
            quantity=initial_stock,
            unit_cost=self.chicken.current_unit_cost,
            note="Baseline imported stock test",
        )

        self.assertIsNotNone(txn)
        self.assertEqual(txn.transaction_type, StockTransaction.TransactionType.OPENING_BALANCE)
        self.assertEqual(txn.quantity, initial_stock)
        self.assertEqual(txn.unit_cost_snapshot, self.chicken.current_unit_cost)
        self.assertEqual(txn.restaurant, self.restaurant)
        self.assertEqual(txn.note, "Baseline imported stock test")

        # Current stock must be strictly untouched
        self.chicken.refresh_from_db()
        self.assertEqual(self.chicken.current_stock, initial_stock)

    def test_ingredient_create_records_opening_balance_without_doubling_stock(self):
        """
        Creating an ingredient with initial stock creates an OPENING_BALANCE transaction
        and preserves current_stock equal to initial stock (not doubled).
        """
        self.client.force_login(self.owner)
        cat = IngredientCategory.objects.filter(restaurant=self.restaurant).first()

        data = {
            "restaurant": str(self.restaurant.pk),
            "category": str(cat.pk),
            "name": "Organic Olive Oil",
            "sku": "OIL-TEST-001",
            "base_unit": "ML",
            "pack_size": "1000",
            "pack_unit": "BOTTLE",
            "current_pack_price": "1200.00",
            "current_stock": "2500",
            "minimum_level": "500",
            "target_level": "5000",
        }

        resp = self.client.post(reverse("ingredient_create"), data, follow=True)
        self.assertEqual(resp.status_code, 200)

        oil = Ingredient.objects.get(sku="OIL-TEST-001")
        self.assertEqual(oil.current_stock, Decimal("2500.000"))

        txns = StockTransaction.objects.filter(ingredient=oil)
        self.assertEqual(txns.count(), 1)
        tx = txns.first()
        self.assertEqual(tx.transaction_type, StockTransaction.TransactionType.OPENING_BALANCE)
        self.assertEqual(tx.quantity, Decimal("2500.000"))
        self.assertEqual(tx.unit_cost_snapshot, Decimal("1.200"))
        self.assertEqual(tx.restaurant, self.restaurant)

    def test_opening_stock_backfill_is_idempotent(self):
        """
        Regression test:
        Backfill creates OPENING_BALANCE only for ingredients with current_stock > 0
        and no prior opening or purchase transactions. It never modifies current_stock,
        and multiple executions create zero duplicate transactions.
        """
        cat = IngredientCategory.objects.filter(restaurant=self.restaurant).first()

        # Clear any existing opening balance or purchase transactions for clean test setup
        StockTransaction.objects.filter(
            transaction_type__in=[
                StockTransaction.TransactionType.OPENING_BALANCE,
                StockTransaction.TransactionType.PURCHASE,
            ]
        ).delete()

        # 1. Ingredient with stock > 0, no transactions -> candidate
        ing1 = Ingredient.objects.create(
            restaurant=self.restaurant,
            category=cat,
            name="Test Backfill Ing 1",
            sku="BF-001",
            base_unit=Ingredient.BaseUnit.GRAM,
            pack_size=Decimal("1000.000"),
            current_pack_price=Decimal("100.00"),
            current_stock=Decimal("350.000"),
        )

        # 2. Ingredient with stock == 0 -> should NOT get opening balance
        ing2 = Ingredient.objects.create(
            restaurant=self.restaurant,
            category=cat,
            name="Test Backfill Ing Zero",
            sku="BF-002",
            base_unit=Ingredient.BaseUnit.GRAM,
            pack_size=Decimal("1000.000"),
            current_pack_price=Decimal("100.00"),
            current_stock=Decimal("0.000"),
        )

        # 3. Ingredient with stock > 0, but already has PURCHASE transaction -> should NOT get opening balance
        ing3 = Ingredient.objects.create(
            restaurant=self.restaurant,
            category=cat,
            name="Test Backfill Ing Has Purchase",
            sku="BF-003",
            base_unit=Ingredient.BaseUnit.GRAM,
            pack_size=Decimal("1000.000"),
            current_pack_price=Decimal("100.00"),
            current_stock=Decimal("200.000"),
        )
        StockTransaction.objects.create(
            ingredient=ing3,
            restaurant=self.restaurant,
            transaction_type=StockTransaction.TransactionType.PURCHASE,
            quantity=Decimal("200.000"),
            unit_cost_snapshot=Decimal("0.100"),
        )

        # First run: backfill
        created = backfill_opening_stock_transactions()
        self.assertGreaterEqual(len(created), 1)

        # Verify ing1 was backfilled
        ing1_txns = StockTransaction.objects.filter(
            ingredient=ing1,
            transaction_type=StockTransaction.TransactionType.OPENING_BALANCE,
        )
        self.assertEqual(ing1_txns.count(), 1)
        ing1_tx = ing1_txns.first()
        self.assertEqual(ing1_tx.quantity, Decimal("350.000"))
        self.assertEqual(ing1_tx.unit_cost_snapshot, Decimal("0.100"))
        self.assertEqual(ing1_tx.restaurant, self.restaurant)
        # Stock must remain strictly untouched
        ing1.refresh_from_db()
        self.assertEqual(ing1.current_stock, Decimal("350.000"))

        # ing2 (zero stock) must have 0 opening balance transactions
        self.assertFalse(
            StockTransaction.objects.filter(
                ingredient=ing2,
                transaction_type=StockTransaction.TransactionType.OPENING_BALANCE,
            ).exists()
        )

        # ing3 (already had PURCHASE) must not get an opening balance transaction
        self.assertFalse(
            StockTransaction.objects.filter(
                ingredient=ing3,
                transaction_type=StockTransaction.TransactionType.OPENING_BALANCE,
            ).exists()
        )

        # Second run: must be strictly idempotent (0 new transactions)
        second_run_created = backfill_opening_stock_transactions()
        self.assertEqual(len(second_run_created), 0)

        # Third run via management command: must also be idempotent
        from io import StringIO
        out = StringIO()
        call_command("backfill_opening_stock", stdout=out)
        self.assertIn("No ingredients needed opening balance backfill", out.getvalue())

        # Final check: ing1 still has exactly 1 transaction and untouched stock
        self.assertEqual(
            StockTransaction.objects.filter(
                ingredient=ing1,
                transaction_type=StockTransaction.TransactionType.OPENING_BALANCE,
            ).count(),
            1,
        )
        ing1.refresh_from_db()
        self.assertEqual(ing1.current_stock, Decimal("350.000"))

