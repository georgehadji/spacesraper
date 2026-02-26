#!/usr/bin/env python3
"""
Spacescraper Web Dashboard
Interactive UI for the tender intelligence platform.
"""

import streamlit as st
import requests
import json
from datetime import datetime, timedelta
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

# Page config
st.set_page_config(
    page_title="Spacescraper Intelligence",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS
st.markdown("""
<style>
    .main-header {
        font-size: 2.5rem;
        font-weight: bold;
        color: #1f77b4;
    }
    .metric-card {
        background-color: #f0f2f6;
        border-radius: 10px;
        padding: 20px;
        margin: 10px 0;
    }
    .win-high {
        color: #00cc00;
        font-weight: bold;
    }
    .win-medium {
        color: #ffcc00;
        font-weight: bold;
    }
    .win-low {
        color: #ff4444;
        font-weight: bold;
    }
    .tender-card {
        background-color: #ffffff;
        border: 1px solid #e0e0e0;
        border-radius: 10px;
        padding: 20px;
        margin: 10px 0;
        box-shadow: 2px 2px 5px rgba(0,0,0,0.1);
    }
</style>
""", unsafe_allow_html=True)

# Session state
if 'api_key' not in st.session_state:
    st.session_state.api_key = "ss_demo_key"
if 'profile' not in st.session_state:
    st.session_state.profile = None
if 'tenders' not in st.session_state:
    st.session_state.tenders = []

# Sidebar
st.sidebar.markdown("## 🚀 Spacescraper")

# API Key input
api_key = st.sidebar.text_input(
    "API Key",
    value=st.session_state.api_key,
    type="password"
)
st.session_state.api_key = api_key

st.sidebar.markdown("---")

# Navigation
page = st.sidebar.radio(
    "Navigation",
    ["🏠 Dashboard", "🔍 Find Tenders", "📊 My Profile", "📈 Analytics", "⚙️ Settings"]
)

# Helper functions
def get_headers():
    return {"Authorization": f"Bearer {api_key}"}

# ==================== DASHBOARD PAGE ====================
if page == "🏠 Dashboard":
    st.markdown('<p class="main-header">Spacescraper Intelligence Dashboard</p>', unsafe_allow_html=True)
    
    # Metrics row
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric(
            label="Total Tenders",
            value="15,432",
            delta="+234 this week"
        )
    
    with col2:
        st.metric(
            label="High Quality (A/B)",
            value="8,921",
            delta="58% of total"
        )
    
    with col3:
        st.metric(
            label="Your Matches",
            value="47",
            delta="+12 this week"
        )
    
    with col4:
        st.metric(
            label="Win Rate",
            value="67%",
            delta="+5% vs last month"
        )
    
    st.markdown("---")
    
    # Recent Activity
    col_left, col_right = st.columns([2, 1])
    
    with col_left:
        st.subheader("🎯 Top Matches for You")
        
        # Sample matches (would come from API)
        sample_matches = [
            {
                "title": "Advanced Satellite Communication Terminals",
                "buyer": "European Defence Agency",
                "budget": "€2,500,000",
                "deadline": "2024-06-15",
                "win_prob": 0.87,
                "grade": "A+",
                "match_score": 0.92
            },
            {
                "title": "Earth Observation Ground Segment",
                "buyer": "ESA",
                "budget": "€12,000,000",
                "deadline": "2024-08-20",
                "win_prob": 0.78,
                "grade": "A",
                "match_score": 0.85
            },
            {
                "title": "Secure Military Comms Upgrade",
                "buyer": "NATO",
                "budget": "€45,000,000",
                "deadline": "2024-07-30",
                "win_prob": 0.62,
                "grade": "B+",
                "match_score": 0.71
            }
        ]
        
        for match in sample_matches:
            prob_class = "win-high" if match['win_prob'] >= 0.7 else "win-medium" if match['win_prob'] >= 0.5 else "win-low"
            
            st.markdown(f"""
            <div class="tender-card">
                <h4>{match['title']}</h4>
                <p><strong>Buyer:</strong> {match['buyer']} | 
                   <strong>Budget:</strong> {match['budget']} | 
                   <strong>Deadline:</strong> {match['deadline']}</p>
                <p><span class="{prob_class}">Win Probability: {match['win_prob']:.0%}</span> | 
                   Quality: {match['grade']} | 
                   Match Score: {match['match_score']:.0%}</p>
            </div>
            """, unsafe_allow_html=True)
    
    with col_right:
        st.subheader("📊 Quick Stats")
        
        # Win probability distribution
        fig = go.Figure(data=[go.Pie(
            labels=['High (>70%)', 'Medium (50-70%)', 'Low (<50%)'],
            values=[18, 21, 8],
            hole=.3,
            marker_colors=['#00cc00', '#ffcc00', '#ff4444']
        )])
        fig.update_layout(height=300, showlegend=True)
        st.plotly_chart(fig, use_container_width=True)
        
        st.markdown("### 📈 Trending Keywords")
        keywords = ["satellite", "AI", "defense", "communication", "optical"]
        counts = [45, 38, 32, 28, 24]
        
        for kw, count in zip(keywords, counts):
            st.markdown(f"- **{kw}**: {count} tenders")

# ==================== FIND TENDERS PAGE ====================
elif page == "🔍 Find Tenders":
    st.markdown('<p class="main-header">Find Matching Tenders</p>', unsafe_allow_html=True)
    
    col1, col2 = st.columns([1, 2])
    
    with col1:
        st.subheader("🔎 Filters")
        
        min_quality = st.slider("Minimum Quality Score", 0, 100, 70)
        min_win_prob = st.slider("Minimum Win Probability", 0.0, 1.0, 0.5)
        
        st.markdown("### 💰 Budget Range")
        budget_min = st.number_input("Min (EUR)", value=500000, step=100000)
        budget_max = st.number_input("Max (EUR)", value=10000000, step=1000000)
        
        st.markdown("### 🌍 Geography")
        regions = st.multiselect(
            "Regions",
            ["EU", "NATO", "US", "Asia-Pacific"],
            default=["EU", "NATO"]
        )
        
        st.markdown("### 🏷️ Keywords")
        keywords = st.text_input("Keywords (comma-separated)", "satellite, communication")
        
        if st.button("🔍 Search Tenders", type="primary", use_container_width=True):
            with st.spinner("Finding matches..."):
                # Would call API here
                st.success(f"Found 47 matching tenders!")
    
    with col2:
        st.subheader("📋 Results")
        
        # Sample results
        results = [
            {
                "title": "Supply of Advanced Satellite Communication Terminals",
                "buyer": "European Defence Agency",
                "country": "Belgium",
                "budget": "€2,500,000",
                "deadline": "2024-06-15",
                "win_prob": 0.87,
                "quality": 94,
                "grade": "A+",
                "match": 0.92
            },
            {
                "title": "Earth Observation Ground Segment",
                "buyer": "ESA",
                "country": "France",
                "budget": "€12,000,000",
                "deadline": "2024-08-20",
                "win_prob": 0.78,
                "quality": 88,
                "grade": "A",
                "match": 0.85
            }
        ]
        
        for tender in results:
            with st.expander(f"{tender['title'][:50]}... (Win: {tender['win_prob']:.0%})"):
                col_a, col_b, col_c = st.columns(3)
                
                with col_a:
                    st.markdown(f"**Buyer:** {tender['buyer']}")
                    st.markdown(f"**Country:** {tender['country']}")
                
                with col_b:
                    st.markdown(f"**Budget:** {tender['budget']}")
                    st.markdown(f"**Deadline:** {tender['deadline']}")
                
                with col_c:
                    st.markdown(f"**Quality:** {tender['grade']} ({tender['quality']})")
                    st.markdown(f"**Match:** {tender['match']:.0%}")
                
                st.markdown("---")
                st.markdown("**Why this matches you:**")
                st.markdown("- Strong keyword match (satellite, communication)")
                st.markdown("- Budget in your preferred range")
                st.markdown("- Located in preferred geography (EU)")
                st.markdown("- High historical win rate with this buyer")
                
                col_x, col_y = st.columns(2)
                with col_x:
                    st.button("📎 View Details", key=f"view_{tender['title'][:20]}")
                with col_y:
                    st.button("✅ I'm Bidding", key=f"bid_{tender['title'][:20]}", type="primary")

# ==================== PROFILE PAGE ====================
elif page == "📊 My Profile":
    st.markdown('<p class="main-header">My Capability Profile</p>', unsafe_allow_html=True)
    
    col1, col2 = st.columns([1, 1])
    
    with col1:
        st.subheader("🏢 Organization Details")
        
        org_name = st.text_input("Organization Name", "SpaceTech Solutions GmbH")
        
        st.markdown("### 🎯 Capabilities")
        keywords = st.text_area(
            "Keywords (one per line)",
            "satellite\ncommunication\nground station\nRF\nantenna\noptical"
        )
        
        industries = st.multiselect(
            "Industries",
            ["space", "defense", "telecommunications", "dual-use"],
            default=["space", "defense"]
        )
        
        services = st.multiselect(
            "Services",
            ["manufacturing", "consulting", "system integration", "R&D"],
            default=["manufacturing", "consulting"]
        )
        
        st.markdown("### 💰 Financial")
        min_budget = st.number_input("Minimum Budget (EUR)", value=500000, step=100000)
        max_budget = st.number_input("Maximum Budget (EUR)", value=15000000, step=1000000)
        
        st.markdown("### 🌍 Geography")
        geo_focus = st.multiselect(
            "Preferred Regions",
            ["EU", "NATO", "US", "APAC"],
            default=["EU", "NATO"]
        )
        
        if st.button("💾 Save Profile", type="primary"):
            st.success("Profile saved successfully!")
    
    with col2:
        st.subheader("📈 Your Performance")
        
        # Win rate chart
        months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun']
        win_rates = [55, 58, 62, 64, 67, 70]
        
        fig = px.line(
            x=months, 
            y=win_rates,
            labels={'x': 'Month', 'y': 'Win Rate (%)'},
            title="Win Rate Trend"
        )
        fig.update_layout(height=300)
        st.plotly_chart(fig, use_container_width=True)
        
        st.markdown("### 🏆 Wins by Buyer")
        buyers = ['ESA', 'EDA', 'NATO', 'National']
        wins = [8, 5, 3, 4]
        total_bids = [12, 8, 7, 10]
        
        fig2 = go.Figure()
        fig2.add_trace(go.Bar(name='Wins', x=buyers, y=wins, marker_color='green'))
        fig2.add_trace(go.Bar(name='Losses', x=buyers, y=[t-w for t,w in zip(total_bids, wins)], marker_color='red'))
        fig2.update_layout(barmode='stack', height=300, title="Bid Outcomes by Buyer")
        st.plotly_chart(fig2, use_container_width=True)
        
        st.markdown("### 📊 Summary Statistics")
        stats_col1, stats_col2, stats_col3 = st.columns(3)
        
        with stats_col1:
            st.metric("Total Bids", 37)
        with stats_col2:
            st.metric("Wins", 20)
        with stats_col3:
            st.metric("Win Rate", "54%")

# ==================== ANALYTICS PAGE ====================
elif page == "📈 Analytics":
    st.markdown('<p class="main-header">Market Analytics</p>', unsafe_allow_html=True)
    
    tab1, tab2, tab3 = st.tabs(["Market Trends", "Competitive Intelligence", "Quality Analysis"])
    
    with tab1:
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("Tenders by Month")
            months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun']
            tender_counts = [234, 245, 289, 312, 298, 334]
            
            fig = px.bar(
                x=months, 
                y=tender_counts,
                labels={'x': 'Month', 'y': 'Tenders'},
                color=tender_counts,
                color_continuous_scale='blues'
            )
            st.plotly_chart(fig, use_container_width=True)
        
        with col2:
            st.subheader("Budget Distribution")
            budgets = ['<1M', '1-5M', '5-10M', '10-50M', '>50M']
            counts = [45, 67, 34, 28, 12]
            
            fig = px.pie(
                values=counts,
                names=budgets,
                hole=0.4
            )
            st.plotly_chart(fig, use_container_width=True)
        
        st.subheader("Trending Keywords")
        keywords = ['satellite', 'AI', 'defense', 'communication', 'optical', 'cyber', 'quantum']
        trend = [85, 78, 72, 68, 55, 45, 32]
        
        fig = px.bar(
            x=keywords,
            y=trend,
            orientation='v',
            color=trend,
            color_continuous_scale='viridis'
        )
        fig.update_layout(height=400)
        st.plotly_chart(fig, use_container_width=True)
    
    with tab2:
        st.subheader("🏆 Top Competitors")
        
        competitors = pd.DataFrame({
            'Company': ['Airbus Defence', 'Thales', 'Leonardo', 'BAE Systems', 'Northrop Grumman'],
            'Wins (6mo)': [24, 19, 16, 14, 12],
            'Avg Budget': ['€12M', '€8M', '€6M', '€15M', '€20M'],
            'Win Rate': ['32%', '28%', '25%', '22%', '18%']
        })
        
        st.dataframe(competitors, use_container_width=True)
        
        st.subheader("Win Rate by Category")
        categories = ['Space', 'Defense', 'Dual-Use', 'Commercial']
        your_rate = [65, 58, 52, 35]
        market_avg = [45, 42, 38, 40]
        
        fig = go.Figure()
        fig.add_trace(go.Bar(name='Your Win Rate', x=categories, y=your_rate, marker_color='green'))
        fig.add_trace(go.Bar(name='Market Average', x=categories, y=market_avg, marker_color='gray'))
        fig.update_layout(barmode='group', height=400)
        st.plotly_chart(fig, use_container_width=True)
    
    with tab3:
        st.subheader("Data Quality Distribution")
        
        grades = ['A+', 'A', 'B', 'C', 'D', 'F']
        counts = [234, 567, 1234, 2345, 4567, 6543]
        colors = ['#00cc00', '#66ff66', '#ffff00', '#ffcc00', '#ff6666', '#ff0000']
        
        fig = px.bar(
            x=grades,
            y=counts,
            color=grades,
            color_discrete_sequence=colors
        )
        fig.update_layout(height=400)
        st.plotly_chart(fig, use_container_width=True)
        
        st.markdown("### Quality Issues by Category")
        issues = pd.DataFrame({
            'Issue': ['Missing deadline', 'No budget specified', 'Low description', 'No buyer info', 'Unclear scope'],
            'Count': [3456, 2890, 5678, 1234, 2345],
            '% of Total': ['22%', '19%', '37%', '8%', '15%']
        })
        st.dataframe(issues, use_container_width=True)

# ==================== SETTINGS PAGE ====================
elif page == "⚙️ Settings":
    st.markdown('<p class="main-header">Settings</p>', unsafe_allow_html=True)
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("🔐 API Configuration")
        
        st.text_input("API Key", value=st.session_state.api_key, type="password")
        st.text_input("API Endpoint", value="http://localhost:8000")
        
        st.markdown("### 📧 Notifications")
        st.checkbox("Email alerts for new high-probability matches")
        st.checkbox("Daily digest of top 10 tenders")
        st.checkbox("Alert when deadline approaching (7 days)")
        
        st.markdown("### 🔔 Webhooks")
        st.text_input("Webhook URL", placeholder="https://hooks.yourcompany.com/spacescraper")
        st.selectbox("Webhook Events", ["All events", "Only wins", "Only high-probability matches"])
    
    with col2:
        st.subheader("📊 Display Preferences")
        
        st.selectbox("Default Sort", ["Win Probability", "Match Score", "Deadline", "Budget"])
        st.selectbox("Currency", ["EUR", "USD", "GBP"])
        st.number_input("Results per page", value=20, min_value=5, max_value=100)
        
        st.markdown("### 🎨 Theme")
        theme = st.radio("Theme", ["Light", "Dark", "Auto"])
        
        st.markdown("### 📥 Data Export")
        st.download_button(
            "Download My Bid History (CSV)",
            "date,tender,buyer,budget,outcome\n2024-01-15,Satellite Comms,ESA,2500000,won\n...",
            "my_bids.csv"
        )

# Footer
st.sidebar.markdown("---")
st.sidebar.markdown("### 📊 Status")
st.sidebar.markdown("✅ API Connected")
st.sidebar.markdown("✅ 15,432 tenders indexed")
st.sidebar.markdown("🔄 Last update: 5 min ago")

st.sidebar.markdown("---")
st.sidebar.markdown("**v2.0.0** | © 2024 Spacescraper")
