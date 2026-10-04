# Vinyl Jukebox

Personal vinyl catalog & tablet jukebox — **one-time purchase**, local-first, single-user.

Digitize a side through a USB interface, catalog with Discogs metadata, fingerprint for recognition, then play lossless copies on iPad / Android tablet. Speakers via system AirPlay or Bluetooth.

This is the product monorepo (sibling to the Raspberry Pi Vinyl Streamer). Same soul; tablet-native distribution.

## Product rules

- Pay once — no subscription
- Personal vinyl you own only
- No sharing, no public file links, no multi-user audio CDN
- Local-first; optional cloud later is **private backup only**

Pricing (from `@vinyl-jukebox/shared`): launch **$14.99**, standard **$24.99**.

## Monorepo layout

```
jukebox/
  apps/
    api/      Fastify + Drizzle (SQLite) + Better Auth stub
    web/      Vite marketing site
    mobile/   Expo (iOS / Android tablet) app shell
  packages/
    shared/   Pricing, gear list, product rules, shared types
```

## Prerequisites

- Node 20+
- [pnpm](https://pnpm.io) 9+

## Setup

```bash
cd jukebox
pnpm install
cp apps/api/.env.example apps/api/.env   # optional
```

## Develop

```bash
# API on :8787
pnpm dev:api

# Marketing site on :5173
pnpm dev:web

# Expo
pnpm dev:mobile
```

## Test

```bash
pnpm test
pnpm typecheck
```

## API surface (scaffold)

| Route | Purpose |
|-------|---------|
| `GET /api/health` | Liveness |
| `GET /api/product` | Name, pricing, product rules |
| `GET /api/gear` | Recommended USB interfaces (affiliate placeholders) |
| `/api/auth/*` | Better Auth (email/password) |

Catalog tables exist (`album`, `album_side`, `license`) with **local audio keys only** — never public object URLs.

## Mobile tabs (scaffold)

1. **Jukebox** — demo library list + one-time pricing note
2. **Record** — capture UI placeholder (USB / Expo audio next)
3. **Gear** — vetted interface list with affiliate deep-links

## Out of scope (for now)

- Maps
- Subscriptions / freemium cloud sharing
- Shipping a Pi appliance with this app
