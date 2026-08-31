#!/usr/bin/env python3
"""
Restaurant 360 - Shared Owner Sidebar Installer

Run from the project root:
    python sync_owner_sidebar.py

What it does:
1. Creates templates/dashboard/_owner_sidebar.html
2. Replaces the old sidebar in owner/back-office templates with one shared include
3. Adds the shared sidebar to pages that currently have no sidebar
4. Creates a .bak backup before changing each existing template

It does NOT touch models, migrations, views, URLs, or the database.
"""

from pathlib import Path
import re
import shutil
import sys

PROJECT_ROOT = Path(__file__).resolve().parent
TEMPLATES = PROJECT_ROOT / "templates"

SIDEBAR_CONTENT = '{# Shared Restaurant 360 owner/back-office sidebar #}\n<style>\n  .r360-sidebar {\n    width: 260px;\n    height: 100vh;\n    position: fixed;\n    inset: 0 auto 0 0;\n    z-index: 1200;\n    overflow-y: auto;\n    background: #111827;\n    color: #fff;\n    font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;\n  }\n\n  .r360-sidebar * {\n    box-sizing: border-box;\n  }\n\n  .r360-sidebar::-webkit-scrollbar {\n    width: 5px;\n  }\n\n  .r360-sidebar::-webkit-scrollbar-thumb {\n    background: #374151;\n    border-radius: 10px;\n  }\n\n  .r360-brand {\n    height: 78px;\n    display: flex;\n    align-items: center;\n    gap: 12px;\n    padding: 0 20px;\n    border-bottom: 1px solid #263244;\n  }\n\n  .r360-brand-logo {\n    width: 42px;\n    height: 42px;\n    flex: 0 0 42px;\n    display: flex;\n    align-items: center;\n    justify-content: center;\n    border-radius: 12px;\n    background: #f97316;\n    color: #fff;\n    font-size: 21px;\n    font-weight: 800;\n  }\n\n  .r360-brand h2 {\n    margin: 0;\n    font-size: 17px;\n    line-height: 1.2;\n    color: #fff;\n  }\n\n  .r360-brand p {\n    margin: 4px 0 0;\n    color: #9ca3af;\n    font-size: 10px;\n  }\n\n  .r360-search-wrap {\n    position: relative;\n    padding: 14px 14px 4px;\n  }\n\n  .r360-search-icon {\n    position: absolute;\n    left: 26px;\n    top: 25px;\n    color: #9ca3af;\n    font-size: 14px;\n    pointer-events: none;\n  }\n\n  .r360-search {\n    width: 100%;\n    height: 38px;\n    padding: 0 12px 0 36px;\n    border: 1px solid #374151;\n    border-radius: 9px;\n    outline: none;\n    background: #182233;\n    color: #fff;\n    font-size: 11px;\n  }\n\n  .r360-search::placeholder {\n    color: #9ca3af;\n  }\n\n  .r360-search:focus {\n    border-color: #f97316;\n    box-shadow: 0 0 0 3px rgba(249, 115, 22, 0.12);\n  }\n\n  .r360-menu {\n    padding: 14px 14px 30px;\n  }\n\n  .r360-section {\n    margin-bottom: 18px;\n  }\n\n  .r360-title {\n    padding: 0 12px;\n    margin-bottom: 7px;\n    color: #6b7280;\n    font-size: 10px;\n    font-weight: 700;\n    letter-spacing: 1px;\n  }\n\n  .r360-link,\n  .r360-toggle,\n  .r360-sub-link {\n    text-decoration: none !important;\n  }\n\n  .r360-link,\n  .r360-toggle {\n    width: 100%;\n    min-height: 42px;\n    display: flex;\n    align-items: center;\n    gap: 12px;\n    padding: 10px 12px;\n    margin-bottom: 3px;\n    border: 0;\n    border-radius: 8px;\n    background: transparent;\n    color: #d1d5db;\n    text-align: left;\n    font: inherit;\n    font-size: 12px;\n    cursor: pointer;\n    transition: 0.18s ease;\n  }\n\n  .r360-link:hover,\n  .r360-toggle:hover,\n  .r360-group.r360-open > .r360-toggle {\n    background: #1f2937;\n    color: #fff;\n  }\n\n  .r360-link.r360-active {\n    background: #f97316;\n    color: #fff;\n    font-weight: 700;\n  }\n\n  .r360-icon {\n    width: 22px;\n    flex: 0 0 22px;\n    display: inline-flex;\n    justify-content: center;\n    align-items: center;\n    font-size: 15px;\n  }\n\n  .r360-text {\n    min-width: 0;\n    flex: 1;\n  }\n\n  .r360-chevron {\n    margin-left: auto;\n    color: #9ca3af;\n    font-size: 14px;\n    transition: transform 0.18s ease;\n  }\n\n  .r360-group.r360-open .r360-chevron {\n    transform: rotate(180deg);\n  }\n\n  .r360-group.r360-has-active > .r360-toggle {\n    color: #fff;\n  }\n\n  .r360-group.r360-has-active > .r360-toggle .r360-icon {\n    color: #fb923c;\n  }\n\n  .r360-submenu {\n    display: none;\n    margin: 2px 0 8px 23px;\n    padding: 3px 0 3px 11px;\n    border-left: 1px solid #374151;\n  }\n\n  .r360-group.r360-open > .r360-submenu {\n    display: block;\n  }\n\n  .r360-sub-link {\n    min-height: 36px;\n    display: flex;\n    align-items: center;\n    gap: 9px;\n    padding: 8px 9px;\n    margin-bottom: 2px;\n    border-radius: 7px;\n    color: #b8c0cc;\n    font-size: 11px;\n    transition: 0.18s ease;\n  }\n\n  .r360-sub-link:hover {\n    background: #1f2937;\n    color: #fff;\n  }\n\n  .r360-sub-link.r360-active {\n    background: rgba(249, 115, 22, 0.14);\n    color: #ffb27a;\n    font-weight: 700;\n  }\n\n  .r360-sub-icon {\n    width: 18px;\n    flex: 0 0 18px;\n    display: inline-flex;\n    justify-content: center;\n  }\n\n  .r360-hidden {\n    display: none !important;\n  }\n\n  .r360-sidebar-overlay {\n    display: none;\n    position: fixed;\n    inset: 0;\n    z-index: 1190;\n    background: rgba(17, 24, 39, 0.52);\n  }\n\n  /* Make existing Restaurant 360 pages line up with the shared sidebar. */\n  .r360-sidebar ~ .main {\n    margin-left: 260px !important;\n  }\n\n  .r360-sidebar ~ .content {\n    width: auto !important;\n    max-width: none !important;\n    margin-left: 290px !important;\n    margin-right: 30px !important;\n  }\n\n  @media (max-width: 950px) {\n    .r360-sidebar {\n      transform: translateX(-100%);\n      transition: transform 0.23s ease;\n    }\n\n    .r360-sidebar.r360-mobile-open {\n      transform: translateX(0);\n    }\n\n    .r360-sidebar-overlay.r360-show {\n      display: block;\n    }\n\n    .r360-sidebar ~ .main {\n      margin-left: 0 !important;\n    }\n\n    .r360-sidebar ~ .content {\n      margin-left: 18px !important;\n      margin-right: 18px !important;\n    }\n  }\n</style>\n\n<aside class="r360-sidebar" id="r360Sidebar">\n  <div class="r360-brand">\n    <div class="r360-brand-logo">R</div>\n    <div>\n      <h2>Restaurant 360</h2>\n      <p>Restaurant Management</p>\n    </div>\n  </div>\n\n  <div class="r360-search-wrap">\n    <span class="r360-search-icon">⌕</span>\n    <input\n      id="r360SidebarSearch"\n      class="r360-search"\n      type="text"\n      placeholder="Quick search"\n      autocomplete="off"\n    >\n  </div>\n\n  <nav class="r360-menu" id="r360SidebarMenu">\n    <div class="r360-section">\n      <a\n        href="{% url \'owner_dashboard\' %}"\n        class="r360-link r360-real-link r360-searchable"\n        data-label="dashboard"\n      >\n        <span class="r360-icon">▦</span>\n        <span class="r360-text">Dashboard</span>\n      </a>\n    </div>\n\n    <div class="r360-section">\n      <div class="r360-title">OPERATIONS</div>\n\n      <a\n        href="{% url \'orders_list\' %}"\n        class="r360-link r360-real-link r360-searchable"\n        data-label="orders"\n      >\n        <span class="r360-icon">🧾</span>\n        <span class="r360-text">Orders</span>\n      </a>\n\n      <a href="#" class="r360-link r360-searchable" data-label="pos">\n        <span class="r360-icon">▦</span>\n        <span class="r360-text">POS</span>\n      </a>\n\n      <a href="#" class="r360-link r360-searchable" data-label="waiter">\n        <span class="r360-icon">👨\u200d🍳</span>\n        <span class="r360-text">Waiter</span>\n      </a>\n\n      <a\n        href="{% url \'kitchen_dashboard\' %}"\n        class="r360-link r360-real-link r360-searchable"\n        data-label="kds kitchen display"\n      >\n        <span class="r360-icon">🍳</span>\n        <span class="r360-text">KDS</span>\n      </a>\n\n      <a href="#" class="r360-link r360-searchable" data-label="display">\n        <span class="r360-icon">▣</span>\n        <span class="r360-text">Display</span>\n      </a>\n    </div>\n\n    <div class="r360-section">\n      <div class="r360-title">MENU &amp; BRANCHES</div>\n\n      <div class="r360-group" data-group="menu-management">\n        <button\n          type="button"\n          class="r360-toggle r360-searchable"\n          data-label="menu management"\n        >\n          <span class="r360-icon">◈</span>\n          <span class="r360-text">Menu Management</span>\n          <span class="r360-chevron">⌄</span>\n        </button>\n\n        <div class="r360-submenu">\n          <a\n            href="{% url \'category_list\' %}"\n            class="r360-sub-link r360-real-link r360-searchable"\n            data-label="menu categories categories"\n          >\n            <span class="r360-sub-icon">▤</span>\n            <span>Menu Categories</span>\n          </a>\n\n          <a href="#" class="r360-sub-link r360-searchable" data-label="menu items recipes">\n            <span class="r360-sub-icon">🍔</span>\n            <span>Menu Items</span>\n          </a>\n\n          <a href="#" class="r360-sub-link r360-searchable" data-label="addons">\n            <span class="r360-sub-icon">＋</span>\n            <span>Addons</span>\n          </a>\n\n          <a href="#" class="r360-sub-link r360-searchable" data-label="coupons offers">\n            <span class="r360-sub-icon">%</span>\n            <span>Coupons &amp; Offers</span>\n          </a>\n        </div>\n      </div>\n\n      <div class="r360-group" data-group="branch-management">\n        <button\n          type="button"\n          class="r360-toggle r360-searchable"\n          data-label="branch management"\n        >\n          <span class="r360-icon">🏢</span>\n          <span class="r360-text">Branch Management</span>\n          <span class="r360-chevron">⌄</span>\n        </button>\n\n        <div class="r360-submenu">\n          <a href="#" class="r360-sub-link r360-searchable" data-label="branches">\n            <span class="r360-sub-icon">🏢</span>\n            <span>Branches</span>\n          </a>\n\n          <a href="#" class="r360-sub-link r360-searchable" data-label="floors">\n            <span class="r360-sub-icon">▥</span>\n            <span>Floors</span>\n          </a>\n\n          <a href="#" class="r360-sub-link r360-searchable" data-label="tables qr">\n            <span class="r360-sub-icon">▣</span>\n            <span>Tables &amp; QR</span>\n          </a>\n        </div>\n      </div>\n    </div>\n\n    <div class="r360-section">\n      <div class="r360-title">GUESTS</div>\n\n      <a href="#" class="r360-link r360-searchable" data-label="customers guests">\n        <span class="r360-icon">👥</span>\n        <span class="r360-text">Customers</span>\n      </a>\n\n      <a href="#" class="r360-link r360-searchable" data-label="feedback">\n        <span class="r360-icon">★</span>\n        <span class="r360-text">Feedback</span>\n      </a>\n\n      <a href="#" class="r360-link r360-searchable" data-label="reservations">\n        <span class="r360-icon">📅</span>\n        <span class="r360-text">Reservations</span>\n      </a>\n    </div>\n\n    <div class="r360-section">\n      <div class="r360-title">STAFF</div>\n\n      <div class="r360-group" data-group="staff-management">\n        <button\n          type="button"\n          class="r360-toggle r360-searchable"\n          data-label="staff management team"\n        >\n          <span class="r360-icon">👤</span>\n          <span class="r360-text">Staff Management</span>\n          <span class="r360-chevron">⌄</span>\n        </button>\n\n        <div class="r360-submenu">\n          <a href="#" class="r360-sub-link r360-searchable" data-label="employees team">\n            <span class="r360-sub-icon">👤</span>\n            <span>Employees</span>\n          </a>\n\n          <a href="#" class="r360-sub-link r360-searchable" data-label="attendance">\n            <span class="r360-sub-icon">✓</span>\n            <span>Attendance</span>\n          </a>\n\n          <a href="#" class="r360-sub-link r360-searchable" data-label="leave">\n            <span class="r360-sub-icon">📆</span>\n            <span>Leave</span>\n          </a>\n\n          <a href="#" class="r360-sub-link r360-searchable" data-label="payroll salary">\n            <span class="r360-sub-icon">৳</span>\n            <span>Payroll</span>\n          </a>\n        </div>\n      </div>\n    </div>\n\n    <div class="r360-section">\n      <div class="r360-title">BACK OFFICE</div>\n\n      <div class="r360-group" data-group="inventory">\n        <button\n          type="button"\n          class="r360-toggle r360-searchable"\n          data-label="inventory"\n        >\n          <span class="r360-icon">📦</span>\n          <span class="r360-text">Inventory</span>\n          <span class="r360-chevron">⌄</span>\n        </button>\n\n        <div class="r360-submenu">\n          <a\n            href="{% url \'inventory_category_list\' %}"\n            class="r360-sub-link r360-real-link r360-searchable"\n            data-label="ingredient groups inventory categories"\n          >\n            <span class="r360-sub-icon">▤</span>\n            <span>Ingredient Groups</span>\n          </a>\n\n          <a\n            href="{% url \'ingredient_list\' %}"\n            class="r360-sub-link r360-real-link r360-searchable"\n            data-label="inventory items ingredients"\n          >\n            <span class="r360-sub-icon">📦</span>\n            <span>Inventory Items</span>\n          </a>\n\n          <a href="#" class="r360-sub-link r360-searchable" data-label="stock transactions ledger">\n            <span class="r360-sub-icon">▧</span>\n            <span>Stock Transactions</span>\n          </a>\n\n          <a href="#" class="r360-sub-link r360-searchable" data-label="wastage waste">\n            <span class="r360-sub-icon">🗑</span>\n            <span>Wastage</span>\n          </a>\n\n          <a href="#" class="r360-sub-link r360-searchable" data-label="suppliers">\n            <span class="r360-sub-icon">🚚</span>\n            <span>Suppliers</span>\n          </a>\n\n          <a href="#" class="r360-sub-link r360-searchable" data-label="purchase orders">\n            <span class="r360-sub-icon">🛒</span>\n            <span>Purchase Orders</span>\n          </a>\n        </div>\n      </div>\n\n      <div class="r360-group" data-group="expense-management">\n        <button\n          type="button"\n          class="r360-toggle r360-searchable"\n          data-label="expense management expenses"\n        >\n          <span class="r360-icon">💰</span>\n          <span class="r360-text">Expense Management</span>\n          <span class="r360-chevron">⌄</span>\n        </button>\n\n        <div class="r360-submenu">\n          <a href="#" class="r360-sub-link r360-searchable" data-label="expense categories">\n            <span class="r360-sub-icon">▤</span>\n            <span>Expense Categories</span>\n          </a>\n\n          <a href="#" class="r360-sub-link r360-searchable" data-label="expenses">\n            <span class="r360-sub-icon">৳</span>\n            <span>Expenses</span>\n          </a>\n        </div>\n      </div>\n\n      <div class="r360-group" data-group="reports">\n        <button\n          type="button"\n          class="r360-toggle r360-searchable"\n          data-label="reports analytics"\n        >\n          <span class="r360-icon">📊</span>\n          <span class="r360-text">Reports</span>\n          <span class="r360-chevron">⌄</span>\n        </button>\n\n        <div class="r360-submenu">\n          <a href="#" class="r360-sub-link r360-searchable" data-label="sales report">\n            <span class="r360-sub-icon">📊</span>\n            <span>Sales Report</span>\n          </a>\n\n          <a href="#" class="r360-sub-link r360-searchable" data-label="item sales report">\n            <span class="r360-sub-icon">📈</span>\n            <span>Item Sales</span>\n          </a>\n\n          <a href="#" class="r360-sub-link r360-searchable" data-label="stock report">\n            <span class="r360-sub-icon">📦</span>\n            <span>Stock Report</span>\n          </a>\n\n          <a href="#" class="r360-sub-link r360-searchable" data-label="purchase report">\n            <span class="r360-sub-icon">🛒</span>\n            <span>Purchase Report</span>\n          </a>\n\n          <a href="#" class="r360-sub-link r360-searchable" data-label="profit loss pnl">\n            <span class="r360-sub-icon">📉</span>\n            <span>Profit &amp; Loss</span>\n          </a>\n        </div>\n      </div>\n    </div>\n\n    <div class="r360-section">\n      <div class="r360-title">SYSTEM</div>\n\n      <a href="#" class="r360-link r360-searchable" data-label="support tickets">\n        <span class="r360-icon">◉</span>\n        <span class="r360-text">Support &amp; Tickets</span>\n      </a>\n\n      <a href="#" class="r360-link r360-searchable" data-label="settings">\n        <span class="r360-icon">⚙</span>\n        <span class="r360-text">Settings</span>\n      </a>\n    </div>\n  </nav>\n</aside>\n\n<div class="r360-sidebar-overlay" id="r360SidebarOverlay"></div>\n\n<script>\n  (function () {\n    function initR360Sidebar() {\n      const sidebar = document.getElementById("r360Sidebar");\n\n      if (!sidebar || sidebar.dataset.ready === "1") {\n        return;\n      }\n\n      sidebar.dataset.ready = "1";\n\n      const overlay = document.getElementById("r360SidebarOverlay");\n      const search = document.getElementById("r360SidebarSearch");\n      const groups = Array.from(sidebar.querySelectorAll(".r360-group"));\n      const toggles = Array.from(sidebar.querySelectorAll(".r360-toggle"));\n      const realLinks = Array.from(sidebar.querySelectorAll(".r360-real-link[href]"));\n\n      function setGroup(group, open, remember) {\n        group.classList.toggle("r360-open", open);\n\n        const toggle = group.querySelector(":scope > .r360-toggle");\n\n        if (toggle) {\n          toggle.setAttribute("aria-expanded", open ? "true" : "false");\n        }\n\n        if (remember !== false && group.dataset.group) {\n          localStorage.setItem(\n            "r360-sidebar-" + group.dataset.group,\n            open ? "open" : "closed"\n          );\n        }\n      }\n\n      groups.forEach(function (group) {\n        const saved = localStorage.getItem(\n          "r360-sidebar-" + group.dataset.group\n        );\n\n        setGroup(group, saved === "open", false);\n      });\n\n      toggles.forEach(function (toggle) {\n        toggle.addEventListener("click", function () {\n          const group = toggle.closest(".r360-group");\n          setGroup(group, !group.classList.contains("r360-open"), true);\n        });\n      });\n\n      const currentPath = window.location.pathname;\n      let best = null;\n      let bestLength = -1;\n\n      realLinks.forEach(function (link) {\n        const href = link.getAttribute("href");\n\n        if (!href || href === "#") {\n          return;\n        }\n\n        let match = currentPath === href;\n\n        if (!match && href !== "/" && href !== "/dashboard/") {\n          match = currentPath.startsWith(href);\n        }\n\n        if (match && href.length > bestLength) {\n          best = link;\n          bestLength = href.length;\n        }\n      });\n\n      sidebar.querySelectorAll(".r360-active").forEach(function (item) {\n        item.classList.remove("r360-active");\n      });\n\n      groups.forEach(function (group) {\n        group.classList.remove("r360-has-active");\n      });\n\n      if (best) {\n        best.classList.add("r360-active");\n\n        const group = best.closest(".r360-group");\n\n        if (group) {\n          group.classList.add("r360-has-active");\n          setGroup(group, true, false);\n        }\n      }\n\n      if (search) {\n        search.addEventListener("input", function () {\n          const query = search.value.trim().toLowerCase();\n\n          sidebar.querySelectorAll(".r360-searchable").forEach(function (item) {\n            item.classList.remove("r360-hidden");\n          });\n\n          if (!query) {\n            groups.forEach(function (group) {\n              const saved = localStorage.getItem(\n                "r360-sidebar-" + group.dataset.group\n              );\n\n              const active = group.classList.contains("r360-has-active");\n\n              setGroup(group, active || saved === "open", false);\n            });\n\n            return;\n          }\n\n          groups.forEach(function (group) {\n            let found = false;\n\n            group.querySelectorAll(".r360-sub-link").forEach(function (item) {\n              const label = (\n                item.dataset.label ||\n                item.textContent ||\n                ""\n              ).toLowerCase();\n\n              const match = label.includes(query);\n              item.classList.toggle("r360-hidden", !match);\n\n              if (match) {\n                found = true;\n              }\n            });\n\n            const toggle = group.querySelector(":scope > .r360-toggle");\n            const toggleText = toggle\n              ? (toggle.dataset.label || toggle.textContent || "").toLowerCase()\n              : "";\n\n            const toggleMatch = toggleText.includes(query);\n\n            group.classList.toggle(\n              "r360-hidden",\n              !found && !toggleMatch\n            );\n\n            if (found || toggleMatch) {\n              setGroup(group, true, false);\n            }\n          });\n\n          sidebar.querySelectorAll(".r360-link.r360-searchable").forEach(function (item) {\n            const label = (\n              item.dataset.label ||\n              item.textContent ||\n              ""\n            ).toLowerCase();\n\n            item.classList.toggle(\n              "r360-hidden",\n              !label.includes(query)\n            );\n          });\n        });\n      }\n\n      function closeMobile() {\n        sidebar.classList.remove("r360-mobile-open");\n\n        if (overlay) {\n          overlay.classList.remove("r360-show");\n        }\n      }\n\n      document.addEventListener("click", function (event) {\n        const mobileButton = event.target.closest(".mobile-menu-btn");\n\n        if (mobileButton) {\n          sidebar.classList.toggle("r360-mobile-open");\n\n          if (overlay) {\n            overlay.classList.toggle("r360-show");\n          }\n        }\n      });\n\n      if (overlay) {\n        overlay.addEventListener("click", closeMobile);\n      }\n    }\n\n    if (document.readyState === "loading") {\n      document.addEventListener("DOMContentLoaded", initR360Sidebar);\n    } else {\n      initR360Sidebar();\n    }\n  })();\n</script>\n'

TARGETS = [
    "dashboard/index.html",
    "dashboard/orders.html",
    "dashboard/order_detail.html",
    "dashboard/categories.html",
    "dashboard/category_form.html",
    "inventory/category_list.html",
    "inventory/category_form.html",
    "inventory/ingredient_list.html",
    "inventory/ingredient_form.html",
    "inventory/ingredient_detail.html",
    "kitchen/dashboard.html",
]

INCLUDE = '{% include "dashboard/_owner_sidebar.html" %}'

ASIDE_RE = re.compile(
    r'<aside\b(?=[^>]*class=["\'][^"\']*\bsidebar\b[^"\']*["\'])[^>]*>.*?</aside>',
    re.IGNORECASE | re.DOTALL,
)

OLD_OVERLAY_RE = re.compile(
    r'<div\b[^>]*class=["\'][^"\']*sidebar-overlay[^"\']*["\'][^>]*>\s*</div>',
    re.IGNORECASE | re.DOTALL,
)


def backup(path: Path):
    backup_path = path.with_suffix(path.suffix + ".bak")

    if not backup_path.exists():
        shutil.copy2(path, backup_path)

    return backup_path


def patch_template(path: Path):
    text = path.read_text(encoding="utf-8")

    if INCLUDE in text:
        return "already shared"

    original = text

    match = ASIDE_RE.search(text)

    if match:
        text = (
            text[:match.start()]
            + INCLUDE
            + text[match.end():]
        )

        text = OLD_OVERLAY_RE.sub("", text, count=1)

    else:
        body_match = re.search(
            r"<body(?:\s[^>]*)?>",
            text,
            flags=re.IGNORECASE,
        )

        if not body_match:
            return "skipped: no <body> found"

        insert_at = body_match.end()

        text = (
            text[:insert_at]
            + "\n\n    "
            + INCLUDE
            + "\n"
            + text[insert_at:]
        )

    if text == original:
        return "unchanged"

    backup(path)
    path.write_text(text, encoding="utf-8")

    return "updated"


def main():
    if not (PROJECT_ROOT / "manage.py").exists():
        print("ERROR: Put this file in the Restaurant 360 project root.")
        print("Expected manage.py beside sync_owner_sidebar.py")
        sys.exit(1)

    sidebar_path = TEMPLATES / "dashboard" / "_owner_sidebar.html"
    sidebar_path.parent.mkdir(parents=True, exist_ok=True)
    sidebar_path.write_text(SIDEBAR_CONTENT, encoding="utf-8")

    print(f"[created] {sidebar_path.relative_to(PROJECT_ROOT)}")
    print()

    changed = 0
    missing = 0

    for relative in TARGETS:
        path = TEMPLATES / relative

        if not path.exists():
            print(f"[missing] templates/{relative}")
            missing += 1
            continue

        result = patch_template(path)
        print(f"[{result}] templates/{relative}")

        if result == "updated":
            changed += 1

    print()
    print("Done.")
    print(f"Updated templates: {changed}")
    print(f"Missing templates: {missing}")
    print()
    print("Backups use the .html.bak extension.")
    print("Now run:")
    print("    python manage.py check")


if __name__ == "__main__":
    main()
