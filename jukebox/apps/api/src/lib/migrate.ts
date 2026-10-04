import { sqlite } from "../db/client.js";

/** Minimal SQL bootstrap so `pnpm dev` works without drizzle-kit on first boot. */
export function ensureSchema() {
  sqlite.exec(`
    CREATE TABLE IF NOT EXISTS user (
      id TEXT PRIMARY KEY NOT NULL,
      name TEXT NOT NULL,
      email TEXT NOT NULL UNIQUE,
      email_verified INTEGER NOT NULL DEFAULT 0,
      image TEXT,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS session (
      id TEXT PRIMARY KEY NOT NULL,
      expires_at INTEGER NOT NULL,
      token TEXT NOT NULL UNIQUE,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      ip_address TEXT,
      user_agent TEXT,
      user_id TEXT NOT NULL REFERENCES user(id)
    );
    CREATE TABLE IF NOT EXISTS account (
      id TEXT PRIMARY KEY NOT NULL,
      account_id TEXT NOT NULL,
      provider_id TEXT NOT NULL,
      user_id TEXT NOT NULL REFERENCES user(id),
      access_token TEXT,
      refresh_token TEXT,
      id_token TEXT,
      access_token_expires_at INTEGER,
      refresh_token_expires_at INTEGER,
      scope TEXT,
      password TEXT,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS verification (
      id TEXT PRIMARY KEY NOT NULL,
      identifier TEXT NOT NULL,
      value TEXT NOT NULL,
      expires_at INTEGER NOT NULL,
      created_at INTEGER,
      updated_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS license (
      id TEXT PRIMARY KEY NOT NULL,
      user_id TEXT NOT NULL UNIQUE REFERENCES user(id),
      platform TEXT NOT NULL,
      store_transaction_id TEXT,
      purchased_at INTEGER NOT NULL,
      created_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS album (
      id TEXT PRIMARY KEY NOT NULL,
      user_id TEXT NOT NULL REFERENCES user(id),
      title TEXT NOT NULL,
      artist TEXT NOT NULL,
      year INTEGER,
      discogs_id TEXT,
      artwork_local_key TEXT,
      notes TEXT,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS album_side (
      id TEXT PRIMARY KEY NOT NULL,
      album_id TEXT NOT NULL REFERENCES album(id),
      user_id TEXT NOT NULL REFERENCES user(id),
      side TEXT NOT NULL,
      local_audio_key TEXT NOT NULL,
      duration_secs INTEGER,
      created_at INTEGER NOT NULL
    );
  `);
}
