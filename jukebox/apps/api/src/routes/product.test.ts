import { describe, expect, it } from "vitest";
import Fastify from "fastify";
import { productRoutes } from "./product.js";

describe("product routes", () => {
  it("returns health and one-time product contract", async () => {
    const app = Fastify();
    await app.register(productRoutes);

    const health = await app.inject({ method: "GET", url: "/api/health" });
    expect(health.statusCode).toBe(200);
    expect(health.json().ok).toBe(true);

    const product = await app.inject({ method: "GET", url: "/api/product" });
    expect(product.statusCode).toBe(200);
    const body = product.json();
    expect(body.pricing.model).toBe("one_time");
    expect(body.pricing.subscription).toBe(false);
    expect(body.rules.noSharing).toBe(true);
    expect(body.rules.localFirst).toBe(true);

    const gear = await app.inject({ method: "GET", url: "/api/gear" });
    expect(gear.statusCode).toBe(200);
    expect(gear.json().items.length).toBeGreaterThan(0);

    await app.close();
  });
});
