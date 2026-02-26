# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Procurement Intelligence Dashboard)

import uvicorn
import os
import yaml
import uuid
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Infrastructure & Domain
from src.infrastructure.monitoring.observability import metrics_tracker
from src.domain.models import ScrapeJob
from src.infrastructure.queues.redis_worker import RedisQueueWorker
from src.infrastructure.storage.sqlite_tracker import intel_tracker

# --- Configuration & Paths ---
SOURCES_YAML = Path(__file__).resolve().parent / "sources.yaml"
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
EXPORTS_DIR = Path(__file__).resolve().parent / "exports"

# --- Global Component Initialization ---
queue_worker = RedisQueueWorker(redis_url=REDIS_URL)
scheduler = AsyncIOScheduler()

# Volatile system logs for live UI feedback
system_logs = [{"id": 0, "time": datetime.now().strftime("%H:%M:%S"), "level": "INFO", "msg": "Spacescraper Intel Center initialized."}]

async def internal_dispatch_jobs():
    """YAML-based recurring job dispatcher."""
    if not SOURCES_YAML.is_file(): return
    try:
        with open(SOURCES_YAML, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        for source in config.get("sources", []):
            if not source.get("enabled", True): continue
            target_site = source.get("target_site")
            for url in source.get("start_urls", []):
                job_id = f"intel_auto_{uuid.uuid4().hex[:6]}"
                job = ScrapeJob(job_id=job_id, url=url, target_site=target_site)
                await queue_worker.push_job("jobs_queue", job)
                system_logs.insert(0, {"id": uuid.uuid4().hex, "time": datetime.now().strftime("%H:%M:%S"), "level": "INFO", "msg": f"Auto-dispatch: {source.get('name')}"})
    except Exception as e:
        logger.error(f"Spacescraper Cron fault: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await intel_tracker.initialize()
    await queue_worker.connect()
    scheduler.start()
    if not scheduler.get_job('cron_dispatcher'):
        scheduler.add_job(internal_dispatch_jobs, 'interval', minutes=60, id='cron_dispatcher')
    yield
    scheduler.shutdown()
    await queue_worker.close()

app = FastAPI(title="Spacescraper IntelOps", lifespan=lifespan)

# --- Integrated UI (Ant Design v5 SPA) ---
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><title>Spacescraper IntelOps | Command Center</title>
    <script src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
    <script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
    <script src="https://unpkg.com/antd@5.14.1/dist/antd.min.js"></script>
    <script src="https://unpkg.com/@ant-design/icons/dist/index.umd.js"></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body { background: #020617; color: #f8fafc; font-family: 'Inter', sans-serif; }
        .ant-layout { background: transparent !important; }
        .ant-card { background: #0f172a !important; border: 1px solid #1e293b !important; border-radius: 12px; }
        .ant-table { background: transparent !important; }
        .ant-table-thead > tr > th { background: #1e293b !important; color: #94a3b8 !important; }
        .ant-table-tbody > tr > td { border-bottom: 1px solid #1e293b !important; }
        .gradient-header { background: linear-gradient(to right, #0f172a, #1ecc9522); }
        .log-line { font-family: 'Fira Code', monospace; font-size: 11px; }
    </style>
</head>
<body>
    <div id="root"></div>
    <script>
        const { useState, useEffect } = React;
        const { Layout, Card, Col, Row, Statistic, Table, Button, Space, Tag, message, Typography, Switch, List, Tabs } = antd;
        const { RocketOutlined, GlobalOutlined, BugOutlined, HistoryOutlined, DownloadOutlined, PlayCircleOutlined } = icons;

        const App = () => {
            const [metrics, setMetrics] = useState({ jobs_total: 0, success_rate: 0 });
            const [sources, setSources] = useState([]);
            const [runs, setRuns] = useState([]);
            const [logs, setLogs] = useState([]);

            const refresh = async () => {
                const [m, s, r, l] = await Promise.all([
                    fetch('/api/metrics').then(res => res.json()),
                    fetch('/api/sources').then(res => res.json()),
                    fetch('/api/history').then(res => res.json()),
                    fetch('/api/logs').then(res => res.json())
                ]);
                setMetrics(m);
                setSources(s.sources || []);
                setRuns(r || []);
                setLogs(l || []);
            };

            useEffect(() => { refresh(); const t = setInterval(refresh, 5000); return () => clearInterval(t); }, []);

            const triggerManual = async (site, url) => {
                await fetch('/api/trigger', { method: 'POST', body: JSON.stringify({ target_site: site, url }), headers: {'Content-Type': 'application/json'} });
                message.success(`Job dispatched to cluster: ${site}`);
            };

            return (
                <Layout className="min-h-screen">
                    <Layout.Header className="gradient-header flex items-center px-8 border-b border-slate-800">
                        <Space>
                            <RocketOutlined className="text-emerald-400 text-2xl" />
                            <Typography.Title level={4} style={{ color: '#fff', margin: 0 }}>Spacescraper <span className="text-emerald-500">IntelOps</span></Typography.Title>
                        </Space>
                    </Layout.Header>
                    
                    <Layout.Content className="p-8">
                        <Row gutter={[24, 24]}>
                            <Col span={6}><Card><Statistic title="Cluster Velocity" value={metrics.jobs_total} suffix="tasks" /></Card></Col>
                            <Col span={6}><Card><Statistic title="Success Index" value={metrics.success_rate} precision={2} suffix="%" valueStyle={{color: '#10b981'}} /></Card></Col>
                            <Col span={12}>
                                <Card title={<Space><GlobalOutlined /> Intelligence Registries</Space>}>
                                    <Table dataSource={sources} size="small" pagination={false} columns={[
                                        { title: 'Intel Node', dataIndex: 'name', key: 'n' },
                                        { title: 'State', dataIndex: 'enabled', render: e => <Tag color={e?'success':'default'}>{e?'READY':'STANDBY'}</Tag> },
                                        { title: 'Launch', render: (_, r) => <Button icon={<PlayCircleOutlined />} type="text" className="text-emerald-400" onClick={() => triggerManual(r.target_site, r.start_urls[0])} /> }
                                    ]} />
                                </Card>
                            </Col>

                            <Col span={16}>
                                <Card title={<Space><HistoryOutlined /> Intelligence Ingestion History</Space>}>
                                    <Table dataSource={runs} size="small" columns={[
                                        { title: 'Run ID', dataIndex: 'run_id', render: t => <span className="font-mono text-xs">{t}</span> },
                                        { title: 'Source', dataIndex: 'source' },
                                        { title: 'N', dataIndex: 'new_count', render: c => <span className="text-emerald-400">+{c}</span> },
                                        { title: 'U', dataIndex: 'updated_count', render: c => <span className="text-blue-400">Δ{c}</span> },
                                        { title: 'Time', dataIndex: 'timestamp', render: t => dayjs(t).fromNow() }
                                    ]} />
                                </Card>
                            </Col>

                            <Col span={8}>
                                <Card title={<Space><BugOutlined /> System Forensics</Space>}>
                                    <div className="h-96 overflow-y-auto space-y-1">
                                        {logs.map(l => (
                                            <div key={l.id} className="log-line p-1 hover:bg-slate-800/50 rounded flex gap-2">
                                                <span className="text-slate-500">[{l.time}]</span>
                                                <span className={l.level === 'SUCCESS' ? 'text-emerald-400' : 'text-blue-400'}>{l.msg}</span>
                                            </div>
                                        ))}
                                    </div>
                                </Card>
                            </Col>
                        </Row>
                    </Layout.Content>
                </Layout>
            );
        };
        const root = ReactDOM.createRoot(document.getElementById('root'));
        root.render(<App />);
    </script>
    <script src="https://unpkg.com/dayjs/dayjs.min.js"></script>
    <script src="https://unpkg.com/dayjs/plugin/relativeTime.js"></script>
    <script>dayjs.extend(dayjs_plugin_relativeTime)</script>
</body>
</html>
"""

# --- RESTful API ---
@app.get("/", response_class=HTMLResponse)
async def home(): return DASHBOARD_HTML

@app.get("/api/metrics")
async def get_metrics():
    return {"jobs_total": metrics_tracker.metrics.get("jobs_total", 0), "success_rate": metrics_tracker.get_success_rate()}

@app.get("/api/sources")
async def get_sources():
    if not SOURCES_YAML.is_file(): return {"sources": []}
    with open(SOURCES_YAML, "r", encoding="utf-8") as f: return yaml.safe_load(f)

@app.get("/api/history")
async def get_history():
    import aiosqlite
    async with aiosqlite.connect(intel_tracker.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM runs ORDER BY timestamp DESC LIMIT 20") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

@app.get("/api/logs")
async def get_logs(): return system_logs[:40]

@app.post("/api/trigger")
async def trigger(r: Request):
    data = await r.json()
    job = ScrapeJob(job_id=f"man_ss_{uuid.uuid4().hex[:6]}", url=data.get('url'), target_site=data.get('target_site'))
    await queue_worker.push_job("jobs_queue", job)
    system_logs.insert(0, {"id": uuid.uuid4().hex, "time": datetime.now().strftime("%H:%M:%S"), "level": "SUCCESS", "msg": f"Manual authorization: {data.get('target_site')}"})
    return {"status": "success"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
