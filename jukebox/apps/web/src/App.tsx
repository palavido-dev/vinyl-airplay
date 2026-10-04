import { useEffect, useState } from "react";
import {
  APP_NAME,
  APP_TAGLINE,
  PRICING,
  PRODUCT_RULES,
  RECOMMENDED_GEAR,
  formatPrice,
  type GearItem,
} from "@vinyl-jukebox/shared";
import "./App.css";

type ProductApi = {
  pricing: { launchLabel: string; standardLabel: string; model: string };
};

const API = import.meta.env.VITE_API_URL ?? "http://localhost:8787";

export default function App() {
  const [product, setProduct] = useState<ProductApi | null>(null);
  const [gear, setGear] = useState<GearItem[]>(RECOMMENDED_GEAR);

  useEffect(() => {
    fetch(`${API}/api/product`)
      .then((r) => r.json())
      .then(setProduct)
      .catch(() => setProduct(null));
    fetch(`${API}/api/gear`)
      .then((r) => r.json())
      .then((d) => setGear(d.items ?? RECOMMENDED_GEAR))
      .catch(() => undefined);
  }, []);

  const price =
    product?.pricing.standardLabel ?? formatPrice(PRICING.standardCents);
  const launch =
    product?.pricing.launchLabel ?? formatPrice(PRICING.launchCents);

  return (
    <div className="page">
      <header className="hero">
        <div className="hero-copy">
          <p className="brand">{APP_NAME}</p>
          <h1>Your vinyl, digitized once — played forever.</h1>
          <p className="lead">{APP_TAGLINE}</p>
          <div className="cta-row">
            <a className="btn btn-primary" href="#download">
              Get the app
            </a>
            <a className="btn btn-ghost" href="#gear">
              Recommended gear
            </a>
          </div>
          <p className="price-line">
            Launch {launch} · then {price} · {PRICING.label}
          </p>
        </div>
        <div className="hero-visual" aria-hidden="true">
          <div className="platter">
            <div className="platter-ring" />
            <div className="platter-label" />
            <div className="platter-spindle" />
          </div>
        </div>
      </header>

      <section className="section">
        <h2>Capture. Catalog. Play.</h2>
        <p className="sub">
          Line-in through a USB interface, Discogs metadata, Chromaprint
          fingerprints, then a touch-first jukebox on iPad or Android tablet.
          Speakers via system AirPlay or Bluetooth — no extra appliance.
        </p>
        <ol className="steps">
          <li>
            <strong>Record once</strong>
            <span>Lossless capture from your turntable into a vetted interface.</span>
          </li>
          <li>
            <strong>Personal catalog</strong>
            <span>Albums, artwork, notes — on your device. Local-first by design.</span>
          </li>
          <li>
            <strong>Jukebox playback</strong>
            <span>Browse and play without putting another mile on the grooves.</span>
          </li>
        </ol>
      </section>

      <section className="section" id="gear">
        <h2>Recommended interfaces</h2>
        <p className="sub">
          A short list we intend to validate on real tablets. Affiliate links may
          earn a small commission — never required to use the app.
        </p>
        <ul className="gear-list">
          {gear.map((g) => (
            <li key={g.id}>
              <span className="tier">{g.tier}</span>
              <a href={g.affiliateUrl} target="_blank" rel="noreferrer sponsored">
                {g.name}
              </a>
              <p>{g.blurb}</p>
            </li>
          ))}
        </ul>
      </section>

      <section className="section" id="download">
        <div className="rules">
          <h2>Built to stay personal</h2>
          <ul>
            <li>
              One-time purchase
              {PRODUCT_RULES.singleUser ? " · single-user library" : ""}
            </li>
            <li>No sharing, no public links, no multi-user audio CDN</li>
            <li>For digitizing vinyl you own — personal backup & playback</li>
            <li>
              Cloud optional later, and only as private backup — never a
              file-share service
            </li>
          </ul>
          <p className="coming">
            App Store / Play Store listings coming. This site is the product home
            while we build.
          </p>
        </div>
      </section>

      <footer className="footer">
        Descended from the open-source Vinyl Streamer Pi project — same soul,
        tablet-native. Pricing model: one-time ({price}), not a subscription.
      </footer>
    </div>
  );
}
