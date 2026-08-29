from django.shortcuts import render, get_object_or_404, redirect
from restaurant.models import Restaurant, Table
from .models import Category, MenuItem
from orders.models import Order, OrderItem
from orders.models import Order, OrderItem

def customer_menu(request, restaurant_id, table_id):
    restaurant = get_object_or_404(Restaurant, id=restaurant_id)
    table = get_object_or_404(
        Table,
        id=table_id,

        restaurant=restaurant
    )

    categories = Category.objects.filter(
        restaurant=restaurant
    ).prefetch_related('items')

    context = {
        'restaurant': restaurant,
        'table': table,
        'categories': categories,
    }

    return render(request, 'customer/menu.html', context)


def add_to_cart(request, restaurant_id, table_id, item_id):
    item = get_object_or_404(MenuItem, id=item_id)

    cart = request.session.get('cart', {})

    item_key = str(item.id)

    if item_key in cart:
        cart[item_key]['quantity'] += 1
    else:
        cart[item_key] = {
            'name': item.name,
            'price': float(item.price),
            'quantity': 1,
        }

    request.session['cart'] = cart

    return redirect(
        'customer_menu',
        restaurant_id=restaurant_id,
        table_id=table_id
    )
def cart_view(request, restaurant_id, table_id):
    restaurant = get_object_or_404(Restaurant, id=restaurant_id)
    table = get_object_or_404(
        Table,
        id=table_id,
        restaurant=restaurant
    )

    cart = request.session.get('cart', {})

    total = 0

    for item in cart.values():
        item['subtotal'] = item['price'] * item['quantity']
        total += item['subtotal']

    context = {
        'restaurant': restaurant,
        'table': table,
        'cart': cart,
        'total': total,
    }

    return render(request, 'customer/cart.html', context)
def increase_cart_item(request, restaurant_id, table_id, item_id):
    cart = request.session.get('cart', {})
    item_key = str(item_id)

    if item_key in cart:
        cart[item_key]['quantity'] += 1

    request.session['cart'] = cart

    return redirect(
        'cart',
        restaurant_id=restaurant_id,
        table_id=table_id
    )


def decrease_cart_item(request, restaurant_id, table_id, item_id):
    cart = request.session.get('cart', {})
    item_key = str(item_id)

    if item_key in cart:
        cart[item_key]['quantity'] -= 1

        if cart[item_key]['quantity'] <= 0:
            del cart[item_key]

    request.session['cart'] = cart

    return redirect(
        'cart',
        restaurant_id=restaurant_id,
        table_id=table_id
    )


def remove_cart_item(request, restaurant_id, table_id, item_id):
    cart = request.session.get('cart', {})
    item_key = str(item_id)

    if item_key in cart:
        del cart[item_key]

    request.session['cart'] = cart

    return redirect(
        'cart',
        restaurant_id=restaurant_id,
        table_id=table_id
    )
def checkout(request, restaurant_id, table_id):
    restaurant = get_object_or_404(Restaurant, id=restaurant_id)

    table = get_object_or_404(
        Table,
        id=table_id,
        restaurant=restaurant
    )

    cart = request.session.get('cart', {})

    if not cart:
        return redirect(
            'cart',
            restaurant_id=restaurant_id,
            table_id=table_id
        )

    total = 0

    for item in cart.values():
        item['subtotal'] = item['price'] * item['quantity']
        total += item['subtotal']

    if request.method == 'POST':
        order_type = request.POST.get('order_type')

        order = Order.objects.create(
            restaurant=restaurant,
            table=table,
            order_type=order_type,
            total_amount=total
        )

        for item_id, item_data in cart.items():
            menu_item = get_object_or_404(
                MenuItem,
                id=item_id
            )

            OrderItem.objects.create(
                order=order,
                menu_item=menu_item,
                quantity=item_data['quantity'],
                price=item_data['price']
            )

        request.session['cart'] = {}

        return redirect(
            'order_success',
            order_id=order.id
        )

    return render(
        request,
        'customer/checkout.html',
        {
            'restaurant': restaurant,
            'table': table,
            'cart': cart,
            'total': total,
        }
    )
def order_success(request, order_id):
    order = get_object_or_404(Order, id=order_id)

    return render(
        request,
        'customer/order_success.html',
        {'order': order}
    )