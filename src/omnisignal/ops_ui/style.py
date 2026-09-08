"""Static visual theme for the Streamlit operations console."""

APP_CSS = """
<style>
:root {
  color-scheme: dark;
  --os-bg: #06080c;
  --os-panel: #0c1118;
  --os-panel-2: #101722;
  --os-line: #1b2736;
  --os-text: #eef4ff;
  --os-muted: #8290a3;
  --os-blue: #3b82f6;
  --os-blue-soft: #10264a;
}

[data-testid="stAppViewContainer"] {
  background:
    radial-gradient(circle at 84% 0%, rgba(59, 130, 246, 0.12), transparent 24rem),
    var(--os-bg);
  color: var(--os-text);
}

[data-testid="stHeader"] { background: rgba(6, 8, 12, 0.88); }
[data-testid="stSidebar"] { background: #080c12; border-right: 1px solid var(--os-line); }
[data-testid="stSidebarNav"] span { color: var(--os-text); }
[data-testid="stSidebarNav"] a[aria-current="page"] {
  background: var(--os-blue-soft);
  border-left: 2px solid var(--os-blue);
}

.block-container { max-width: 1440px; padding-top: 2rem; padding-bottom: 4rem; }
.os-kicker { color: #72a7ff; font-size: .75rem; font-weight: 750; letter-spacing: .14em; text-transform: uppercase; }
.os-title { color: var(--os-text); font-size: clamp(1.8rem, 3vw, 2.8rem); font-weight: 760; letter-spacing: -.04em; margin: .25rem 0 .35rem; }
.os-subtitle { color: var(--os-muted); font-size: .95rem; max-width: 54rem; margin-bottom: 1.35rem; }
.os-note {
  background: linear-gradient(135deg, rgba(16, 38, 74, .78), rgba(12, 17, 24, .92));
  border: 1px solid #234d87;
  border-radius: 12px;
  color: #b9d3ff;
  padding: .8rem 1rem;
  margin: .4rem 0 1.2rem;
}

[data-testid="stMetric"] {
  background: linear-gradient(145deg, rgba(16, 23, 34, .96), rgba(12, 17, 24, .96));
  border: 1px solid var(--os-line);
  border-radius: 14px;
  padding: 1rem 1.05rem;
}
[data-testid="stMetric"] label { color: var(--os-muted); }
[data-testid="stMetricValue"] { color: var(--os-text); }

[data-testid="stVerticalBlockBorderWrapper"] {
  background: rgba(12, 17, 24, .88);
  border-color: var(--os-line) !important;
  border-radius: 14px;
}

.stButton > button[kind="primary"] {
  background: var(--os-blue);
  border-color: #67a0ff;
  color: white;
}
.stButton > button[kind="secondary"] {
  background: var(--os-panel-2);
  border-color: var(--os-line);
  color: var(--os-text);
}
.stButton > button:hover { border-color: var(--os-blue); }

[data-testid="stDataFrame"] {
  background: var(--os-panel);
  border: 1px solid var(--os-line);
  border-radius: 12px;
  overflow: hidden;
}

[data-testid="stAlert"] { border-radius: 12px; }
hr { border-color: var(--os-line); }

@keyframes os-enter {
  from { opacity: 0; transform: translateY(5px); }
  to { opacity: 1; transform: translateY(0); }
}
.os-title, .os-subtitle, [data-testid="stMetric"] { animation: os-enter .28s ease-out both; }

@media (prefers-reduced-motion: reduce) {
  .os-title, .os-subtitle, [data-testid="stMetric"] { animation: none; }
}

@media (max-width: 768px) {
  .block-container { padding-top: 1.25rem; padding-left: 1rem; padding-right: 1rem; }
  .os-title { font-size: 1.8rem; }
  .os-subtitle { margin-bottom: 1rem; }
  [data-testid="stMetric"] { padding: .8rem .85rem; }
  .os-note { padding: .7rem .8rem; }
}
</style>
"""
