"""Shared visual components: CSS, KPI cards, status pills, chart styling."""

import html

import altair as alt
import pandas as pd
import streamlit as st

PRIMARY = "#29B5E8"
NAVY = "#11567F"
GOOD = "#10B981"
WARN = "#F59E0B"
BAD = "#EF4444"
MUTED = "#64748B"

STATUS_COLORS = {"PASS": GOOD, "FAIL": BAD, "ERROR": WARN}
SEVERITY_COLORS = {"CRITICAL": "#B91C1C", "HIGH": BAD, "MEDIUM": WARN, "LOW": MUTED}
SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
DIMENSIONS = ["Completeness", "Uniqueness", "Validity", "Consistency", "Timeliness", "Volume"]

_CSS = """
<style>
.block-container {padding-top: 2rem; padding-bottom: 3rem;}
.dq-hero {
  background: linear-gradient(120deg, #11567F 0%, #1A7FB5 55%, #29B5E8 100%);
  border-radius: 18px; padding: 26px 30px; color: white; margin-bottom: 1.2rem;
  box-shadow: 0 10px 30px rgba(17, 86, 127, .25);
}
.dq-hero h1 {color: white; font-size: 1.9rem; margin: 0 0 4px 0; padding: 0;}
.dq-hero p {color: rgba(255,255,255,.85); margin: 0; font-size: .98rem;}
.dq-card {
  background: white; border: 1px solid #E2E8F0; border-radius: 14px;
  padding: 16px 18px; height: 100%; position: relative; overflow: hidden;
  box-shadow: 0 1px 3px rgba(15, 23, 42, .06);
}
.dq-card:before {content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 5px; background: var(--accent);}
.dq-card .lbl {font-size: .78rem; text-transform: uppercase; letter-spacing: .06em; color: #64748B; font-weight: 600;}
.dq-card .val {font-size: 2rem; font-weight: 700; color: #0F172A; line-height: 1.2; margin-top: 4px;}
.dq-card .sub {font-size: .82rem; color: #64748B; margin-top: 2px;}
.dq-pill {display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: .75rem; font-weight: 700; color: white;}
.dq-issue {
  border: 1px solid #E2E8F0; border-left: 5px solid var(--accent); border-radius: 10px;
  padding: 10px 14px; margin-bottom: 8px; background: white;
}
.dq-issue .t {font-weight: 600; color: #0F172A;}
.dq-issue .m {font-size: .82rem; color: #64748B;}
.dq-section {font-size: 1.05rem; font-weight: 700; color: #11567F; margin: .4rem 0 .2rem 0;}
</style>
"""


def inject_css():
    st.markdown(_CSS, unsafe_allow_html=True)


def hero(title: str, subtitle: str):
    st.markdown(
        f'<div class="dq-hero"><h1>{html.escape(title)}</h1><p>{html.escape(subtitle)}</p></div>',
        unsafe_allow_html=True,
    )


def score_color(score) -> str:
    if score is None or pd.isna(score):
        return MUTED
    return GOOD if score >= 95 else WARN if score >= 80 else BAD


def kpi(label: str, value, sub: str = "", accent: str = PRIMARY):
    st.markdown(
        f'<div class="dq-card" style="--accent:{accent}"><div class="lbl">{html.escape(label)}</div>'
        f'<div class="val">{html.escape(str(value))}</div><div class="sub">{html.escape(sub)}</div></div>',
        unsafe_allow_html=True,
    )


def pill(text: str, color: str) -> str:
    return f'<span class="dq-pill" style="background:{color}">{html.escape(str(text))}</span>'


def issue_card(title: str, meta: str, severity: str, status: str):
    color = SEVERITY_COLORS.get(severity, MUTED)
    st.markdown(
        f'<div class="dq-issue" style="--accent:{color}">'
        f'{pill(severity, color)} {pill(status, STATUS_COLORS.get(status, MUTED))} '
        f'<span class="t">&nbsp;{html.escape(title)}</span><div class="m">{html.escape(meta)}</div></div>',
        unsafe_allow_html=True,
    )


def section(title: str):
    st.markdown(f'<div class="dq-section">{html.escape(title)}</div>', unsafe_allow_html=True)


def fmt_int(n) -> str:
    return "—" if n is None or pd.isna(n) else f"{int(n):,}"


def fmt_ts(ts) -> str:
    if ts is None or pd.isna(ts):
        return "never"
    return pd.Timestamp(ts).strftime("%b %d, %H:%M")


def status_scale():
    return alt.Scale(domain=list(STATUS_COLORS), range=list(STATUS_COLORS.values()))


def score_scale():
    return alt.Scale(domain=[50, 80, 95, 100], range=[BAD, WARN, "#A3E635", GOOD], clamp=True)


def score_trend_chart(df: pd.DataFrame, x="RUN_DATE", y="DQ_SCORE", height=260):
    base = alt.Chart(df).encode(x=alt.X(f"{x}:T", title=None, axis=alt.Axis(format="%b %d")))
    area = base.mark_area(
        clip=True,
        line={"color": PRIMARY, "strokeWidth": 2.5},
        color=alt.Gradient(
            gradient="linear",
            stops=[alt.GradientStop(color="rgba(41,181,232,0.35)", offset=0),
                   alt.GradientStop(color="rgba(41,181,232,0.02)", offset=1)],
            x1=1, x2=1, y1=0, y2=1,
        ),
    ).encode(y=alt.Y(f"{y}:Q", title="DQ score", scale=alt.Scale(domain=[max(0, float(df[y].min()) - 5) if len(df) else 0, 100])))
    points = base.mark_circle(size=45, color=NAVY, clip=True).encode(
        y=f"{y}:Q", tooltip=[alt.Tooltip(f"{x}:T", title="Date"), alt.Tooltip(f"{y}:Q", title="Score", format=".1f")]
    )
    target = alt.Chart(pd.DataFrame({"t": [95]})).mark_rule(strokeDash=[5, 4], color=GOOD).encode(y="t:Q")
    return (area + points + target).properties(height=height, padding={"top": 8})
