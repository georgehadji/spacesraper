# Spacescraper Web UI

Interactive web dashboard for the Spacescraper tender intelligence platform.

## 🚀 Quick Start

### Install Dependencies
```bash
pip install -r requirements-dashboard.txt
```

### Run Dashboard
```bash
streamlit run dashboard.py
```

Open browser: **http://localhost:8501**

## 📱 Screenshots

### Dashboard
- Real-time metrics
- Top tender matches
- Win probability visualization

### Find Tenders
- Advanced filters
- Quality scoring
- Match explanations

### My Profile
- Capability configuration
- Performance analytics
- Win rate trends

## 🎨 Features

### Pages
1. **🏠 Dashboard** - Overview and top matches
2. **🔍 Find Tenders** - Search with filters
3. **📊 My Profile** - Capabilities & performance
4. **📈 Analytics** - Market insights
5. **⚙️ Settings** - Configuration

### Interactive Elements
- Real-time filtering
- Sortable tables
- Expandable tender cards
- Charts & visualizations

## 🔧 Customization

Edit `dashboard.py` to:
- Change theme colors
- Add new charts
- Modify filters
- Update API endpoints

## 📡 API Integration

The dashboard connects to your Spacescraper API:

```python
# Default: localhost:8000
API_BASE_URL = "http://localhost:8000"

# For production
API_BASE_URL = "https://api.spacescraper.com"
```

Make sure your API is running before starting the dashboard.

## 🐳 Docker

```bash
# Build
docker build -f Dockerfile.dashboard -t spacescraper-dashboard .

# Run
docker run -p 8501:8501 spacescraper-dashboard
```

## 📊 Demo Mode

Without API connection, dashboard shows sample data for demonstration.

To use real data:
1. Start API: `python main.py`
2. Set API key in sidebar
3. Refresh dashboard
