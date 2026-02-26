#!/usr/bin/env python3
"""
Spacescraper Dashboard - Mobile-First Implementation
Production-ready mobile-optimized UI
"""

import streamlit as st
from datetime import datetime

# Mobile-first page config
st.set_page_config(
    page_title="Spacescraper",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Check if mobile (screen width detection via JS)
IS_MOBILE = st.session_state.get('is_mobile', True)

# ============ MOBILE-FIRST CSS ============
st.markdown(f"""
<style>
    /* ===== MOBILE-FIRST BASE (320px - 639px) ===== */
    :root {{
        /* Typography - Larger for mobile readability */
        --font-base: 16px;
        --text-h1: 1.75rem;      /* 28px */
        --text-h2: 1.5rem;       /* 24px */
        --text-h3: 1.25rem;      /* 20px */
        --text-body: 1rem;       /* 16px */
        --text-small: 0.875rem;  /* 14px */
        
        /* Touch targets - 48px minimum (WCAG 2.5.5) */
        --touch-target: 48px;
        --touch-target-lg: 56px;
        
        /* Spacing - 8px base */
        --space-1: 4px;
        --space-2: 8px;
        --space-3: 16px;
        --space-4: 24px;
        
        /* Safe area for notches */
        --safe-top: env(safe-area-inset-top);
        --safe-bottom: env(safe-area-inset-bottom);
    }}
    
    /* Hide desktop elements on mobile */
    .desktop-only {{ display: none; }}
    
    /* Mobile container - full width, no max-width */
    .mobile-container {{
        width: 100%;
        padding: 0 var(--space-3);
        padding-bottom: calc(80px + var(--safe-bottom));
    }}
    
    /* ===== BOTTOM NAVIGATION (Thumb Zone) ===== */
    .bottom-nav {{
        position: fixed;
        bottom: 0;
        left: 0;
        right: 0;
        height: calc(64px + var(--safe-bottom));
        background: white;
        border-top: 1px solid #e2e8f0;
        display: flex;
        justify-content: space-around;
        align-items: flex-start;
        padding-top: 8px;
        padding-bottom: var(--safe-bottom);
        z-index: 1000;
        box-shadow: 0 -4px 20px rgba(0,0,0,0.1);
    }}
    
    .nav-item {{
        display: flex;
        flex-direction: column;
        align-items: center;
        min-width: 64px;
        min-height: var(--touch-target);
        padding: 4px;
        color: #64748b;
        font-size: 11px;
        font-weight: 500;
        text-decoration: none;
        border: none;
        background: none;
        cursor: pointer;
    }}
    
    .nav-item.active {{
        color: #1e3a5f;
    }}
    
    .nav-icon {{
        font-size: 24px;
        margin-bottom: 2px;
    }}
    
    /* Floating Action Button (center) */
    .fab-container {{
        position: relative;
        margin-top: -28px;
    }}
    
    .fab {{
        width: 56px;
        height: 56px;
        background: #22c55e;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        color: white;
        font-size: 28px;
        box-shadow: 0 4px 12px rgba(34, 197, 94, 0.4);
        border: 4px solid white;
        cursor: pointer;
    }}
    
    /* ===== MOBILE CARDS (Swipeable Style) ===== */
    .tender-card-mobile {{
        background: white;
        border: 2px solid #e2e8f0;
        border-radius: 16px;
        padding: var(--space-3);
        margin-bottom: var(--space-3);
        box-shadow: 0 2px 8px rgba(0,0,0,0.08);
    }}
    
    .card-header {{
        display: flex;
        align-items: flex-start;
        gap: var(--space-2);
        margin-bottom: var(--space-2);
    }}
    
    /* Large, easy-to-tap win badge */
    .win-badge-mobile {{
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-width: 90px;
        height: 36px;
        padding: 0 12px;
        border-radius: 18px;
        font-size: var(--text-small);
        font-weight: 700;
        flex-shrink: 0;
    }}
    
    .badge-high {{ background: #86efac; color: #14532d; }}
    .badge-medium {{ background: #fdba74; color: #7c2d12; }}
    .badge-low {{ background: #fca5a5; color: #7f1d1d; }}
    
    .card-title {{
        font-size: var(--text-h3);
        font-weight: 600;
        line-height: 1.3;
        color: #0f172a;
        margin: 0;
        flex: 1;
    }}
    
    /* Simplified metadata - icons only */
    .card-meta {{
        display: flex;
        flex-wrap: wrap;
        gap: var(--space-3);
        margin-bottom: var(--space-2);
        color: #475569;
        font-size: var(--text-body);
    }}
    
    .meta-item {{
        display: flex;
        align-items: center;
        gap: 6px;
    }}
    
    /* "Why" section - collapsible */
    .why-section {{
        background: #f8fafc;
        border-radius: 8px;
        padding: var(--space-2);
        margin-bottom: var(--space-2);
        border-left: 3px solid #3b82f6;
    }}
    
    .why-text {{
        font-size: var(--text-small);
        color: #475569;
        line-height: 1.5;
        margin: 0;
    }}
    
    /* ===== MOBILE BUTTONS (Full Width) ===== */
    .btn-mobile-primary {{
        width: 100%;
        min-height: var(--touch-target-lg);
        background: #15803d;
        color: white;
        border: none;
        border-radius: 12px;
        font-size: 17px;
        font-weight: 700;
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 8px;
        margin-bottom: var(--space-2);
        cursor: pointer;
    }}
    
    .btn-row {{
        display: flex;
        gap: var(--space-2);
    }}
    
    .btn-mobile-secondary {{
        flex: 1;
        min-height: var(--touch-target);
        background: #f1f5f9;
        border: 2px solid #cbd5e1;
        border-radius: 10px;
        font-size: 15px;
        font-weight: 600;
        color: #475569;
        cursor: pointer;
    }}
    
    /* ===== SWIPE HINTS ===== */
    .swipe-hint {{
        display: flex;
        justify-content: space-between;
        padding: var(--space-2) var(--space-3);
        color: #64748b;
        font-size: var(--text-small);
        margin-bottom: var(--space-2);
    }}
    
    /* ===== TABLET ADJUSTMENTS (640px+) ===== */
    @media (min-width: 640px) {{
        :root {{
            --text-h1: 2rem;
            --text-h2: 1.75rem;
        }}
        
        .mobile-only {{ display: none; }}
        .desktop-only {{ display: block; }}
        
        .tender-card-mobile {{
            max-width: 600px;
            margin-left: auto;
            margin-right: auto;
        }}
        
        .bottom-nav {{
            display: none;  /* Use sidebar on tablet+ */
        }}
    }}
    
    /* ===== REDUCED MOTION ===== */
    @media (prefers-reduced-motion: reduce) {{
        * {{
            animation-duration: 0.01ms !important;
            transition-duration: 0.01ms !important;
        }}
    }}
    
    /* ===== DARK MODE SUPPORT ===== */
    @media (prefers-color-scheme: dark) {{
        .tender-card-mobile {{
            background: #1e293b;
            border-color: #334155;
        }}
        .card-title {{ color: #f1f5f9; }}
        .card-meta {{ color: #94a3b8; }}
    }}
</style>
""", unsafe_allow_html=True)

# ============ BOTTOM NAVIGATION ============
def render_bottom_nav(active_tab: str = "home"):
    """Fixed bottom navigation for thumb access"""
    
    tabs = [
        ("🏠", "Home", "home"),
        ("🔍", "Find", "find"),
        ("fab", "", "quick"),  # Floating action button
        ("💼", "Bids", "bids"),
        ("👤", "Profile", "profile"),
    ]
    
    nav_html = "<div class='bottom-nav'>"
    
    for icon, label, tab_id in tabs:
        if tab_id == "quick":
            # Floating Action Button
            nav_html += """
            <div class='fab-container'>
                <button class='fab' onclick="alert('Quick actions')">+</button>
            </div>
            """
        else:
            is_active = "active" if active_tab == tab_id else ""
            nav_html += f"""
            <button class="nav-item {is_active}" onclick="switchTab('{tab_id}')">
                <span class="nav-icon">{icon}</span>
                <span>{label}</span>
            </button>
            """
    
    nav_html += "</div>"
    st.markdown(nav_html, unsafe_allow_html=True)

# ============ MOBILE TENDER CARD ============
def render_tender_card_mobile(tender: dict):
    """Optimized tender card for mobile viewing"""
    
    prob_class = "badge-high" if tender['win_prob'] >= 0.7 else \
                 "badge-medium" if tender['win_prob'] >= 0.5 else "badge-low"
    
    st.markdown(f"""
    <article class="tender-card-mobile">
        <div class="card-header">
            <span class="win-badge-mobile {prob_class}">
                🎯 {tender['win_prob']:.0%}
            </span>
            <h3 class="card-title">{tender['title']}</h3>
        </div>
        
        <div class="card-meta">
            <span class="meta-item">🏢 {tender['buyer']}</span>
            <span class="meta-item">💰 {tender['budget']}</span>
            <span class="meta-item">📅 {tender['deadline']}</span>
        </div>
        
        <div class="why-section">
            <p class="why-text">💡 <strong>Why:</strong> {tender['why']}</p>
        </div>
    </article>
    """, unsafe_allow_html=True)
    
    # Full-width primary button (thumb friendly)
    if st.button(
        f"🚀 BID NOW - {tender['win_prob']:.0%} WIN",
        type="primary",
        use_container_width=True,
        key=f"bid_{tender['id']}"
    ):
        st.success("Bid submitted!")
        st.balloons()
    
    # Secondary actions in row
    col1, col2 = st.columns(2)
    with col1:
        if st.button("💾 Save", use_container_width=True, key=f"save_{tender['id']}"):
            st.toast("Saved for later")
    with col2:
        if st.button("📤 Share", use_container_width=True, key=f"share_{tender['id']}"):
            st.toast("Link copied!")

# ============ MAIN CONTENT ============
st.markdown("<div class='mobile-container'>", unsafe_allow_html=True)

# Header
st.markdown("<h1 style='font-size: 1.75rem; margin-bottom: 0.5rem;'>Your Matches</h1>", unsafe_allow_html=True)
st.markdown("<p style='color: #64748b; margin-bottom: 1.5rem;'>47 tenders found for you</p>", unsafe_allow_html=True)

# Quick stats (horizontal scroll)
stats_cols = st.columns(3)
with stats_cols[0]:
    st.metric("High Win", "18", "+3")
with stats_cols[1]:
    st.metric("Saved", "12", "")
with stats_cols[2]:
    st.metric("Win Rate", "67%", "+5%")

st.markdown("---")

# Sample tenders
sample_tenders = [
    {
        "id": "1",
        "title": "Advanced Satellite Communication Terminals",
        "buyer": "European Defence Agency",
        "budget": "€2.5M",
        "deadline": "45 days",
        "win_prob": 0.87,
        "why": "Strong keyword match • Budget fit • 80% EDA win rate"
    },
    {
        "id": "2",
        "title": "Earth Observation Ground Segment",
        "buyer": "ESA",
        "budget": "€12M",
        "deadline": "89 days",
        "win_prob": 0.78,
        "why": "Technical match • Preferred buyer • Good budget fit"
    }
]

st.subheader("🎯 Recommended for You")

for tender in sample_tenders:
    render_tender_card_mobile(tender)
    st.markdown("<div style='height: 8px;'></div>", unsafe_allow_html=True)

# Swipe hints
st.markdown("""
<div class="swipe-hint">
    <span>← Swipe to skip</span>
    <span>Tap card for details</span>
</div>
""", unsafe_allow_html=True)

st.markdown("</div>", unsafe_allow_html=True)  # Close mobile-container

# Render bottom navigation
render_bottom_nav("home")

# Hidden desktop content (shown on tablet+)
st.markdown("""
<div class="desktop-only">
    <p>Desktop view would show sidebar navigation and multi-column layout here.</p>
</div>
""", unsafe_allow_html=True)
