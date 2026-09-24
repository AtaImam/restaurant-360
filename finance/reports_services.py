from decimal import Decimal
from django.db.models import Avg, Count, F, Q, Sum
from django.db.models.functions import Coalesce

from inventory.models import (
    BranchIngredientStock,
    Ingredient,
    PurchaseOrder,
    PurchaseOrderItem,
    StockTransaction,
    WasteRecord,
)
from menu.models import Category, MenuItem
from orders.models import Order, OrderItem, PaymentTransaction
from staff.models import PayrollRecord
from .models import Expense, ExpenseCategory
from .money_utils import format_money, normalize_zero_money


def get_sales_report_data(restaurant, start_datetime, end_datetime, branch=None):
    """
    Generate Sales Report metrics for a restaurant within the date range.
    Driven by settled financial transactions (PaymentTransaction), never creation time.
    """
    # Query transactions in the datetime range for this restaurant
    txns = PaymentTransaction.objects.filter(
        restaurant=restaurant,
        transaction_at__gte=start_datetime,
        transaction_at__lte=end_datetime,
    ).select_related("order")

    if branch is not None:
        txns = txns.filter(branch=branch, order__branch=branch)
    txns = txns.filter(order__restaurant=restaurant)

    gross_collections = (
        txns.filter(transaction_type=PaymentTransaction.TYPE_PAYMENT).aggregate(
            total=Coalesce(Sum("amount"), Decimal("0.00"))
        )["total"]
    )

    refunds = (
        txns.filter(transaction_type=PaymentTransaction.TYPE_REFUND).aggregate(
            total=Coalesce(Sum("amount"), Decimal("0.00"))
        )["total"]
    )

    net_collections = gross_collections - refunds

    # Distinct paid orders with payment transactions in range (excluding cancelled orders)
    paid_order_ids = (
        txns.filter(transaction_type=PaymentTransaction.TYPE_PAYMENT)
        .exclude(order__status=Order.STATUS_CANCELLED)
        .values_list("order_id", flat=True)
        .distinct()
    )

    orders_qs = Order.objects.filter(id__in=paid_order_ids, restaurant=restaurant)
    order_count = orders_qs.count()

    order_aggregates = orders_qs.aggregate(
        subtotal=Coalesce(Sum("subtotal"), Decimal("0.00")),
        discount=Coalesce(Sum("discount_amount"), Decimal("0.00")),
        service_charge=Coalesce(Sum("service_charge"), Decimal("0.00")),
        vat=Coalesce(Sum("vat_amount"), Decimal("0.00")),
        total=Coalesce(Sum("total_amount"), Decimal("0.00")),
    )

    # Net Sales is the actual food & beverage revenue before tax & service charge, minus refunds
    food_bev_subtotal = order_aggregates["subtotal"] - order_aggregates["discount"]
    net_sales = max(Decimal("0.00"), food_bev_subtotal - refunds)
    vat_amount = order_aggregates["vat"]
    service_charge = order_aggregates["service_charge"]

    average_order_value = (
        (net_sales / Decimal(order_count)).quantize(Decimal("0.01"))
        if order_count > 0
        else Decimal("0.00")
    )

    # Payment method breakdown
    method_data = {}
    for code, label in Order.PAYMENT_METHOD_CHOICES:
        m_payments = txns.filter(
            transaction_type=PaymentTransaction.TYPE_PAYMENT,
            payment_method=code,
        ).aggregate(total=Coalesce(Sum("amount"), Decimal("0.00")))["total"]

        m_refunds = txns.filter(
            transaction_type=PaymentTransaction.TYPE_REFUND,
            payment_method=code,
        ).aggregate(total=Coalesce(Sum("amount"), Decimal("0.00")))["total"]

        m_net = m_payments - m_refunds
        if m_payments > 0 or m_refunds > 0:
            method_data[label] = {
                "payments": m_payments,
                "refunds": m_refunds,
                "net": m_net,
            }

    # Order type breakdown (Dine-in vs Takeaway)
    type_data = {
        "Dine In": {"count": 0, "net_sales": Decimal("0.00")},
        "Takeaway": {"count": 0, "net_sales": Decimal("0.00")},
    }
    for ord_obj in orders_qs:
        key = "Dine In" if ord_obj.order_type == "DINE_IN" else "Takeaway"
        type_data[key]["count"] += 1
        type_data[key]["net_sales"] += ord_obj.subtotal - ord_obj.discount_amount

    return {
        "gross_collections": gross_collections,
        "refunds": refunds,
        "net_collections": net_collections,
        "net_sales": net_sales,
        "vat_amount": vat_amount,
        "service_charge": service_charge,
        "order_count": order_count,
        "average_order_value": average_order_value,
        "payment_method_breakdown": method_data,
        "order_type_breakdown": type_data,
        "transactions": txns.order_by("-transaction_at")[:50],
    }


def get_item_sales_report_data(restaurant, start_datetime, end_datetime, branch=None):
    """
    Generate Item Sales Report data for paid orders within date range.
    Includes quantity sold, sales amount, historical food cost, and margins.
    """
    # Get paid orders settled in this period
    paid_order_ids = (
        PaymentTransaction.objects.filter(
            restaurant=restaurant,
            order__restaurant=restaurant,
            **({"branch": branch, "order__branch": branch} if branch is not None else {}),
            transaction_type=PaymentTransaction.TYPE_PAYMENT,
            transaction_at__gte=start_datetime,
            transaction_at__lte=end_datetime,
        )
        .exclude(order__status=Order.STATUS_CANCELLED)
        .values_list("order_id", flat=True)
        .distinct()
    )

    items_qs = (
        OrderItem.objects.filter(
            order_id__in=paid_order_ids,
            order__restaurant=restaurant,
        )
        .select_related("menu_item", "menu_item__category")
    )

    item_map = {}
    category_summary = {}

    for item in items_qs:
        m_id = item.menu_item_id
        m_item = item.menu_item
        cat_name = m_item.category.name if m_item.category else "Uncategorized"

        if m_id not in item_map:
            # Determine estimated food cost per portion from recipe
            food_cost_per_portion = Decimal("0.00")
            if hasattr(m_item, "recipe") and m_item.recipe:
                food_cost_per_portion = m_item.recipe.estimated_food_cost

            item_map[m_id] = {
                "name": m_item.name,
                "category": cat_name,
                "quantity": 0,
                "sales_amount": Decimal("0.00"),
                "food_cost_per_unit": food_cost_per_portion,
                "total_food_cost": Decimal("0.00"),
                "gross_margin": Decimal("0.00"),
                "margin_percent": Decimal("0.00"),
            }

        item_map[m_id]["quantity"] += item.quantity
        item_sales = getattr(item, "net_subtotal", item.price * item.quantity)
        item_map[m_id]["sales_amount"] += item_sales
        total_fc = item_map[m_id]["food_cost_per_unit"] * item.quantity
        item_map[m_id]["total_food_cost"] += total_fc

    # Calculate margins and category breakdown
    total_sales = Decimal("0.00")
    total_cost = Decimal("0.00")

    item_list = list(item_map.values())
    for it in item_list:
        it["gross_margin"] = normalize_zero_money(it["sales_amount"] - it["total_food_cost"])
        if it["sales_amount"] > Decimal("0.00"):
            it["margin_percent"] = (
                (it["gross_margin"] / it["sales_amount"]) * Decimal("100")
            ).quantize(Decimal("0.1"))
        else:
            it["margin_percent"] = Decimal("0.0")

        total_sales += it["sales_amount"]
        total_cost += it["total_food_cost"]

        cat = it["category"]
        if cat not in category_summary:
            category_summary[cat] = {
                "quantity": 0,
                "sales_amount": Decimal("0.00"),
                "food_cost": Decimal("0.00"),
                "gross_margin": Decimal("0.00"),
                "margin_percent": Decimal("0.0"),
            }
        category_summary[cat]["quantity"] += it["quantity"]
        category_summary[cat]["sales_amount"] += it["sales_amount"]
        category_summary[cat]["food_cost"] += it["total_food_cost"]

    # Compute category gross margins: category sales - category historical food cost
    for cat_name, cdata in category_summary.items():
        cdata["gross_margin"] = normalize_zero_money(cdata["sales_amount"] - cdata["food_cost"])
        if cdata["sales_amount"] > Decimal("0.00"):
            cdata["margin_percent"] = (
                (cdata["gross_margin"] / cdata["sales_amount"]) * Decimal("100")
            ).quantize(Decimal("0.1"))
        else:
            cdata["margin_percent"] = Decimal("0.0")

    item_list.sort(key=lambda x: x["sales_amount"], reverse=True)

    return {
        "items": item_list,
        "total_quantity": sum(it["quantity"] for it in item_list),
        "total_sales": normalize_zero_money(total_sales),
        "total_food_cost": normalize_zero_money(total_cost),
        "total_gross_margin": normalize_zero_money(total_sales - total_cost),
        "category_summary": category_summary,
    }


def get_stock_report_data(restaurant, start_datetime, end_datetime, branch=None):
    """
    Generate Stock Valuation and Movement Report for the restaurant.
    """
    balances = BranchIngredientStock.objects.filter(
        branch__restaurant=restaurant,
        ingredient__restaurant=restaurant,
        ingredient__is_active=True,
    ).select_related("branch", "ingredient", "ingredient__category", "ingredient__storage_location")
    if branch is not None:
        balances = balances.filter(branch=branch)

    stock_items = []
    total_inventory_valuation = Decimal("0.00")
    low_stock_count = 0

    for balance in balances:
        ing = balance.ingredient
        current_val = (balance.current_stock * ing.current_unit_cost).quantize(Decimal("0.01"))
        total_inventory_valuation += current_val
        is_low = balance.available_stock <= balance.min_stock_alert
        if is_low:
            low_stock_count += 1
        stock_items.append({
            "ingredient": ing,
            "branch": balance.branch,
            "current_stock": balance.current_stock,
            "reserved_stock": balance.reserved_stock,
            "available_stock": balance.available_stock,
            "unit_cost": ing.current_unit_cost,
            "valuation": current_val,
            "is_low": is_low,
            "status": balance.stock_status,
        })

    # Movement summary in range - grouped by incompatible unit types (G, ML, PCS)
    movements = (
        StockTransaction.objects.filter(
            ingredient__restaurant=restaurant,
            created_at__gte=start_datetime,
            created_at__lte=end_datetime,
        )
        .select_related("ingredient")
    )

    if branch is not None:
        movements = movements.filter(branch=branch)

    def _format_unit_qty(qty, unit):
        if qty == qty.to_integral():
            num_str = f"{int(qty):,}"
        else:
            num_str = f"{qty:,.3f}".rstrip("0").rstrip(".")
        return f"{num_str} {unit}"

    def _group_by_unit(tx_qs):
        unit_map = {}
        count = 0
        for tx in tx_qs:
            count += 1
            u = tx.ingredient.base_unit
            unit_map[u] = unit_map.get(u, Decimal("0.000")) + tx.quantity

        formatted_list = [
            _format_unit_qty(q, u)
            for u, q in sorted(unit_map.items())
        ]
        return {
            "count": count,
            "units": unit_map,
            "formatted": formatted_list,
        }

    opening_data = _group_by_unit(
        movements.filter(transaction_type=StockTransaction.TransactionType.OPENING_BALANCE)
    )
    purchase_data = _group_by_unit(
        movements.filter(transaction_type=StockTransaction.TransactionType.PURCHASE)
    )
    consumed_data = _group_by_unit(
        movements.filter(transaction_type=StockTransaction.TransactionType.CONSUMPTION)
    )
    wasted_data = _group_by_unit(
        movements.filter(transaction_type=StockTransaction.TransactionType.WASTE)
    )

    adj_in_data = _group_by_unit(
        movements.filter(transaction_type=StockTransaction.TransactionType.ADJUSTMENT_IN)
    )
    adj_out_data = _group_by_unit(
        movements.filter(transaction_type=StockTransaction.TransactionType.ADJUSTMENT_OUT)
    )

    all_adj_units = sorted(set(adj_in_data["units"].keys()) | set(adj_out_data["units"].keys()))
    adj_formatted = []
    adj_net_units = {}
    for u in all_adj_units:
        in_q = adj_in_data["units"].get(u, Decimal("0.000"))
        out_q = adj_out_data["units"].get(u, Decimal("0.000"))
        net_q = in_q - out_q
        adj_net_units[u] = net_q
        if net_q > 0:
            adj_formatted.append(f"+{_format_unit_qty(net_q, u)}")
        elif net_q < 0:
            adj_formatted.append(f"-{_format_unit_qty(abs(net_q), u)}")
        else:
            adj_formatted.append(f"0 {u}")

    adjustments_data = {
        "count": adj_in_data["count"] + adj_out_data["count"],
        "in_count": adj_in_data["count"],
        "out_count": adj_out_data["count"],
        "units": adj_net_units,
        "in_units": adj_in_data["units"],
        "out_units": adj_out_data["units"],
        "formatted": adj_formatted,
    }

    return {
        "stock_items": stock_items,
        "total_valuation": total_inventory_valuation,
        "low_stock_count": low_stock_count,
        "total_items_count": len(stock_items),
        "movements_summary": {
            "opening_balance": opening_data,
            "purchases": purchase_data,
            "consumption": consumed_data,
            "waste": wasted_data,
            "adjustments": adjustments_data,
            "adjustments_in": adj_in_data,
            "adjustments_out": adj_out_data,
        },
    }


def get_purchase_report_data(restaurant, start_date, end_date, branch=None):
    """
    Generate Purchase Report for RECEIVED Purchase Orders in date range.
    """
    received_pos = (
        PurchaseOrder.objects.filter(
            restaurant=restaurant,
            status=PurchaseOrder.Status.RECEIVED,
            purchase_date__gte=start_date,
            purchase_date__lte=end_date,
        )
        .select_related("supplier", "received_by")
        .prefetch_related("items", "items__ingredient")
    )

    if branch is not None:
        received_pos = received_pos.filter(branch=branch)

    total_spend = received_pos.aggregate(
        total=Coalesce(Sum("total_amount"), Decimal("0.00"))
    )["total"]

    # Supplier spend breakdown
    supplier_map = {}
    for po in received_pos:
        s_name = po.supplier.name if po.supplier else "Direct / Unassigned"
        if s_name not in supplier_map:
            supplier_map[s_name] = {
                "po_count": 0,
                "total_spend": Decimal("0.00"),
            }
        supplier_map[s_name]["po_count"] += 1
        supplier_map[s_name]["total_spend"] += po.total_amount

    # Ingredient spend breakdown
    po_items = PurchaseOrderItem.objects.filter(
        purchase_order__in=received_pos
    ).select_related("ingredient")

    ingredient_spend = {}
    for item in po_items:
        i_name = item.ingredient.name
        if i_name not in ingredient_spend:
            ingredient_spend[i_name] = {
                "quantity": Decimal("0.00"),
                "base_unit": item.ingredient.base_unit,
                "total_spend": Decimal("0.00"),
                "latest_pack_price": item.pack_price,
            }
        ingredient_spend[i_name]["quantity"] += item.pack_quantity
        ingredient_spend[i_name]["total_spend"] += item.total_price

    return {
        "purchase_orders": received_pos,
        "total_orders_count": received_pos.count(),
        "total_spend": total_spend,
        "supplier_breakdown": supplier_map,
        "ingredient_breakdown": ingredient_spend,
    }


def get_profit_and_loss_data(restaurant, start_date, end_date, start_datetime, end_datetime, branch=None):
    """
    Generate Profit & Loss (P&L) Statement according to real-world restaurant accounting rules:
      Net Sales
    - COGS (Historical Consumption)
    - Waste Loss (Inventory Loss)
    = Gross Profit
    - Paid Staff Payroll Cost
    - General Operating Expenses
    = Operating Profit (EBIT)

    CRITICAL INTEGRITY RULES:
    1. Raw inventory purchases (PurchaseOrder) are NEVER added to P&L expenses.
    2. TableSession totals and Order totals are NEVER double-counted.
    3. Salary advances and gross payroll are NEVER double-counted.
    """
    # 1. Net Sales from Sales Report
    sales_data = get_sales_report_data(restaurant, start_datetime, end_datetime, branch=branch)
    net_sales = sales_data["net_sales"]

    # 2. Historical COGS: Sum of StockTransaction of type CONSUMPTION in range * unit_cost_snapshot
    consumption_txns = StockTransaction.objects.filter(
        ingredient__restaurant=restaurant,
        transaction_type=StockTransaction.TransactionType.CONSUMPTION,
        created_at__gte=start_datetime,
        created_at__lte=end_datetime,
    )

    if branch is not None:
        consumption_txns = consumption_txns.filter(branch=branch)

    cogs = Decimal("0.00")
    for ctxn in consumption_txns:
        # Uses the snapshotted unit cost at the time of consumption
        cost = (ctxn.quantity * ctxn.unit_cost_snapshot).quantize(Decimal("0.01"))
        cogs += cost

    # 3. Measurable Waste Loss
    waste_records = WasteRecord.objects.filter(
        ingredient__restaurant=restaurant,
        created_at__gte=start_datetime,
        created_at__lte=end_datetime,
    )

    if branch is not None:
        waste_records = waste_records.filter(branch=branch)

    waste_loss = Decimal("0.00")
    for w in waste_records:
        cost = (w.quantity * w.unit_cost_snapshot).quantize(Decimal("0.01"))
        waste_loss += cost

    # Gross Profit
    gross_profit = normalize_zero_money(net_sales - cogs - waste_loss)
    gross_margin_percent = (
        ((gross_profit / net_sales) * Decimal("100")).quantize(Decimal("0.1"))
        if net_sales > Decimal("0.00")
        else Decimal("0.0")
    )

    # 4. Paid Staff Payroll Cost (strictly records where status='paid' and paid_at within range)
    paid_payroll_records = PayrollRecord.objects.filter(
        restaurant=restaurant,
        status="paid",
        paid_at__gte=start_datetime,
        paid_at__lte=end_datetime,
    )
    if branch is not None:
        paid_payroll_records = paid_payroll_records.filter(branch=branch)

    payroll_cost = paid_payroll_records.aggregate(
        total=Coalesce(Sum("net_salary"), Decimal("0.00"))
    )["total"]

    # 5. General Operating Expenses (from Expense model in date range)
    expenses = Expense.objects.filter(
        restaurant=restaurant,
        expense_date__gte=start_date,
        expense_date__lte=end_date,
    ).select_related("category")

    if branch is not None:
        expenses = expenses.filter(branch=branch)

    operating_expenses = expenses.aggregate(
        total=Coalesce(Sum("amount"), Decimal("0.00"))
    )["total"]

    # Category-wise expense breakdown
    expense_category_breakdown = {}
    for exp in expenses:
        cat_name = exp.category.name
        expense_category_breakdown[cat_name] = (
            expense_category_breakdown.get(cat_name, Decimal("0.00")) + exp.amount
        )

    # 6. Operating Profit
    operating_profit = normalize_zero_money(gross_profit - payroll_cost - operating_expenses)
    operating_margin_percent = (
        ((operating_profit / net_sales) * Decimal("100")).quantize(Decimal("0.1"))
        if net_sales > Decimal("0.00")
        else Decimal("0.0")
    )

    return {
        "net_sales": net_sales,
        "gross_collections": sales_data["gross_collections"],
        "refunds": sales_data["refunds"],
        "cogs": cogs,
        "waste_loss": waste_loss,
        "total_cogs_and_waste": cogs + waste_loss,
        "gross_profit": gross_profit,
        "gross_margin_percent": gross_margin_percent,
        "payroll_cost": payroll_cost,
        "operating_expenses": operating_expenses,
        "expense_category_breakdown": expense_category_breakdown,
        "total_operating_costs": payroll_cost + operating_expenses,
        "operating_profit": operating_profit,
        "operating_margin_percent": operating_margin_percent,
        "paid_orders_count": sales_data["order_count"],
    }
