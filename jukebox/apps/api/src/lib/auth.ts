/**
 * Better Auth — self-hosted, free/OSS, no SaaS signup.
 * Email/password for now; add social providers later if needed.
 *
 * Product rule: one user owns their catalog. No multi-user sharing.
 */
import { betterAuth } from "better-auth";
import { drizzleAdapter } from "better-auth/adapters/drizzle";
import { db } from "../db/client.js";
import * as schema from "../db/schema.js";

export const auth = betterAuth({
  database: drizzleAdapter(db, {
    provider: "sqlite",
    schema: {
      user: schema.user,
      session: schema.session,
      account: schema.account,
      verification: schema.verification,
    },
  }),
  emailAndPassword: {
    enabled: true,
  },
  trustedOrigins: [
    process.env.APP_URL ?? "http://localhost:5173",
    "http://localhost:8081",
    "exp://localhost:8081",
  ],
  secret: process.env.BETTER_AUTH_SECRET ?? "dev-change-me-to-a-long-random-string",
  baseURL: process.env.BETTER_AUTH_URL ?? "http://localhost:8787",
});

export type Session = typeof auth.$Infer.Session;
