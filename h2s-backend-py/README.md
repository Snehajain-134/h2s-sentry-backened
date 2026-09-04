# H2S Sentry — Backend (Python / Flask)

This replaces the earlier Node/Express backend. `h2s-sentry.html`
was always built expecting a **Python** server on **port 5000** — its
own error messages say "make sure it is running (`python server.py`)"
and it calls routes (`/api/wristband/:id`, `/api/analyze`) that never
existed in the Node version. This is the real, matching backend.

## Setup

```bash
pip install flask pyjwt pillow
python server.py
```

You should see: `H2S Sentry API (Python) running on http://localhost:5000`

Then just open `h2s-sentry.html` in a browser (served over
`http://localhost:...`, not double-clicked as a file, so the camera
works) and log in.

## Demo logins (all passwords are `1234`)

| ID | Role |
|---|---|
| WB-0942 | Worker — Rajesh Nayak |
| WB-1187 | Worker — Sunita Pradhan |
| WB-0655 | Worker — Manoj Sethi |
| WB-1340 | Worker — Debashish Rout |
| WB-0821 | Worker — Priya Mallick |
| SV-01 | Supervisor — Anita Behera |
| AD-01 | Admin — Site Admin |

## What's real vs. what's a stand-in

- **Real:** the CIELAB → ΔE → ppm colorimetric model in `/api/analyze`
  is the same math as your calibration dataset — it genuinely reads
  the pixel color from the captured photo, not a random number.
- **Real:** JWT auth, role-based access (a worker can only see their
  own profile/history; only supervisor/admin can scan or list workers;
  a supervisor only sees workers in their assigned zones).
- **Real:** entry/exit session logic — the first scan for a worker
  opens a session, the next one closes it and computes duration.
- **Stand-in:** `db.json` is a flat file, not a real database — fine
  for a prototype/demo, swap `read_db()`/`write_db()` for a real DB
  (Postgres/SQLite) later without touching any route logic.
- **Stand-in:** the calibration constants (L0, a0, b0, ppm_scale) are
  the same modeled values from your synthetic calibration dataset —
  replace them once you have real chamber-exposure measurements.

## API reference

| Method | Route | Who | What |
|---|---|---|---|
| POST | `/api/login` | anyone | `{id, password}` → `{token, role, name, workerId}` |
| GET | `/api/me` | logged in | confirms who the token belongs to |
| GET | `/api/worker/:id` | worker (self), supervisor, admin | one worker's profile |
| GET | `/api/wristband/:id` | supervisor, admin | same profile, looked up by scanned wristband ID (used in the scan flow) |
| GET | `/api/workers` | supervisor (own zones only), admin (all) | worker directory |
| GET | `/api/history/:id` | worker (self), supervisor, admin | shift session history |
| GET | `/api/zones` | supervisor, admin | per-zone worker count + worst status |
| POST | `/api/analyze` | supervisor, admin | `{wristband_id, image_base64}` → runs the real colorimetric analysis, logs an Entry or Exit reading |

Every route except `/api/login` requires `Authorization: Bearer <token>`.
