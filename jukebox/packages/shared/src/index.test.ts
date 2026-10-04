import { describe, expect, it } from "vitest";
import { formatPrice, PRICING, RECOMMENDED_GEAR, PRODUCT_RULES } from "./index.js";

describe("pricing", () => {
  it("is one-time standard $24.99", () => {
    expect(PRICING.standardCents).toBe(2499);
    expect(formatPrice(PRICING.standardCents)).toBe("$24.99");
  });
});

describe("gear list", () => {
  it("has a short vetted list", () => {
    expect(RECOMMENDED_GEAR.length).toBeGreaterThanOrEqual(2);
    expect(RECOMMENDED_GEAR.length).toBeLessThanOrEqual(6);
  });
});

describe("product rules", () => {
  it("forbids sharing / public links", () => {
    expect(PRODUCT_RULES.noSharing).toBe(true);
    expect(PRODUCT_RULES.noPublicLinks).toBe(true);
    expect(PRODUCT_RULES.localFirst).toBe(true);
  });
});
