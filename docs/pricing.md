# Pricing one-pager (internal)

Street prices checked ~Oct 2026 (US). Buyer shipping is **not** included — quote that separately. Essential SKUs are planning numbers until hardware is validated.

## Assumptions

| Item | Rule |
|---|---|
| Soft buffer | **8%** of parts (packaging, RCA/odds, PayPal scrape, one bad part) |
| Labor rate | **~$55/hr** effective |
| Studio labor | **3.0 hrs → $175** (assemble, image, burn-in, pack, early support) |
| Essential Touch labor | **2.5 hrs → $140** |
| Essential Headless labor | **2.0 hrs → $110** |
| Target quote | Loaded cost ÷ **0.75** (~**25%** gross after labor) |

**Loaded cost** = parts + soft buffer + labor value.

**Cash after parts** = quote − parts − soft buffer (what’s left to pay you for time + profit).  
**Economic profit** = cash after parts − labor value (true margin if you value your hours).

---

## Studio — path A (buy CanaKit like README)

| Part | Plan $ |
|---|---:|
| CanaKit Pi 5 8GB starter | 230 |
| HiFiBerry DAC2 ADC Pro | 95 |
| NVMe HAT | 20 |
| 256GB NVMe | 25 |
| ROADOM 10.1" touch | 95 |
| Misc | 30 |
| **Parts** | **495** |
| Soft 8% | 40 |
| Labor | 175 |
| **Loaded cost** | **710** |

| Quote | Cash after parts | Economic profit | Effective $/hr (3h) |
|---:|---:|---:|---:|
| **$849** | $314 | **$139** | $105 |
| **$899** (suggested) | $364 | **$189** | $121 |
| **$949** | $414 | **$239** | $138 |

---

## Studio — path B (optimized parts buy) ← preferred

| Part | Plan $ |
|---|---:|
| Pi 5 8GB + PSU + cooler | 100 |
| HiFiBerry DAC2 ADC Pro | 85 |
| NVMe HAT | 20 |
| 256GB NVMe | 25 |
| ROADOM 10.1" touch | 95 |
| Misc | 30 |
| **Parts** | **355** |
| Soft 8% | 28 |
| Labor | 175 |
| **Loaded cost** | **558** |

| Quote | Cash after parts | Economic profit | Effective $/hr (3h) |
|---:|---:|---:|---:|
| **$699** | $316 | **$141** | $105 |
| **$749** | $366 | **$191** | $122 |
| **$799** (suggested) | $416 | **$241** | $139 |

---

## Essential Touch (validate before selling)

| Part | Plan $ |
|---|---:|
| Pi 4 4GB + PSU + SD/small SSD | 75 |
| USB line-in ADC (Behringer-class) | 50 |
| 7–10" touch | 80 |
| Misc | 25 |
| **Parts** | **230** |
| Soft 8% | 18 |
| Labor | 140 |
| **Loaded cost** | **388** |

| Quote | Cash after parts | Economic profit | Effective $/hr (2.5h) |
|---:|---:|---:|---:|
| **$499** | $251 | **$111** | $100 |
| **$529** (suggested) | $281 | **$141** | $112 |
| **$549** | $301 | **$161** | $120 |

---

## Essential Headless (validate before selling)

| Part | Plan $ |
|---|---:|
| Pi 4 4GB + PSU + SD | 75 |
| USB line-in ADC | 50 |
| Misc | 20 |
| **Parts** | **145** |
| Soft 8% | 12 |
| Labor | 110 |
| **Loaded cost** | **267** |

| Quote | Cash after parts | Economic profit | Effective $/hr (2h) |
|---:|---:|---:|---:|
| **$349** | $192 | **$82** | $96 |
| **$379** (suggested) | $222 | **$112** | $111 |
| **$399** | $242 | **$132** | $121 |

---

## Single-unit snapshot (at suggested quotes)

| SKU | Quote | You spend (parts+soft) | Cash left | After valuing labor | Notes |
|---|---:|---:|---:|---:|---|
| Studio (kit) | **$899** | $535 | **$364** | **+$189** | Easy buy path; weaker margin |
| Studio (optimized) | **$799** | $383 | **$416** | **+$241** | Best Studio economics |
| Essential Touch | **$529** | $248 | **$281** | **+$141** | Waitlist until validated |
| Essential Headless | **$379** | $157 | **$222** | **+$112** | Waitlist until validated |

**Read of the numbers:** on a single optimized Studio at $799, expect roughly **$400 cash** after parts, or about **$240** if you count 3 hours of your time as a real cost (~**$140/hr** all-in for that build). Essential makes less absolute profit per unit but less cash tied up in inventory.

---

## Quote checklist

1. Confirm HiFiBerry / display stock and today’s cart total (swap in real numbers).  
2. Add outbound shipping (often **$15–40** domestic).  
3. Don’t firm-price Essential until burn-in passes.  
4. Prefer Studio path B parts buys if listing near **$799**.

Related: [Prebuilt](prebuilt.md) · [Hardware tiers](hardware-tiers.md)
