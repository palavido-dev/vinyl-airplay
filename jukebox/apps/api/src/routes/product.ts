import type { FastifyInstance } from "fastify";
import {
  APP_NAME,
  APP_TAGLINE,
  PRICING,
  PRODUCT_RULES,
  RECOMMENDED_GEAR,
  formatPrice,
} from "@vinyl-jukebox/shared";

export async function productRoutes(app: FastifyInstance) {
  app.get("/api/health", async () => ({
    ok: true,
    service: "vinyl-jukebox-api",
    ts: new Date().toISOString(),
  }));

  app.get("/api/product", async () => ({
    name: APP_NAME,
    tagline: APP_TAGLINE,
    pricing: {
      ...PRICING,
      launchLabel: formatPrice(PRICING.launchCents),
      standardLabel: formatPrice(PRICING.standardCents),
      model: "one_time" as const,
      subscription: false,
    },
    rules: PRODUCT_RULES,
  }));

  app.get("/api/gear", async () => ({
    disclaimer:
      "We may earn a commission from recommended gear links. Interfaces are vetted for personal vinyl capture — not a storefront for shared music files.",
    items: RECOMMENDED_GEAR,
  }));
}
