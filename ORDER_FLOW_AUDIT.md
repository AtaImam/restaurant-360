**Restaurant 360 — order flow audit, 2026-09-11**

The repository was inspected before editing, including all existing uncommitted
changes. Existing model additions, migrations, menu design, routes, POS structure,
and customer templates were preserved. No reset, revert, historical deletion, or
production migration was performed.

**Inventory timing currently implemented:** NEW reserves ingredients and reduces
available stock; ACCEPTED retains those reservations; PREPARING deducts physical
stock exactly once. READY, SERVED, and COMPLETED do not deduct again after a valid
preparation transition. The latest request explicitly confirms this timing.

**What was wrong and what changed**

Kitchen and dashboard endpoints saved Order.status directly without invoking
inventory consumption. The reservation service deleted ACTIVE rows and recreated
them, violating the unique order/ingredient constraint when RELEASED rows existed.
Consumption did not inspect existing ledger evidence. Kitchen POST retries also
computed a fresh next status, allowing the same submitted action to advance again.

The shared `transition_order_status()` service now validates the locked database
state and performs inventory effects in the same transaction. Kitchen submits an
explicit target and handles ACCEPTED/PREPARING/READY only; dashboard staff handle
acceptance, serving, and completion. Kitchen, order list/detail, and POS now allow
access without login or role restrictions, as requested. Signed-in staff retain
restaurant scope; anonymous order pages can access orders and anonymous POS uses
the existing first-restaurant fallback. The pre-existing owner dashboard login
requirement is unchanged. Transition and inventory logic are unchanged by this
access correction.

The existing inventory services now aggregate normal recipes, recipe yields,
component quantities, set menus, and shared ingredients. Requirements round upward
once after aggregation to the existing 0.001 stock precision. Reservation sync
updates/reuses the unique row, retains removed requirements as RELEASED, and rejects
re-reservation of consumed orders. Ingredient and order locks serialize competing
operations; SQLite uses BEGIN IMMEDIATE because SELECT FOR UPDATE is unavailable.

QR and POS checkout retain their existing atomic order/items/reservation blocks.
Both validate server prices, tenant ownership, quantities, menu availability, and
available ingredient stock. Invalid lines reject the whole order. Stock errors are
visible, failed QR carts are retained, and successful page refreshes do not create
new orders or consume stock. POS also rejects malformed JSON and nonfinite billing.

The tracking page and API share the same database status payload. The page renders
the actual initial status, polls using GET, retries connection failures, and stops
polling at COMPLETED. Time never advances an order. Expired estimates leave the
order Cooking and explain that it is taking longer than estimated.

Recipe edits preserve actual Ingredient foreign keys, existing visibility flags,
and batch yields. Set menus display component recipes and reject direct raw
ingredient recipes. Ingredient edits now lock stock and atomically save adjustments
and ledger/history, reject stale explicit adjustments, and avoid restoring consumed
stock when an older form only changes metadata.

Before the continuation request, the shared services, endpoint wiring, input
validation, recipe safeguards, tracking changes, and initial database tests were
already implemented. During continuation, the diff was reviewed again, the overdue
JavaScript test expectation was corrected, complete menu/cart and staff-page tests
were added, the rendered polling script and template JavaScript were executed or
syntax-checked, Order #6 was repaired and verified, and all checks were rerun.

**Verification results**

All **68 tests passed**, with **zero skips**, using a separate file-backed SQLite
test database. Its existing migrations were applied only to that disposable test
database. The live database was not used for test fixtures.

| Audit item | Result | Evidence |
| --- | --- | --- |
| Customer menu → cart → checkout → tracking | PASS | Actual Django routes, add/increase/decrease/remove, totals, checkout and refresh |
| Customer QR reservation | PASS | NEW has ACTIVE reservations and unchanged physical stock |
| POS reservation | PASS | Same stock service and full lifecycle verified |
| Normal recipe calculation | PASS | Recipe yields and shared ingredients aggregate correctly |
| Set menu recipe calculation | PASS | Component and order quantities aggregate; direct set recipe ignored/rejected |
| Stock availability check | PASS | Other orders' reservations reduce available quantities |
| Insufficient stock rollback | PASS | No partial order, items, reservations, or consumption |
| Invalid/zero/negative quantities | PASS | Entire invalid order rejected; QR/POS cases verified |
| NEW keeps physical stock | PASS | Available stock reduced by reservation only |
| ACCEPTED keeps reservation | PASS | Original reserved ingredient quantities retained |
| PREPARING consumes stock | PASS | Status, deduction, ledger, and reservation commit atomically |
| StockTransaction created | PASS | Exact quantity and order/ingredient association |
| Duplicate consumption protection | PASS | Repeated requests, stale instances, concurrent preparation |
| Concurrent order reservation | PASS | Two connections compete for one remaining portion; only one succeeds |
| READY no second deduction | PASS | Persisted quantities and ledger unchanged |
| SERVED no second deduction | PASS | Persisted quantities and ledger unchanged |
| COMPLETED no second deduction | PASS | Persisted quantities and ledger unchanged |
| Customer live tracking | PASS | All database states; rendered JS executed with DOM test doubles; reconnect/overdue/terminal cases |
| Waiter/dashboard/kitchen pages | PASS | Public kitchen/order pages and actions, signed-in tenant scope, allowed transitions |
| Recipe builder and ingredient edits | PASS | FK selection, set restrictions, yield/visibility, stale stock and rollback |
| Stale reservation reconciliation mechanism | PASS | Dry-run, repeat apply, matching/partial/duplicate/missing ledgers and per-order rollback |
| Ingredient FK integrity | PASS | Actual SQLite constraint enforcement and live PRAGMA foreign_key_check |
| All historical orders repaired | NEEDS REVIEW | Order #3 lacks reservations and consumption evidence |
| Interactive browser verification | UNAVAILABLE | In-app browser connection returned unavailable; not claimed as visually verified |

Commands run:

```sh
venv/bin/python manage.py check
venv/bin/python manage.py makemigrations --check --dry-run
venv/bin/python manage.py test orders.test_flow inventory.test_services --noinput
venv/bin/python manage.py test menu.test_tracking_client menu.test_workflow_edges --noinput
venv/bin/python manage.py reconcile_order_inventory --order-id 6
venv/bin/python manage.py reconcile_order_inventory --order-id 6 --apply
venv/bin/python manage.py reconcile_order_inventory --order-id 6 --apply
venv/bin/python manage.py reconcile_order_inventory
git diff --check
```

The complete suite, including both concurrent-connection tests, was run with:

```sh
venv/bin/python - <<'PY'
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from django.conf import settings
from django.core.management import call_command
settings.DATABASES["default"].setdefault("TEST", {})["NAME"] = "/private/tmp/restaurant360-flow-tests.sqlite3"
call_command("test", verbosity=2, interactive=False)
PY
```

Latest access-correction verification: **Ran 68 tests in 0.691s — OK**, including
the full anonymous QR/kitchen/order lifecycle, public POS, and repeated anonymous
preparation requests. Django system check: **0 issues**. The checks below also
passed during the original inventory implementation.
Migration drift check: **No changes detected**. Python `py_compile` passed for all
16 changed Python files; `git diff --check` passed. Rendered inline JavaScript from
customer, POS, dashboard, kitchen, and recipe pages passed Node syntax compilation.
The customer polling script additionally executed against real backend payloads
with simulated DOM/timers/network responses, including failed-request recovery.

**Live database repair**

A SQLite backup was saved before repair at
`/private/tmp/restaurant360-before-inventory-repair.sqlite3`.

Order #6 remains COMPLETED. Its 11 ACTIVE reservations became CONSUMED and exactly
11 matching CONSUMPTION transactions were created. The second apply reported
already consistent and made no changes. A direct read-only comparison with the
backup verified every deduction and preservation of all 6 orders, 13 order items,
25 reservation records, and 46 menu items.

| Ingredient | Before | After | Consumed |
| --- | ---: | ---: | ---: |
| BBQ Sauce (ML) | 1800 | 1765 | 35 |
| Capsicum (G) | 2000 | 1975 | 25 |
| Chicken Breast (G) | 5000 | 4900 | 100 |
| Mint Leaves (G) | 800 | 795 | 5 |
| Mozzarella Cheese (G) | 2500 | 2410 | 90 |
| Onion (G) | 5000 | 4975 | 25 |
| Pizza Dough (G) | 5000 | 4750 | 250 |
| Roasted Cumin (G) | 800 | 797 | 3 |
| Salt (G) | 3000 | 2998 | 2 |
| Sugar (G) | 5000 | 4992 | 8 |
| Yogurt (G) | 3000 | 2820 | 180 |

Order #5 retains 14 ACTIVE reservations at ACCEPTED. The full reconciliation preview
correctly exits nonzero for Order #3 (READY, no reservations or consumption history),
without modifying it. Original ingredient quantities cannot safely be reconstructed
from today's recipes. Early legacy orders #1, #2, and #4 also lack reservations;
the transition service reserves inventory before they next enter acceptance or
preparation, and rejects the action if their current recipes/stock cannot support it.

**Files changed or created by this work**

| File | Purpose |
| --- | --- |
| [config/settings.py](config/settings.py) | SQLite IMMEDIATE transactions and lock timeout |
| [orders/services.py](orders/services.py) | Central locked transitions and staff order scope |
| [inventory/services.py](inventory/services.py) | Requirements, stock checks, reservation sync/release, idempotent consumption |
| [inventory/management/commands/reconcile_order_inventory.py](inventory/management/commands/reconcile_order_inventory.py) | Safe historical preview and repair |
| [kitchen/views.py](kitchen/views.py) | Public kitchen access, explicit targets, shared service and errors |
| [dashboard/views.py](dashboard/views.py) | Shared transitions and scoped staff order views |
| [menu/views.py](menu/views.py) | Atomic validated QR checkout and real initial tracking data |
| [pos/views.py](pos/views.py) | Validated POS checkout and staff restaurant scope |
| [menu/tracking_views.py](menu/tracking_views.py) | Shared real status payload, GET API, approximate ETA handling |
| [menu/management_views.py](menu/management_views.py) | Recipe FK validation, set safeguards, batch yield/visibility preservation |
| [inventory/views.py](inventory/views.py) | Atomic locked stock edits and adjustment integrity |
| [templates/customer/checkout.html](templates/customer/checkout.html) | Visible checkout validation/stock errors |
| [templates/customer/order_success.html](templates/customer/order_success.html) | Real initial status and live polling |
| [templates/kitchen/dashboard.html](templates/kitchen/dashboard.html) | Explicit status buttons and visible errors |
| [templates/dashboard/order_detail.html](templates/dashboard/order_detail.html) | Visible transition errors |
| [templates/dashboard/recipe_builder.html](templates/dashboard/recipe_builder.html) | Set component display and batch-aware recipe metrics |
| [templates/inventory/ingredient_form.html](templates/inventory/ingredient_form.html) | Original stock snapshot for stale-edit protection |
| [orders/test_flow.py](orders/test_flow.py) | QR/POS, lifecycle, tracking, access and atomic failure tests |
| [orders/test_concurrency.py](orders/test_concurrency.py) | Competing checkout and preparation tests using separate connections |
| [inventory/test_services.py](inventory/test_services.py) | Recipe, reservation, ledger, FK and reconciliation tests |
| [menu/test_workflow_edges.py](menu/test_workflow_edges.py) | Full customer journey, staff templates, inputs, recipe and stock-edit regressions |
| [menu/test_tracking_client.py](menu/test_tracking_client.py) | Execute rendered tracking JavaScript against backend payloads |
| [ORDER_FLOW_AUDIT.md](ORDER_FLOW_AUDIT.md) | This verification report and reproducible commands |
| db.sqlite3 (untracked runtime data) | Authorized, verified Order #6 reconciliation only |

Existing changes to `config/urls.py`, `menu/urls.py`, `menu/models.py`,
`orders/models.py`, `inventory/models.py`, `templates/customer/menu.html`, and
pre-existing migrations/catalog assets were reviewed and retained. They were not
reverted or rewritten by this fix.

**Remaining limits**

The model has no CANCELLED/REJECTED state. Releasing unconsumed early reservations
is supported; a cancellation UI requires an explicit lifecycle/model decision.
Consumed food requires a deliberate waste/return/adjustment operation.

The model has no acceptance/preparation timestamps, so ETA remains explicitly
approximate. Menu/POS cards still use their existing availability flag; mandatory
server stock checks enforce actual availability at checkout. No interactive browser
visual check was possible. Tests cover the configured SQLite database; other
database engines were not available for verification.
