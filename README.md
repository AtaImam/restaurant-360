# Restaurant 360

Restaurant 360 is a Django-based restaurant management system with role-based access control, landing page, secure login, and dedicated interfaces for different staff roles.

## Features

- Modern landing page
- Secure login system
- Role-based dashboard flow
- Staff access control by role
- Kitchen and dashboard modules
- Restaurant and order management

## User Roles

- Admin
- Owner
- Manager
- Waiter
- Chef
- Kitchen Manager
- Bar Manager

## Default Login Credentials

| Role | Username | Password |
|------|----------|----------|
| Admin | admin | admin123 |
| Owner | owner | password123 |
| Manager | manager | password123 |
| Waiter | waiter | password123 |
| Chef | chief | password123 |
| Kitchen Manager | kitchenmgr | password123 |
| Bar Manager | barmgr | password123 |

## Run the Project

1. Open terminal in the project folder.
2. Install requirements:

```bash
pip install -r requirements.txt
```

3. Apply migrations:

```bash
python manage.py migrate
```

4. Start the server:

```bash
python manage.py runserver 0.0.0.0:8000
```

5. Open the app:

- http://localhost:8000/
- http://localhost:8000/auth/
- http://localhost:8000/auth/login/

## App Flow

- Landing page: `/auth/`
- Login page: `/auth/login/`
- Role redirect: `/auth/dashboard/`
- Logout: `/auth/logout/`

After login, the user is redirected to the correct dashboard based on role:

- Admin → Django admin or system dashboard
- Owner → `/dashboard/`
- Manager → `/dashboard/`
- Waiter → waiter dashboard
- Chief → kitchen dashboard
- Kitchen Manager → kitchen dashboard
- Bar Manager → bar dashboard

## Project Structure

```text
restaurant-360/
├── config/
├── dashboard/
├── kitchen/
├── menu/
├── orders/
├── restaurant/
├── templates/
├── users/
├── manage.py
├── requirements.txt
├── create_test_users.py
├── README.md
└── db.sqlite3
```

## Authentication Setup

The project uses a custom Django user model with a `role` field. Authentication and access rules are handled in:

- `users/models.py`
- `users/views.py`
- `users/decorators.py`
- `users/forms.py`
- `config/settings.py`

## Important Notes

- The root URL redirects to the auth landing page.
- Logout redirects to the login page.
- Landing page and login page do not auto-redirect authenticated users.
- Role-based access is enforced in protected views.

## Development Notes

To create test users:

```bash
python create_test_users.py
```

## Support

Use the app as a local development project for restaurant operations, kitchen task tracking, and staff role management.

## Phone-accessible table QR codes

`SITE_URL` is the public HTTP(S) origin encoded in every table QR image. It is
read from the process environment or `.env`; no interface/IP detection or
localhost fallback is used. Changing it does not change already printed codes.
If unset, tables can still be managed but QR generation requires configuration.

For a local demo on this Mac, set:

```dotenv
SITE_URL=http://Jisans-MacBook-Air-m4-3.local:8000
DJANGO_ALLOWED_HOSTS=Jisans-MacBook-Air-m4-3.local,localhost,127.0.0.1,testserver
```

On another machine, replace the hostname with its configured LAN DNS/mDNS name
(`scutil --get LocalHostName` on macOS gives the name before `.local`). The
example `restaurant-demo.local` works only if you configure that name on your
network. Keep the phone and server on the same network, allow incoming traffic
to port 8000, and disable Wi-Fi client isolation if the network enforces it.
Some networks/devices do not resolve mDNS; use a LAN DNS record or a stable
public HTTPS hostname in that case. For an immediate local demo, an explicitly
configured, phone-tested LAN IP in `SITE_URL` also works. Reserve that address
in DHCP for stability, or switch to DNS later and regenerate the images. The
application never guesses or hardcodes a LAN IP. A hostname is not created by setting
`SITE_URL`.

Restart the server after editing `.env`, listening on all interfaces:

```bash
python manage.py runserver 0.0.0.0:8000
python manage.py regenerate_qr_codes --dry-run
python manage.py regenerate_qr_codes
```

Optional `--restaurant-id`, `--branch-id`, and `--table-id` flags restrict both
preview and regeneration. `SITE_URL` is the only QR origin; per-command URL overrides are not supported.
Regeneration renders the current URL and checks both the stored reference and
PNG bytes. Valid images are reused; stale, missing or corrupted images are
replaced under `media/qr_codes/current/` with content-based filenames. The old
unreferenced image is removed only after the database commit. Table Management,
QR print and QR download run the same freshness check before serving a QR. Download/print the replacement images from table
management: existing paper QR codes cannot update themselves.

For Docker/production, set `SITE_URL=https://orders.your-domain.example` to the
external origin and include that hostname in `DJANGO_ALLOWED_HOSTS`. Publish the
application port or route it through your HTTPS reverse proxy, and persist/serve
`MEDIA_ROOT` so QR downloads survive container replacement. Never encode a
Docker service name, container IP, `0.0.0.0`, or loopback address. Run the same
regeneration command inside the application container after changing the origin.
No request Host header or internal proxy/container address is used in QR URLs.

The local `runserver` command above serves HTTP only. If a phone scanner or
browser upgrades `http://` to `https://`, it cannot connect to this server; the
server logs report HTTPS on an HTTP-only development port. Preserve the encoded
HTTP scheme for the demo, or configure a trusted HTTPS endpoint and set
`SITE_URL` to that origin. Changing only the URL scheme does not enable TLS.

Phone acceptance check: open the URL shown by `--dry-run` in the phone browser,
then scan the newly downloaded QR. Confirm the restaurant/table, add an item,
open the cart, and submit a test order; confirm the resulting order belongs to
the same restaurant, branch, table and table session. This requires a real phone
on your network; automated hostname/routing tests do not prove Wi-Fi reachability.


### Full QR storage reset

```bash
python manage.py regenerate_qr_codes --reset --dry-run
python manage.py regenerate_qr_codes --reset
```

The reset rebuilds/verifies every table before pruning unreferenced generated
`table_*.png` images from QR storage and the historical project `qr_codes/`
directory. It preserves current references and unrelated uploads, and refuses
scoped resets. Repeating the reset keeps valid paths and image bytes unchanged.
Restart application processes after changing `SITE_URL`; download/reprint fresh
codes. Previously printed images or saved phone screenshots cannot be updated.
