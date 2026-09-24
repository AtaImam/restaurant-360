"""Execute the rendered polling script using persisted backend status payloads."""

import json
import re
import shutil
import subprocess
from datetime import timedelta
from unittest import skipUnless

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from orders.models import Order
from orders.services import transition_order_status
from orders.test_flow import OrderFlowFixture


@skipUnless(shutil.which("node"), "Node is required to execute the customer polling script.")
class TrackingClientTests(OrderFlowFixture, TestCase):
    def test_polling_renders_database_states_retries_errors_and_never_advances_from_time(self):
        order = self.create_order()
        Order.objects.filter(pk=order.pk).update(
            created_at=timezone.now() - timedelta(hours=2),
            payment_status="PAID",
        )
        order.refresh_from_db()
        page = self.client.get(reverse("order_success", args=[order.pk])).content.decode()
        script = re.findall(r"<script>(.*?)</script>", page, flags=re.S)[-1]
        endpoint = reverse("order_status_api", args=[order.pk])
        initial = self.client.get(endpoint).json()
        payloads = [initial, initial]
        for status in ["ACCEPTED", "PREPARING", "READY", "SERVED", "COMPLETED"]:
            transition_order_status(order, status)
            payload = self.client.get(endpoint).json()
            payloads.append(payload)
            if status == "PREPARING":
                self.assertIsNone(payload["remaining_minutes"])
                self.assertEqual(payload["status"], "PREPARING")
                payloads.append(payload)

        harness = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const elements = new Map();
function element(id) {
    if (!elements.has(id)) {
        const classes = new Set();
        elements.set(id, {textContent: '', style: {}, classList: {
            add: (...items) => items.forEach(item => classes.add(item)),
            remove: (...items) => items.forEach(item => classes.delete(item)),
            contains: item => classes.has(item),
        }});
    }
    return elements.get(id);
}
element('initialOrderStatus').textContent = JSON.stringify(input.initial);
const steps = Array.from({length: 6}, (_, index) => element('step' + index));
const timers = [];
let index = 0;
let failNext = false;
const context = {
    document: {getElementById: element, querySelectorAll: () => steps},
    window: {setTimeout: (callback, delay) => timers.push({callback, delay})},
    fetch: async (url, options) => {
        assert.equal(url, input.endpoint);
        assert.equal(options.cache, 'no-store');
        assert.equal(options.method, undefined);  // Default GET, no status mutation.
        assert.equal(options.body, undefined);
        if (failNext) { failNext = false; throw new Error('temporary connection loss'); }
        return {ok: true, json: async () => input.payloads[index++]};
    },
};
function assertRendered(payload) {
    assert.equal(element('statusTitle').textContent, payload.title);
    assert.equal(element('statusBadge').textContent, payload.status_label);
    assert.equal(element('statusMessage').textContent, payload.message);
    assert.equal(element('progressFill').style.width, payload.progress + '%');
    assert.equal(element('etaBox').classList.contains('visible'), payload.remaining_minutes !== null);
    steps.forEach((step, i) => assert.equal(step.classList.contains('active'), i === payload.stage_index));
}
(async () => {
    vm.runInNewContext(input.script, context);
    assertRendered(input.initial);
    await new Promise(resolve => setImmediate(resolve));
    assertRendered(input.payloads[0]);
    failNext = true;
    await timers.shift().callback();
    assert.equal(element('connectionText').textContent, 'Reconnecting...');
    assert.equal(timers[0].delay, 5000);
    assertRendered(input.payloads[0]);
    while (index < input.payloads.length) {
        const timer = timers.shift();
        assert.ok(timer);
        await timer.callback();
        assertRendered(input.payloads[index - 1]);
        assert.equal(element('connectionText').textContent, 'Live');
    }
    assert.equal(timers.length, 0);  // Polling stops only after DB says COMPLETED.
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", harness],
            input=json.dumps({"script": script, "initial": initial, "payloads": payloads, "endpoint": endpoint}),
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
