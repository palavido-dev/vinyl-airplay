import { describe, expect, it } from "vitest";
import { PRICING, PRODUCT_RULES, RECOMMENDED_GEAR } from "@vinyl-jukebox/shared";

describe("api product contract", () => {
  it("exposes one-time pricing only", () => {
    expect(PRICING.standardCents).toBe(2499);
    expect(PRODUCT_RULES.noSharing).toBe(true);
  });

  it("lists gear without implying a file-share CDN", () => {
    for (const g of RECOMMENDED_GEAR) {
      expect(g.affiliateUrl).toMatch(/^https?:\/\//);
      expect(g.name.length).toBeGreaterThan(3);
    }
  });
});
