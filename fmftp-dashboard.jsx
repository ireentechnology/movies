import { useState, useEffect } from "react";

// ── Demo data — GitHub Actions run হওয়ার পর real report.json দিয়ে replace হবে ──
const DEMO_DATA = {
  generated_at: new Date().toISOString(),
  total_movies: 0,
  total_series: 0,
  grand_total: 0,
  movies: {
    total: 0,
    categories: [
      { label: "Bollywood",     category_id: "1",  file: "movies/Bollywood.json",     count: 0, new: 0 },
      { label: "Hollywood",     category_id: "2",  file: "movies/Hollywood.json",     count: 0, new: 0 },
      { label: "Animation",     category_id: "3",  file: "movies/Animation.json",     count: 0, new: 0 },
      { label: "Korean Movies", category_id: "4",  file: "movies/Korean.json",        count: 0, new: 0 },
      { label: "Hindi Dubbed",  category_id: "5",  file: "movies/Hindi_dubbed.json",  count: 0, new: 0 },
      { label: "Horror",        category_id: "6",  file: "movies/Horror.json",        count: 0, new: 0 },
      { label: "Indian Bangla", category_id: "7",  file: "movies/Indian_Bangla.json", count: 0, new: 0 },
      { label: "Tamil",         category_id: "8",  file: "movies/Tamil.json",         count: 0, new: 0 },
      { label: "Foreign",       category_id: "14", file: "movies/foreign.json",       count: 0, new: 0 },
    ],
  },
  series: {
    total: 0,
    categories: [
      { label: "English TV Series",  category_id: "9",  file: "series/English_Tv_Series.json",  count: 0, new: 0 },
      { label: "Indian TV Series",   category_id: "10", file: "series/Indian_Tv_Series.json",   count: 0, new: 0 },
      { label: "Korean TV Series",   category_id: "11", file: "series/Korean_Tv_Series.json",   count: 0, new: 0 },
      { label: "Bangla TV Series",   category_id: "12", file: "series/Bangla_Tv_Series.json",   count: 0, new: 0 },
      { label: "Turkish TV Series",  category_id: "13", file: "series/Turkish_Tv_Series.json",  count: 0, new: 0 },
    ],
  },
};

const MOVIE_EMOJIS = {
  "Bollywood": "🎭", "Hollywood": "🎬", "Animation": "🎨",
  "Korean Movies": "🇰🇷", "Hindi Dubbed": "🎙️", "Horror": "👻",
  "Indian Bangla": "🪔", "Tamil": "🎶", "Foreign": "🌍",
};
const SERIES_EMOJIS = {
  "English TV Series": "🇬🇧", "Indian TV Series": "🇮🇳",
  "Korean TV Series": "🇰🇷", "Bangla TV Series": "🇧🇩", "Turkish TV Series": "🇹🇷",
};

function fmt(n) {
  return Number(n).toLocaleString();
}

function StatCard({ value, label, color }) {
  return (
    <div style={{
      background: "#161b22",
      border: "1px solid #30363d",
      borderRadius: 12,
      padding: "20px 24px",
      textAlign: "center",
      flex: "1 1 160px",
    }}>
      <div style={{ fontSize: 36, fontWeight: 700, color, lineHeight: 1, marginBottom: 6 }}>
        {fmt(value)}
      </div>
      <div style={{ fontSize: 12, color: "#7d8590", letterSpacing: "0.06em", textTransform: "uppercase" }}>
        {label}
      </div>
    </div>
  );
}

function CategoryCard({ cat, maxCount, type, isDemo }) {
  const pct = maxCount ? Math.round((cat.count / maxCount) * 100) : 0;
  const emoji = type === "movie"
    ? (MOVIE_EMOJIS[cat.label] || "🎬")
    : (SERIES_EMOJIS[cat.label] || "📺");
  const barColor   = type === "movie" ? "#58a6ff" : "#bc8cff";
  const countColor = type === "movie" ? "#58a6ff" : "#bc8cff";

  return (
    <div style={{
      background: "#161b22",
      border: "1px solid #30363d",
      borderRadius: 12,
      padding: "18px 20px",
      display: "flex",
      flexDirection: "column",
      gap: 10,
      transition: "border-color .2s",
      cursor: "default",
    }}
      onMouseEnter={e => e.currentTarget.style.borderColor = barColor}
      onMouseLeave={e => e.currentTarget.style.borderColor = "#30363d"}
    >
      {/* Name row */}
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ fontSize: 20 }}>{emoji}</span>
        <span style={{ fontWeight: 600, fontSize: 14, color: "#e6edf3", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {cat.label}
        </span>
      </div>

      {/* Count */}
      <div style={{ display: "flex", alignItems: "baseline", gap: 6 }}>
        <span style={{ fontSize: 32, fontWeight: 700, color: countColor, lineHeight: 1 }}>
          {isDemo ? "—" : fmt(cat.count)}
        </span>
        <span style={{ fontSize: 12, color: "#7d8590" }}>
          {type === "movie" ? "movies" : "shows"}
        </span>
      </div>

      {/* Bar */}
      <div style={{ height: 5, background: "#30363d", borderRadius: 4, overflow: "hidden" }}>
        <div style={{
          height: "100%",
          width: isDemo ? "0%" : `${pct}%`,
          background: barColor,
          borderRadius: 4,
          transition: "width .8s ease",
        }} />
      </div>

      {/* Badges */}
      {!isDemo && cat.new > 0 && (
        <span style={{
          display: "inline-flex", alignItems: "center", gap: 4,
          fontSize: 12, color: "#3fb950",
          background: "rgba(63,185,80,.12)",
          border: "1px solid rgba(63,185,80,.3)",
          borderRadius: 20, padding: "2px 10px", width: "fit-content",
        }}>
          +{fmt(cat.new)} নতুন
        </span>
      )}
      {cat.error && (
        <span style={{ fontSize: 12, color: "#f85149" }}>⚠ {cat.error}</span>
      )}
    </div>
  );
}

// ── Donut Chart ──────────────────────────────────────────────────────────────
function DonutChart({ categories, type, isDemo }) {
  const total = categories.reduce((s, c) => s + c.count, 0);
  if (!total || isDemo) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: 180, color: "#7d8590", fontSize: 13 }}>
        GitHub Actions run করুন
      </div>
    );
  }

  const colors = type === "movie"
    ? ["#58a6ff","#388bfd","#1f6feb","#2f81f7","#79c0ff","#a5d6ff","#cae8ff","#96d0ff","#b3d8ff"]
    : ["#bc8cff","#a371f7","#8957e5","#6e40c9","#d2a8ff"];

  const R = 70, cx = 90, cy = 90, stroke = 22;
  const circumference = 2 * Math.PI * R;
  let offset = 0;
  const slices = categories.map((c, i) => {
    const pct   = c.count / total;
    const dash  = pct * circumference;
    const gap   = circumference - dash;
    const el = (
      <circle key={i} cx={cx} cy={cy} r={R}
        fill="none"
        stroke={colors[i % colors.length]}
        strokeWidth={stroke}
        strokeDasharray={`${dash} ${gap}`}
        strokeDashoffset={-offset}
        style={{ transform: "rotate(-90deg)", transformOrigin: `${cx}px ${cy}px` }}
      >
        <title>{c.label}: {fmt(c.count)}</title>
      </circle>
    );
    offset += dash;
    return el;
  });

  return (
    <svg width={180} height={180} viewBox="0 0 180 180">
      <circle cx={cx} cy={cy} r={R} fill="none" stroke="#30363d" strokeWidth={stroke} />
      {slices}
      <text x={cx} y={cy - 6} textAnchor="middle" fill="#e6edf3" fontSize={20} fontWeight={700}>
        {fmt(total)}
      </text>
      <text x={cx} y={cy + 12} textAnchor="middle" fill="#7d8590" fontSize={10}>
        {type === "movie" ? "Movies" : "Series"}
      </text>
    </svg>
  );
}

// ── Main App ─────────────────────────────────────────────────────────────────
export default function App() {
  const [data, setData]       = useState(DEMO_DATA);
  const [isDemo, setIsDemo]   = useState(true);
  const [loading, setLoading] = useState(false);
  const [repoUrl, setRepoUrl] = useState("https://raw.githubusercontent.com/ireentechnology/movies/main");
  const [error, setError]     = useState("");
  const [tab, setTab]         = useState("movies");

  async function loadReport() {
    setLoading(true);
    setError("");
    try {
      const url = repoUrl.replace(/\/$/, "") + "/report.json?t=" + Date.now();
      const r   = await fetch(url);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const json = await r.json();
      setData(json);
      setIsDemo(false);
    } catch (e) {
      setError("report.json load হয়নি। প্রথমে GitHub Actions run করুন। (" + e.message + ")");
    }
    setLoading(false);
  }

  const mMax = Math.max(...data.movies.categories.map(c => c.count), 1);
  const sMax = Math.max(...data.series.categories.map(c => c.count), 1);

  const scanTime = isDemo ? "—" : new Date(data.generated_at)
    .toLocaleString("bn-BD", { timeZone: "Asia/Dhaka" });

  return (
    <div style={{ background: "#0d1117", minHeight: "100vh", color: "#e6edf3", fontFamily: "system-ui, sans-serif" }}>

      {/* Header */}
      <div style={{ background: "#161b22", borderBottom: "1px solid #30363d", padding: "16px 20px" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12 }}>
          <div>
            <h1 style={{ fontSize: 18, fontWeight: 700, display: "flex", alignItems: "center", gap: 8 }}>
              🎬 FMFTP Content Report
            </h1>
            <div style={{ fontSize: 12, color: "#7d8590", marginTop: 3 }}>
              Last scan: {scanTime}
            </div>
          </div>
          {isDemo && (
            <span style={{ fontSize: 11, background: "rgba(255,166,87,.15)", color: "#ffa657", border: "1px solid rgba(255,166,87,.3)", borderRadius: 20, padding: "3px 10px" }}>
              DEMO — real data load করুন
            </span>
          )}
        </div>

        {/* Repo URL input */}
        <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
          <input
            value={repoUrl}
            onChange={e => setRepoUrl(e.target.value)}
            placeholder="https://raw.githubusercontent.com/faria2177/YOUR-REPO/main"
            style={{
              flex: 1, minWidth: 280,
              background: "#0d1117", border: "1px solid #30363d",
              borderRadius: 6, color: "#e6edf3", padding: "6px 12px", fontSize: 12,
            }}
          />
          <button
            onClick={loadReport}
            disabled={loading}
            style={{
              background: "#238636", border: "none", color: "#fff",
              borderRadius: 6, padding: "6px 16px", fontSize: 13,
              cursor: loading ? "wait" : "pointer", fontWeight: 600,
            }}
          >
            {loading ? "Loading…" : "⟳ Load Report"}
          </button>
        </div>
        {error && <div style={{ fontSize: 12, color: "#f85149", marginTop: 8 }}>{error}</div>}
      </div>

      {/* Stat Cards */}
      <div style={{ display: "flex", gap: 12, padding: "20px 20px 0", flexWrap: "wrap" }}>
        <StatCard value={data.total_movies} label="🎬 Total Movies"  color="#58a6ff" />
        <StatCard value={data.total_series} label="📺 Total Series"  color="#bc8cff" />
        <StatCard value={data.grand_total}  label="📦 Grand Total"   color="#3fb950" />
        <StatCard
          value={data.movies.categories.length + data.series.categories.length}
          label="📂 Categories" color="#ffa657"
        />
      </div>

      {/* Donut Charts */}
      <div style={{ display: "flex", gap: 20, padding: "20px", flexWrap: "wrap", justifyContent: "center" }}>
        <div style={{ background: "#161b22", border: "1px solid #30363d", borderRadius: 12, padding: "16px 24px", textAlign: "center" }}>
          <div style={{ fontSize: 12, color: "#7d8590", marginBottom: 10, letterSpacing: ".06em", textTransform: "uppercase" }}>Movies by Category</div>
          <DonutChart categories={data.movies.categories} type="movie" isDemo={isDemo} />
        </div>
        <div style={{ background: "#161b22", border: "1px solid #30363d", borderRadius: 12, padding: "16px 24px", textAlign: "center" }}>
          <div style={{ fontSize: 12, color: "#7d8590", marginBottom: 10, letterSpacing: ".06em", textTransform: "uppercase" }}>Series by Category</div>
          <DonutChart categories={data.series.categories} type="series" isDemo={isDemo} />
        </div>
      </div>

      {/* Tab bar */}
      <div style={{ padding: "0 20px", borderBottom: "1px solid #30363d", display: "flex", gap: 0 }}>
        {[["movies","🎬 Movies"], ["series","📺 TV Series"]].map(([key, lbl]) => (
          <button key={key} onClick={() => setTab(key)}
            style={{
              background: "none", border: "none", color: tab === key ? "#e6edf3" : "#7d8590",
              borderBottom: `2px solid ${tab === key ? "#58a6ff" : "transparent"}`,
              padding: "10px 18px", fontSize: 14, cursor: "pointer", fontWeight: tab === key ? 600 : 400,
              transition: "color .15s",
            }}>
            {lbl}
          </button>
        ))}
      </div>

      {/* Category Grid */}
      <div style={{ padding: 20 }}>
        {tab === "movies" && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(210px,1fr))", gap: 14 }}>
            {data.movies.categories.map(cat => (
              <CategoryCard key={cat.category_id} cat={cat} maxCount={mMax} type="movie" isDemo={isDemo} />
            ))}
          </div>
        )}
        {tab === "series" && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(210px,1fr))", gap: 14 }}>
            {data.series.categories.map(cat => (
              <CategoryCard key={cat.category_id} cat={cat} maxCount={sMax} type="series" isDemo={isDemo} />
            ))}
          </div>
        )}
      </div>

      <div style={{ textAlign: "center", padding: 20, fontSize: 12, color: "#7d8590", borderTop: "1px solid #30363d" }}>
        Auto-updated every 2 days · fmftp.net
      </div>
    </div>
  );
}
