/** Shared types & constants for Vinyl Jukebox (tablet product). */

export const APP_NAME = "Vinyl Jukebox";
export const APP_TAGLINE =
  "Digitize your vinyl once. Catalog it. Play it back — personal use only.";

/** One-time purchase — no subscription. */
export const PRICING = {
  currency: "USD",
  launchCents: 1499,
  standardCents: 2499,
  label: "Pay once. Yours forever.",
} as const;

export function formatPrice(cents: number, currency = "USD"): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
  }).format(cents / 100);
}

/**
 * Vetted USB interfaces for tablet line-in capture.
 * Replace affiliateUrl with real partner links before launch.
 */
export type GearItem = {
  id: string;
  name: string;
  tier: "budget" | "mid" | "tablet";
  blurb: string;
  /** Placeholder until affiliate program is wired. */
  affiliateUrl: string;
  notes: string;
};

export const RECOMMENDED_GEAR: GearItem[] = [
  {
    id: "behringer-umc202hd",
    name: "Behringer U-Phoria UMC202HD",
    tier: "budget",
    blurb: "Inexpensive class-compliant USB interface; solid starter for line-level vinyl.",
    affiliateUrl: "https://www.amazon.com/s?k=Behringer+UMC202HD",
    notes: "Verify Android USB-C OTG; iPad needs a Camera Connection Kit / USB-C hub.",
  },
  {
    id: "focusrite-scarlett-solo",
    name: "Focusrite Scarlett Solo (4th Gen)",
    tier: "mid",
    blurb: "Common, reliable, easy gain staging for turntables with a preamp.",
    affiliateUrl: "https://www.amazon.com/s?k=Focusrite+Scarlett+Solo+4th+Gen",
    notes: "Preferred mid-tier after hands-on tablet testing.",
  },
  {
    id: "focusrite-scarlett-2i2",
    name: "Focusrite Scarlett 2i2 (4th Gen)",
    tier: "mid",
    blurb: "Stereo line inputs; same family as the original Vinyl Streamer USB path.",
    affiliateUrl: "https://www.amazon.com/s?k=Focusrite+Scarlett+2i2+4th+Gen",
    notes: "Good if you already own one from a studio setup.",
  },
];

export type AlbumSummary = {
  id: string;
  title: string;
  artist: string;
  year?: number | null;
  artworkUri?: string | null;
  sideCount: number;
  trackCount: number;
};

export type LicenseStatus = {
  /** Purchased the one-time unlock. */
  owned: boolean;
  platform?: "ios" | "android" | "web" | "dev";
  purchasedAt?: string | null;
};

/** Product principles — keep these in UI copy and ToS. */
export const PRODUCT_RULES = {
  singleUser: true,
  noSharing: true,
  noPublicLinks: true,
  localFirst: true,
  personalVinylOnly: true,
} as const;
