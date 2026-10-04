/**
 * Local-first catalog schema.
 *
 * Audio files live on the user's device (or user-controlled storage).
 * The API never exposes public file URLs or cross-user sharing.
 */
import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";

export const user = sqliteTable("user", {
  id: text("id").primaryKey(),
  name: text("name").notNull(),
  email: text("email").notNull().unique(),
  emailVerified: integer("email_verified", { mode: "boolean" }).notNull().default(false),
  image: text("image"),
  createdAt: integer("created_at", { mode: "timestamp_ms" }).notNull(),
  updatedAt: integer("updated_at", { mode: "timestamp_ms" }).notNull(),
});

export const session = sqliteTable("session", {
  id: text("id").primaryKey(),
  expiresAt: integer("expires_at", { mode: "timestamp_ms" }).notNull(),
  token: text("token").notNull().unique(),
  createdAt: integer("created_at", { mode: "timestamp_ms" }).notNull(),
  updatedAt: integer("updated_at", { mode: "timestamp_ms" }).notNull(),
  ipAddress: text("ip_address"),
  userAgent: text("user_agent"),
  userId: text("user_id")
    .notNull()
    .references(() => user.id),
});

export const account = sqliteTable("account", {
  id: text("id").primaryKey(),
  accountId: text("account_id").notNull(),
  providerId: text("provider_id").notNull(),
  userId: text("user_id")
    .notNull()
    .references(() => user.id),
  accessToken: text("access_token"),
  refreshToken: text("refresh_token"),
  idToken: text("id_token"),
  accessTokenExpiresAt: integer("access_token_expires_at", { mode: "timestamp_ms" }),
  refreshTokenExpiresAt: integer("refresh_token_expires_at", { mode: "timestamp_ms" }),
  scope: text("scope"),
  password: text("password"),
  createdAt: integer("created_at", { mode: "timestamp_ms" }).notNull(),
  updatedAt: integer("updated_at", { mode: "timestamp_ms" }).notNull(),
});

export const verification = sqliteTable("verification", {
  id: text("id").primaryKey(),
  identifier: text("identifier").notNull(),
  value: text("value").notNull(),
  expiresAt: integer("expires_at", { mode: "timestamp_ms" }).notNull(),
  createdAt: integer("created_at", { mode: "timestamp_ms" }),
  updatedAt: integer("updated_at", { mode: "timestamp_ms" }),
});

/** One-time purchase entitlement — no subscription fields on purpose. */
export const license = sqliteTable("license", {
  id: text("id").primaryKey(),
  userId: text("user_id")
    .notNull()
    .unique()
    .references(() => user.id),
  platform: text("platform").notNull(), // ios | android | web | dev
  storeTransactionId: text("store_transaction_id"),
  purchasedAt: integer("purchased_at", { mode: "timestamp_ms" }).notNull(),
  createdAt: integer("created_at", { mode: "timestamp_ms" }).notNull(),
});

/**
 * Catalog metadata only. Binary audio never stored as a shareable object here.
 * `localAudioKey` is an opaque client-side path/id — not a public URL.
 */
export const album = sqliteTable("album", {
  id: text("id").primaryKey(),
  userId: text("user_id")
    .notNull()
    .references(() => user.id),
  title: text("title").notNull(),
  artist: text("artist").notNull(),
  year: integer("year"),
  discogsId: text("discogs_id"),
  artworkLocalKey: text("artwork_local_key"),
  notes: text("notes"),
  createdAt: integer("created_at", { mode: "timestamp_ms" }).notNull(),
  updatedAt: integer("updated_at", { mode: "timestamp_ms" }).notNull(),
});

export const albumSide = sqliteTable("album_side", {
  id: text("id").primaryKey(),
  albumId: text("album_id")
    .notNull()
    .references(() => album.id),
  userId: text("user_id")
    .notNull()
    .references(() => user.id),
  side: text("side").notNull(), // A | B | 1 | 2 ...
  localAudioKey: text("local_audio_key").notNull(),
  durationSecs: integer("duration_secs"),
  createdAt: integer("created_at", { mode: "timestamp_ms" }).notNull(),
});
