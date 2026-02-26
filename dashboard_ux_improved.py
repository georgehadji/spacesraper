#!/usr/bin/env python3
"""
Spacescraper Dashboard - UX Improved Version
Based on UX Research findings:
- Reduced cognitive load
- 2-click task completion
- Progressive disclosure
"""

import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, timedelta

st.set_page_config(
    page_title="Spacescraper",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="collapsed"  # UX: More screen space
)

# ============ CUSTOM CSS (UX Optimized) ============
st.markdown("""
<style>
    /* === REDUCED COGNITIVE LOAD DESIGN === */
    
    /* Hide default Streamlit elements that add noise */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    
    /* Clean header */
    .main-header {
        font-size: 2rem;
        font-weight: 700;
        color: #1e3a5f;
        margin-bottom: 0.5rem;
    }
    
    .sub-header {
        color: #64748b;
        font-size: 1rem;
        margin-bottom: 2rem;
    }
    
    /* === UX IMPROVEMENT 1: Compact Tender Cards === */
    .tender-card-compact {
        background: white;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 16px;
        margin: 12px 0;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        transition: all 0.2s;
    }
    
    .tender-card-compact:hover {
        box-shadow: 0 4px 12px rgba(0,0,0,0.15);
        transform: translateY(-2px);
    }
    
    .tender-header {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        margin-bottom: 8px;
        gap: 12px;
    }
    
    .tender-title {
        font-size: 1.1rem;
        font-weight: 600;
        color: #1e293b;
        line-height: 1.4;
        flex: 1;
    }
    
    /* Win badge: Combined score + grade */
    .win-badge {
        padding: 6px 12px;
        border-radius: 20px;
        font-size: 0.875rem;
        font-weight: 700;
        white-space: nowrap;
        display: flex;
        align-items: center;
        gap: 4px;
    }
    
    .win-high { 
        background: #dcfce7; 
        color: #166534; 
        border: 2px solid #22c55e;
    }
    .win-medium { 
        background: #fef3c7; 
        color: #92400e; 
        border: 2px solid #f59e0b;
    }
    .win-low { 
        background: #fee2e2; 
        color: #991b1b; 
        border: 2px solid #ef4444;
    }
    
    /* Icon-based metadata (reduces text) */
    .tender-meta {
        display: flex;
        gap: 20px;
        color: #64748b;
        font-size: 0.9rem;
        margin-bottom: 12px;
    }
    
    .meta-item {
        display: flex;
        align-items: center;
        gap: 6px;
    }
    
    /* Action buttons: Clear hierarchy */
    .action-bar {
        display: flex;
        gap: 8px;
        align-items: center;
    }
    
    .btn-bid {
        background: #22c55e !important;
        color: white !important;
        font-weight: 600 !important;
        padding: 8px 20px !important;
        border-radius: 8px !important;
        border: none !important;
        cursor: pointer;
        font-size: 0.95rem;
    }
    
    .btn-bid:hover {
        background: #16a34a !important;
    }
    
    .btn-secondary {
        background: #f1f5f9 !important;
        color: #475569 !important;
        padding: 8px 16px !important;
        border-radius: 8px !important;
        border: 1px solid #cbd5e1 !important;
        font-size: 0.9rem;
    }
    
    /* === UX IMPROVEMENT 2: Fixed Action Bar === */
    .fixed-action-bar {
        position: fixed;
        bottom: 0;
        left: 0;
        right: 0;
        background: white;
        border-top: 1px solid #e2e8f0;
        padding: 12px 24px;
        display: flex;
        justify-content: center;
        gap: 16px;
        z-index: 999;
        box-shadow: 0 -4px 12px rgba(0,0,0,0.1);
    }
    
    .action-btn {
        padding: 12px 32px;
        border-radius: 8px;
        font-weight: 600;
        cursor: pointer;
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 1rem;
    }
    
    .action-btn-skip {
        background: #f1f5f9;
        color: #64748b;
        border: 1px solid #cbd5e1;
    }
    
    .action-btn-save {
        background: #dbeafe;
        color: #1e40af;
        border: 1px solid #3b82f6;
    }
    
    .action-btn-bid {
        background: #22c55e;
        color: white;
        border: none;
        font-size: 1.1rem;
        padding: 12px 48px;
    }
    
    /* === UX IMPROVEMENT 3: Progressive Onboarding === */
    .wizard-step {
        background: white;
        border-radius: 16px;
        padding: 40px;
        max-width: 600px;
        margin: 0 auto;
        box-shadow: 0 4px 20px rgba(0,0,0,0.1);
    }
    
    .wizard-header {
        text-align: center;
        margin-bottom: 32px;
    }
    
    .wizard-progress {
        display: flex;
        justify-content: center;
        gap: 8px;
        margin-bottom: 32px;
    }
    
    .progress-dot {
        width: 12px;
        height: 12px;
        border-radius: 50%;
        background: #e2e8f0;
    }
    
    .progress-dot.active {
        background: #1e3a5f;
    }
    
    .progress-dot.completed {
        background: #22c55e;
    }
    
    .smart-default-chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        background: #dbeafe;
        color: #1e40af;
        padding: 6px 12px;
        border-radius: 16px;
        font-size: 0.875rem;
        margin: 4px;
        cursor: pointer;
    }
    
    .smart-default-chip:hover {
        background: #bfdbfe;
    }
</style>
""", unsafe_allow_html=True)

# ============ SESSION STATE ============
if 'onboarding_complete' not in st.session_state:
    st.session_state.onboarding_complete = False
if 'onboarding_step' not in st.session_state:
    st.session_state.onboarding_step = 1
if 'profile' not in st.session_state:
    st.session_state.profile = {}
if 'current_tender_idx' not in st.session_state:
    st.session_state.current_tender_idx = 0
if 'saved_tenders' not in st.session_state:
    st.session_state.saved_tenders = []
if 'bid_tenders' not in st.session_state:
    st.session_state.bid_tenders = []

# ============ ONBOARDING WIZARD (UX Improvement #3) ============
def show_onboarding():
    """3-step progressive onboarding with smart defaults"""
    
    step = st.session_state.onboarding_step
    
    # Progress indicator
    st.markdown(f"""
    <div class="wizard-progress">
        <div class="progress-dot {'active' if step == 1 else 'completed'}"></div>
        <div class="progress-dot {'active' if step == 2 else 'completed' if step > 2 else ''}"></div>
        <div class="progress-dot {'active' if step == 3 else ''}"></div>
    </div>
    """, unsafe_allow_html=True)
    
    if step == 1:
        st.markdown("""
        <div class="wizard-step">
            <div class="wizard-header">
                <h2>👋 Welcome to Spacescraper</h2>
                <p style="color: #64748b;">Let's find tenders that match your expertise</p>
            </div>
        </div>
        """, unsafe_allow_html=True)
        
        st.subheader("What are your main capabilities?")
        st.caption("Add 3-5 keywords for best matching")
        
        # Smart default suggestions
        st.markdown("**💡 Popular in your industry:**")
        cols = st.columns(5)
        suggestions = ["satellite", "communication", "defense", "optical", "AI"]
        for i, suggestion in enumerate(suggestions):
            if cols[i].button(f"➕ {suggestion}", key=f"sugg_{i}"):
                if 'keywords' not in st.session_state.profile:
                    st.session_state.profile['keywords'] = []
                st.session_state.profile['keywords'].append(suggestion)
        
        keywords = st.text_input(
            "Your keywords",
            value=", ".join(st.session_state.profile.get('keywords', [])),
            placeholder="e.g., satellite, communication, RF"
        )
        
        if st.button("Next →", type="primary", use_container_width=True):
            st.session_state.profile['keywords'] = [k.strip() for k in keywords.split(",") if k.strip()]
            st.session_state.onboarding_step = 2
            st.rerun()
    
    elif step == 2:
        st.markdown("""
        <div class="wizard-step">
            <div class="wizard-header">
                <h2>💰 What's your sweet spot?</h2>
                <p style="color: #64748b;">We'll filter tenders in your comfort zone</p>
            </div>
        </div>
        """, unsafe_allow_html=True)
        
        # Smart defaults based on common ranges
        col1, col2 = st.columns(2)
        with col1:
            min_budget = st.selectbox(
                "Minimum project size",
                ["€100K", "€500K", "€1M", "€5M"],
                index=1  # Smart default: €500K
            )
        with col2:
            max_budget = st.selectbox(
                "Maximum project size",
                ["€1M", "€5M", "€10M", "€50M", "No limit"],
                index=2  # Smart default: €10M
            )
        
        st.markdown("### 🌍 Preferred regions")
        regions = st.multiselect(
            "Select regions",
            ["🇪🇺 EU", "🌐 NATO", "🇺🇸 US", "🌏 Asia-Pacific"],
            default=["🇪🇺 EU", "🌐 NATO"]  # Smart default
        )
        
        col_back, col_next = st.columns(2)
        with col_back:
            if st.button("← Back"):
                st.session_state.onboarding_step = 1
                st.rerun()
        with col_next:
            if st.button("Next →", type="primary", use_container_width=True):
                st.session_state.profile['budget'] = f"{min_budget} - {max_budget}"
                st.session_state.profile['regions'] = regions
                st.session_state.onboarding_step = 3
                st.rerun()
    
    elif step == 3:
        st.markdown(f"""
        <div class="wizard-step">
            <div class="wizard-header">
                <h2>🎉 You're all set!</h2>
                <p style="color: #64748b;">Based on your profile:</p>
            </div>
            <div style="text-align: center; margin: 32px 0;">
                <div style="font-size: 3rem; margin-bottom: 16px;">🎯</div>
                <div style="font-size: 2rem; font-weight: 700; color: #1e3a5f;">47</div>
                <div style="color: #64748b;">tenders match your profile</div>
                <br>
                <div style="font-size: 1.5rem; font-weight: 600; color: #22c55e;">67%</div>
                <div style="color: #64748b;">predicted win rate</div>
            </div>
        </div>
        """, unsafe_allow_html=True)
        
        if st.button("🚀 See My Matches", type="primary", use_container_width=True):
            st.session_state.onboarding_complete = True
            st.rerun()

# ============ MAIN DASHBOARD (With UX Improvements) ============
def show_dashboard():
    """Main dashboard with reduced cognitive load"""
    
    # Header
    st.markdown('<p class="main-header">Your Tender Matches</p>', unsafe_allow_html=True)
    st.markdown('<p class="sub-header">47 tenders found • Sorted by win probability</p>', unsafe_allow_html=True)
    
    # Top metrics (simplified to 3 most important)
    cols = st.columns(3)
    with cols[0]:
        st.metric("High Win Probability", "18", ">70%")
    with cols[1]:
        st.metric("Avg Match Score", "82%", "↑ 5%")
    with cols[2]:
        st.metric("This Week's Wins", "3", "↑ 1")
    
    st.markdown("---")
    
    # Sample tenders
    tenders = [
        {
            "title": "Advanced Satellite Communication Terminals for Defense Applications",
            "buyer": "European Defence Agency",
            "country": "🇧🇪 BE",
            "budget": "€2.5M",
            "deadline": "45 days",
            "win_prob": 0.87,
            "grade": "A+",
            "match": 0.92
        },
        {
            "title": "Earth Observation Ground Segment Infrastructure",
            "buyer": "ESA",
            "country": "🇫🇷 FR",
            "budget": "€12M",
            "deadline": "89 days",
            "win_prob": 0.78,
            "grade": "A",
            "match": 0.85
        },
        {
            "title": "Secure Military Satellite Communications Upgrade",
            "buyer": "NATO",
            "country": "🇳🇱 NL",
            "budget": "€45M",
            "deadline": "67 days",
            "win_prob": 0.62,
            "grade": "B+",
            "match": 0.71
        },
    ]
    
    # Get current tender index
    idx = st.session_state.current_tender_idx
    
    if idx < len(tenders):
        tender = tenders[idx]
        
        # UX IMPROVEMENT 1: Compact Card
        prob_class = "win-high" if tender['win_prob'] >= 0.7 else "win-medium" if tender['win_prob'] >= 0.5 else "win-low"
        
        st.markdown(f"""
        <div class="tender-card-compact">
            <div class="tender-header">
                <span class="tender-title">{tender['title']}</span>
                <span class="win-badge {prob_class}">
                    🎯 {tender['win_prob']:.0%} [{tender['grade']}]
                </span>
            </div>
            <div class="tender-meta">
                <span class="meta-item">🏢 {tender['buyer']}</span>
                <span class="meta-item">💰 {tender['budget']}</span>
                <span class="meta-item">📅 {tender['deadline']}</span>
                <span class="meta-item">{tender['country']}</span>
            </div>
            <div style="color: #64748b; font-size: 0.9rem; margin-bottom: 12px;">
                💡 <strong>Why:</strong> Strong keyword match • Budget fit • 80% win rate with this buyer
            </div>
        </div>
        """, unsafe_allow_html=True)
        
        # UX IMPROVEMENT 2: Fixed Action Bar (Simulated)
        col1, col2, col3 = st.columns([1, 1, 2])
        
        with col1:
            if st.button("✕ Skip", use_container_width=True):
                st.session_state.current_tender_idx += 1
                st.rerun()
        
        with col2:
            if st.button("⭐ Save", use_container_width=True):
                st.session_state.saved_tenders.append(tender)
                st.toast("Saved!")
        
        with col3:
            if st.button(f"🚀 BID NOW ({tender['win_prob']:.0%} win)", type="primary", use_container_width=True):
                st.session_state.bid_tenders.append(tender)
                st.session_state.current_tender_idx += 1
                st.balloons()
                st.success("Bid submitted! Moving to next tender...")
                st.rerun()
        
        # Show progress
        st.progress((idx + 1) / len(tenders))
        st.caption(f"Tender {idx + 1} of {len(tenders)}")
        
    else:
        st.success("🎉 You've reviewed all matching tenders!")
        st.info(f"You bid on {len(st.session_state.bid_tenders)} tenders and saved {len(st.session_state.saved_tenders)} for later.")

# ============ MAIN ============
if not st.session_state.onboarding_complete:
    show_onboarding()
else:
    show_dashboard()

# Sidebar (minimal, only when needed)
with st.sidebar:
    st.markdown("### 🚀 Spacescraper")
    st.markdown("---")
    st.markdown(f"**Bids Today:** {len(st.session_state.bid_tenders)}")
    st.markdown(f"**Saved:** {len(st.session_state.saved_tenders)}")
    
    if st.button("🔄 Restart Onboarding"):
        st.session_state.onboarding_complete = False
        st.session_state.onboarding_step = 1
        st.rerun()
