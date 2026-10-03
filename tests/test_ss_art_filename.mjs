/** Smoke-test the screensaver art filename helper (mirrors app.js). */
function ssArtFilename(url) {
  if (!url) return "";
  try {
    const path = String(url).split("?")[0];
    const parts = path.split("/");
    return decodeURIComponent(parts[parts.length - 1] || "");
  } catch {
    return String(url);
  }
}

function assertEq(a, b, msg) {
  if (a !== b) {
    console.error("FAIL:", msg, "got", JSON.stringify(a), "want", JSON.stringify(b));
    process.exitCode = 1;
  } else {
    console.log("ok:", msg);
  }
}

assertEq(ssArtFilename("/artwork/clipping.jpg"), "clipping.jpg", "relative path");
assertEq(
  ssArtFilename("http://127.0.0.1:8080/artwork/mexicali.jpg?t=123"),
  "mexicali.jpg",
  "absolute + query",
);
assertEq(ssArtFilename("/artwork/foo%20bar.jpg"), "foo bar.jpg", "urlencoded");
assertEq(ssArtFilename(""), "", "empty");
assertEq(ssArtFilename(null), "", "null");

// Simulate the old bug: once an <img> has src, a naive !art.src check never updates.
const prev = ssArtFilename("/artwork/clipping.jpg?t=1");
const next = ssArtFilename("/artwork/mexicali.jpg?t=2");
assertEq(prev === next, false, "track change must detect different cover files");

if (!process.exitCode) console.log("All ss art filename checks passed");
