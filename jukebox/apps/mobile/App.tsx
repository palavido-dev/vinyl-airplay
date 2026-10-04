import { useMemo, useState } from "react";
import {
  Linking,
  Pressable,
  SafeAreaView,
  ScrollView,
  StatusBar,
  StyleSheet,
  Text,
  View,
} from "react-native";
import {
  APP_NAME,
  APP_TAGLINE,
  PRICING,
  PRODUCT_RULES,
  RECOMMENDED_GEAR,
  formatPrice,
  type GearItem,
} from "@vinyl-jukebox/shared";

type Tab = "jukebox" | "record" | "gear";

const DEMO_ALBUMS = [
  { id: "1", title: "Kind of Blue", artist: "Miles Davis", year: 1959 },
  { id: "2", title: "Abbey Road", artist: "The Beatles", year: 1969 },
  { id: "3", title: "Blue", artist: "Joni Mitchell", year: 1971 },
];

export default function App() {
  const [tab, setTab] = useState<Tab>("jukebox");
  const price = useMemo(() => formatPrice(PRICING.standardCents), []);

  return (
    <SafeAreaView style={styles.safe}>
      <StatusBar barStyle="light-content" />
      <View style={styles.header}>
        <Text style={styles.brand}>{APP_NAME}</Text>
        <Text style={styles.tagline}>{APP_TAGLINE}</Text>
      </View>

      <View style={styles.tabs}>
        {(
          [
            ["jukebox", "Jukebox"],
            ["record", "Record"],
            ["gear", "Gear"],
          ] as const
        ).map(([id, label]) => (
          <Pressable
            key={id}
            onPress={() => setTab(id)}
            style={[styles.tab, tab === id && styles.tabActive]}
          >
            <Text style={[styles.tabText, tab === id && styles.tabTextActive]}>
              {label}
            </Text>
          </Pressable>
        ))}
      </View>

      <ScrollView contentContainerStyle={styles.body}>
        {tab === "jukebox" && <JukeboxTab price={price} />}
        {tab === "record" && <RecordTab />}
        {tab === "gear" && <GearTab items={RECOMMENDED_GEAR} />}
      </ScrollView>
    </SafeAreaView>
  );
}

function JukeboxTab({ price }: { price: string }) {
  return (
    <View style={styles.stack}>
      <Text style={styles.h2}>Your library</Text>
      <Text style={styles.muted}>
        Local catalog placeholder. Audio stays on-device — no public links.
      </Text>
      {DEMO_ALBUMS.map((album) => (
        <Pressable key={album.id} style={styles.albumRow}>
          <View style={styles.sleeve} />
          <View style={styles.albumMeta}>
            <Text style={styles.albumTitle}>{album.title}</Text>
            <Text style={styles.albumArtist}>
              {album.artist} · {album.year}
            </Text>
          </View>
          <Text style={styles.playHint}>Play</Text>
        </Pressable>
      ))}
      <View style={styles.note}>
        <Text style={styles.noteTitle}>{PRICING.label}</Text>
        <Text style={styles.muted}>
          {price} one-time unlock · single-user ·{" "}
          {PRODUCT_RULES.noSharing ? "no sharing" : ""}
        </Text>
      </View>
    </View>
  );
}

function RecordTab() {
  return (
    <View style={styles.stack}>
      <Text style={styles.h2}>Capture a side</Text>
      <Text style={styles.muted}>
        Connect a vetted USB interface, set levels, then record a full side to
        lossless audio. Fingerprint + Discogs match comes next.
      </Text>
      <View style={styles.recorder}>
        <View style={styles.levelTrack}>
          <View style={[styles.levelFill, { width: "12%" }]} />
        </View>
        <Text style={styles.timer}>00:00.00</Text>
        <Pressable style={styles.recordBtn}>
          <Text style={styles.recordBtnText}>Start recording</Text>
        </Pressable>
        <Text style={styles.hint}>
          Placeholder — Expo audio capture + USB route lands in a follow-up.
        </Text>
      </View>
    </View>
  );
}

function GearTab({ items }: { items: GearItem[] }) {
  return (
    <View style={styles.stack}>
      <Text style={styles.h2}>Recommended interfaces</Text>
      <Text style={styles.muted}>
        Short list for tablet line-in capture. Affiliate links may earn a
        commission; never required to use the app.
      </Text>
      {items.map((item) => (
        <Pressable
          key={item.id}
          style={styles.gearRow}
          onPress={() => Linking.openURL(item.affiliateUrl)}
        >
          <Text style={styles.tier}>{item.tier}</Text>
          <Text style={styles.albumTitle}>{item.name}</Text>
          <Text style={styles.muted}>{item.blurb}</Text>
          <Text style={styles.link}>View →</Text>
        </Pressable>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  safe: {
    flex: 1,
    backgroundColor: "#0c0c0e",
  },
  header: {
    paddingHorizontal: 24,
    paddingTop: 12,
    paddingBottom: 8,
  },
  brand: {
    fontFamily: "System",
    fontSize: 32,
    fontWeight: "700",
    letterSpacing: -0.8,
    color: "#f2efe8",
  },
  tagline: {
    marginTop: 6,
    fontSize: 14,
    lineHeight: 20,
    color: "#9a958c",
    maxWidth: 420,
  },
  tabs: {
    flexDirection: "row",
    gap: 8,
    paddingHorizontal: 24,
    paddingVertical: 12,
  },
  tab: {
    paddingVertical: 10,
    paddingHorizontal: 16,
    borderBottomWidth: 2,
    borderBottomColor: "transparent",
  },
  tabActive: {
    borderBottomColor: "#e8a838",
  },
  tabText: {
    color: "#7a756c",
    fontSize: 15,
    fontWeight: "600",
  },
  tabTextActive: {
    color: "#f2efe8",
  },
  body: {
    paddingHorizontal: 24,
    paddingBottom: 48,
  },
  stack: {
    gap: 14,
  },
  h2: {
    fontSize: 22,
    fontWeight: "700",
    color: "#f2efe8",
    letterSpacing: -0.4,
  },
  muted: {
    fontSize: 14,
    lineHeight: 20,
    color: "#9a958c",
  },
  albumRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 14,
    paddingVertical: 12,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "rgba(242,239,232,0.12)",
  },
  sleeve: {
    width: 56,
    height: 56,
    backgroundColor: "#1c1c22",
    borderWidth: 1,
    borderColor: "rgba(232,168,56,0.35)",
  },
  albumMeta: {
    flex: 1,
  },
  albumTitle: {
    fontSize: 16,
    fontWeight: "600",
    color: "#f2efe8",
  },
  albumArtist: {
    marginTop: 2,
    fontSize: 13,
    color: "#9a958c",
  },
  playHint: {
    color: "#e8a838",
    fontWeight: "700",
    fontSize: 13,
  },
  note: {
    marginTop: 8,
    padding: 16,
    backgroundColor: "#16161a",
    borderLeftWidth: 3,
    borderLeftColor: "#e8a838",
  },
  noteTitle: {
    color: "#e8a838",
    fontWeight: "700",
    marginBottom: 4,
  },
  recorder: {
    marginTop: 8,
    padding: 20,
    backgroundColor: "#16161a",
    alignItems: "center",
    gap: 16,
  },
  levelTrack: {
    width: "100%",
    height: 8,
    backgroundColor: "#2a2a32",
    overflow: "hidden",
  },
  levelFill: {
    height: "100%",
    backgroundColor: "#e8a838",
  },
  timer: {
    fontSize: 36,
    fontVariant: ["tabular-nums"],
    color: "#f2efe8",
    letterSpacing: 1,
  },
  recordBtn: {
    backgroundColor: "#e8a838",
    paddingVertical: 14,
    paddingHorizontal: 28,
  },
  recordBtnText: {
    color: "#0c0c0e",
    fontWeight: "700",
    fontSize: 15,
  },
  hint: {
    fontSize: 12,
    color: "#7a756c",
    textAlign: "center",
  },
  gearRow: {
    paddingVertical: 14,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: "rgba(242,239,232,0.12)",
    gap: 4,
  },
  tier: {
    fontSize: 11,
    letterSpacing: 1,
    textTransform: "uppercase",
    color: "#e8a838",
    fontWeight: "700",
  },
  link: {
    marginTop: 6,
    color: "#e8a838",
    fontWeight: "600",
  },
});
