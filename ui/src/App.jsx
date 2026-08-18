import React, { useState, useEffect, useMemo, useRef } from 'react';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Cell,
} from 'recharts';

const BASE = import.meta.env.BASE_URL;

const GRAPH_VIEWS = ['distribution', 'hourly'];

const buildHourlySeries = (samples) => {
  const sampleMap = new Map();
  (samples || []).forEach((entry) => {
    if (!entry?.hour) return;
    const d = new Date(entry.hour);
    d.setMinutes(0, 0, 0);
    sampleMap.set(d.toISOString(), Number(entry.kw || 0));
  });

  const points = [];
  const now = new Date();
  now.setMinutes(0, 0, 0);

  for (let i = 95; i >= 0; i -= 1) {
    const slot = new Date(now);
    slot.setHours(slot.getHours() - i);
    const key = slot.toISOString();
    const kw = sampleMap.get(key) || 0;
    const hour = String(slot.getHours()).padStart(2, '0');
    const label = slot.getHours() === 0
      ? String(slot.getDate()).padStart(2, '0') + 'd'
      : hour;
    points.push({
      name: hour,
      label,
      kw: Number(kw.toFixed(2)),
      bucket: slot.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit' }),
    });
  }

  return points;
};

const sumChargePointWatts = (chargePoints) => chargePoints.reduce((sum, cp) => {
  const mv = cp?.meter_values || {};
  for (const key of Object.keys(mv)) {
    if (key === '0') continue;
    if (typeof mv[key]?.power === 'number') sum += mv[key].power;
  }
  return sum;
}, 0);

const STATUS_COLORS = {
  Available: '#0A7D4C', Preparing: '#FF9900', Charging: '#0073BB',
  SuspendedEVSE: '#545B64', SuspendedEV: '#545B64', Finishing: '#FF9900',
  Faulted: '#D13212', Unavailable: '#D13212', Reserved: '#FF9900',
};

const DEFAULT_PERIODS = [
  { start_hour: 0, limit_watts: 4800 },
  { start_hour: 16, limit_watts: 1440 },
];

const toggleStyle = (active, color) => ({
  background: active ? color : 'transparent', color: active ? '#fff' : '#95a5a6',
  border: 'none', padding: '8px 16px', cursor: 'pointer', fontSize: 13,
  fontWeight: 600, transition: 'all 0.15s', flex: 1,
});

const getEventBadge = (type) => {
  const m = { connected: 'badge-on', disconnected: 'badge-off',
    boot_notification: 'badge-info', heartbeat: 'badge-info',
    status_notification: 'badge-warn', start_transaction: 'badge-on',
    stop_transaction: 'badge-off', meter_values: 'badge-info',
    authorize: 'badge-info', data_transfer: 'badge-info' };
  return m[type] || 'badge-info';
};

export default function App() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [lastRefresh, setLastRefresh] = useState(null);
  const [schedule, setSchedule] = useState({});
  const [schedulePending, setSchedulePending] = useState(false);
  const [scheduleMsg, setScheduleMsg] = useState(null);
  const [selectedCpId, setSelectedCpId] = useState(null);
  const [showConfig, setShowConfig] = useState(false);
  const [editPeriods, setEditPeriods] = useState([...DEFAULT_PERIODS]);
  const [editTimezone, setEditTimezone] = useState('Australia/Sydney');
  const [editSolarSmart, setEditSolarSmart] = useState(false);
  const [editOffPeakStart, setEditOffPeakStart] = useState(0);
  const [editOffPeakEnd, setEditOffPeakEnd] = useState(6);
  const [timezones, setTimezones] = useState([]);
  const [graphView, setGraphView] = useState('distribution');
  const [isNarrow, setIsNarrow] = useState(() => window.innerWidth <= 640);
  const touchStartX = useRef(null);

  const fetchData = async () => {
    try {
      const [debugRes, schedRes] = await Promise.all([
        fetch(BASE + 'debug'), fetch(BASE + 'schedule'),
      ]);
      if (!debugRes.ok) throw new Error('Server unavailable');
      const json = await debugRes.json();
      setData(json); setLastRefresh(new Date()); setError(null);
      if (schedRes.ok) {
        try {
          const schedJson = await schedRes.json();
          setSchedule(schedJson.schedule_configs || schedJson.schedule_state || {});
          if (schedJson.timezones) setTimezones(schedJson.timezones);
        } catch {}
      }
    } catch (e) {
      setError(e.message === 'Failed to fetch' ? 'Connection lost - retrying...' : 'Server unavailable - retrying...');
    } finally { setLoading(false); }
  };

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 5000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    const onResize = () => setIsNarrow(window.innerWidth <= 640);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  const chargePoints = data?.charge_points || [];
  const connectedCps = chargePoints.filter(cp => cp.connected);
  const effectiveCpId = selectedCpId || (connectedCps[0]?.id) || (chargePoints[0]?.id) || null;
  const selectedCp = chargePoints.find(cp => cp.id === effectiveCpId) || null;

  const totalPower = sumChargePointWatts(chargePoints);
  const hourlyChartData = useMemo(
    () => buildHourlySeries(data?.hourly_history?.samples || []),
    [data?.hourly_history?.samples],
  );

  const shiftGraph = (step) => {
    const idx = GRAPH_VIEWS.indexOf(graphView);
    const nextIdx = (idx + step + GRAPH_VIEWS.length) % GRAPH_VIEWS.length;
    setGraphView(GRAPH_VIEWS[nextIdx]);
  };

  const setScheduleMode = async (cpId, mode) => {
    setSchedulePending(true); setScheduleMsg(null);
    try {
      const res = await fetch(BASE + 'schedule', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cp_id: cpId, mode }),
      });
      const result = await res.json();
      if (res.ok) {
        const msgs = { auto: 'AUTO (peak/off-peak schedule)', stop: 'STOP - all charging blocked', charge_now: 'CHARGE NOW - full power' };
        setScheduleMsg({ type: 'success', text: cpId + ': ' + (msgs[mode] || mode) });
        try {
          const schedRes = await fetch(BASE + 'schedule');
          if (schedRes.ok) { const schedJson = await schedRes.json(); setSchedule(schedJson.schedule_configs || schedJson.schedule_state || {}); }
        } catch {}
      } else { setScheduleMsg({ type: 'error', text: result.error || 'Request failed' }); }
    } catch (e) { setScheduleMsg({ type: 'error', text: 'Connection issue - try again' }); }
    finally { setSchedulePending(false); }
  };

  const solar = data?.solar_metrics || {};

  const getNow = () => {
    const cfg = schedule[effectiveCpId] || {};
    const tz = cfg.timezone || 'Australia/Sydney';
    try { return parseInt(new Intl.DateTimeFormat('en', { hour: 'numeric', hour12: false, timeZone: tz }).format(new Date())); }
    catch { return new Date().getHours(); }
  };

  return (
    <div className="app">
      <header className="aws-navbar">
        <div className="navbar-brand">
          <span className="brand-icon">🔌</span><span>IoT Core</span>
          <span className="brand-divider">|</span><span className="brand-service">OCPP MQTT Bridge</span>
        </div>
        {lastRefresh && <span className="navbar-refresh">Updated: {lastRefresh.toLocaleTimeString()}</span>}
      </header>

      <main className="main-content">
        {loading && !data && <div className="loader">Loading bridge data...</div>}
        {error && <div className="error-card"><h3>Connection Issue</h3><p>{error}</p><p className="hint">The bridge may be restarting - data will refresh automatically.</p></div>}

        {data && (<>
          <div className="summary-cards">
            <div className="summary-card">
              <div className={'summary-value' + (totalPower > 0 ? ' text-green' : '')}>{totalPower > 0 ? Math.round(totalPower) + 'W' : '0W'}</div>
              <div className="summary-label">Charging Power</div>
            </div>
            <div className="summary-card">
              <div className={'summary-value' + (solar.grid_export > 2000 ? ' text-green' : solar.grid_import > 0 ? ' text-red' : '')}>{solar.grid_export || 0}W</div>
              <div className="summary-label">Grid Export</div>
            </div>
            <div className="summary-card">
              <div className={'summary-value' + ((solar.battery_soc || 0) > 50 ? ' text-green' : ' text-warn')}>{solar.battery_soc || 0}%</div>
              <div className="summary-label">Battery SOC</div>
            </div>
            <div className="summary-card">
              <div className="summary-value">
                {(() => { const schedCfg = schedule[effectiveCpId] || {}; const mode = schedCfg.mode || 'charge_now'; if (mode === 'stop') return '🛑 STOP'; if (mode === 'charge_now') return '⚡ FULL'; return '⏱ ' + (schedCfg.periods || DEFAULT_PERIODS).length + 'w'; })()}
              </div>
              <div className="summary-label">{effectiveCpId ? 'Schedule' : 'Schedule (select CP)'}</div>
            </div>
          </div>

          {/* Schedule Control — above Power Distribution, with CP dropdown in header */}
          <div className="card">
            <div className="card-header">
              <div style={{display: 'flex', alignItems: 'center', gap: 16, flex: 1, minWidth: 0}}>
                <h3>⏱ Schedule Control</h3>
                {chargePoints.length > 0 && (
                  <select value={effectiveCpId || ''} onChange={(e) => setSelectedCpId(e.target.value || null)}
                    style={{ padding: '4px 8px', background: '#0d141e', border: '1px solid #3a4552', color: '#d5dbdb', borderRadius: 3, fontSize: 13, maxWidth: 200 }}>
                    {chargePoints.map(cp => (<option key={cp.id} value={cp.id}>{cp.id}{cp.connected ? '' : ' (offline)'}</option>))}
                  </select>
                )}
              </div>
              <button className="btn btn-secondary icon-only"
                style={{padding: '6px 10px', fontSize: 16, lineHeight: 1, flexShrink: 0}}
                disabled={schedulePending || !selectedCp?.connected} title="Configure schedule periods"
                onClick={() => {
                  const cfg = schedule[effectiveCpId] || {};
                  setEditPeriods(cfg.periods ? [...cfg.periods] : [...DEFAULT_PERIODS]);
                  setEditTimezone(cfg.timezone || 'Australia/Sydney');
                  setEditSolarSmart(cfg.solar_smart || false);
                  setEditOffPeakStart(cfg.off_peak_start_hour ?? 0);
                  setEditOffPeakEnd(cfg.off_peak_end_hour ?? 6);
                  setShowConfig(true);
                }}>⚙</button>
            </div>
            <div className="card-body">
              {!effectiveCpId ? <div className="empty-state"><p>No charge point selected.</p></div>
               : !selectedCp?.connected ? <div className="empty-state"><p>{effectiveCpId} is offline - cannot control schedule.</p></div>
               : (<>
                {scheduleMsg && <div className={'alert ' + (scheduleMsg.type === 'success' ? 'alert-success' : 'alert-error')} style={{ marginBottom: 16 }}>{scheduleMsg.text}</div>}
                <div className="toggle-group" style={{ display: 'inline-flex', borderRadius: 4, width: '100%', overflow: 'hidden', border: '1px solid #3a4552' }}>
                  {(() => { const schedCfg = schedule[effectiveCpId] || {}; const mode = schedCfg.mode || 'charge_now'; return (<>
                    <button className={'toggle-btn' + (mode === 'stop' ? ' toggle-active-danger' : '')} disabled={schedulePending} onClick={() => setScheduleMode(effectiveCpId, 'stop')} style={toggleStyle(mode === 'stop', '#D13212')}>🛑 STOP</button>
                    <button className={'toggle-btn' + (mode === 'auto' ? ' toggle-active-primary' : '')} disabled={schedulePending} onClick={() => setScheduleMode(effectiveCpId, 'auto')} style={toggleStyle(mode === 'auto', '#0073BB')}>⏱ AUTO</button>
                    <button className={'toggle-btn' + (mode === 'charge_now' ? ' toggle-active-charge' : '')} disabled={schedulePending} onClick={() => setScheduleMode(effectiveCpId, 'charge_now')} style={toggleStyle(mode === 'charge_now', '#0A7D4C')}>⚡ CHARGE NOW</button>
                  </>); })()}
                </div>
                <div className="hint" style={{ fontSize: 12, color: '#95a5a6', marginTop: 8 }}>
                  <strong>STOP:</strong> block all | <strong>AUTO:</strong> time-of-day schedule | <strong>CHARGE NOW:</strong> full power
                  {(() => { const cfg = schedule[effectiveCpId] || {}; const periods = cfg.mode === 'auto' ? (cfg.periods || DEFAULT_PERIODS) : null; return periods ? ' - ' + periods.map(p => p.start_hour + ':00→' + p.limit_watts + 'W').join(', ') : ''; })()}
                </div>
              </>)}
            </div>
          </div>

          <div className="card">
            <div className="card-header">
              <h3>Power Graphs</h3>
              <span className="text-secondary" style={{fontSize: 12}}>
                {graphView === 'distribution'
                  ? (solar.last_update ? new Date(solar.last_update).toLocaleTimeString() : 'No data')
                  : 'Last 96 hours'}
              </span>
            </div>
            <div className="card-body">
              <div className="graph-toolbar" role="tablist" aria-label="Graph selector">
                <button
                  className={'graph-tab' + (graphView === 'distribution' ? ' active' : '')}
                  onClick={() => setGraphView('distribution')}
                >
                  Live Split
                </button>
                <button
                  className={'graph-tab' + (graphView === 'hourly' ? ' active' : '')}
                  onClick={() => setGraphView('hourly')}
                >
                  96h Usage
                </button>
              </div>

              <div
                className="graph-swipe-wrap"
                onTouchStart={(e) => {
                  touchStartX.current = e.changedTouches?.[0]?.clientX ?? null;
                }}
                onTouchEnd={(e) => {
                  if (!isNarrow || touchStartX.current == null) return;
                  const endX = e.changedTouches?.[0]?.clientX;
                  if (typeof endX !== 'number') return;
                  const delta = endX - touchStartX.current;
                  if (Math.abs(delta) < 40) return;
                  shiftGraph(delta > 0 ? -1 : 1);
                  touchStartX.current = null;
                }}
              >
                {graphView === 'distribution' ? (
                  <ResponsiveContainer width="100%" height={220}>
                    <BarChart
                      data={[
                        { name: 'PV', value: Math.abs(solar.pv_power || 0) },
                        { name: 'Grid Out', value: Math.abs(solar.grid_export || 0) },
                        { name: 'Grid In', value: Math.abs(solar.grid_import || 0) },
                        { name: 'Charging', value: Math.round(totalPower) },
                      ]}
                      margin={{ top: 5, right: 20, left: 0, bottom: 5 }}
                    >
                      <CartesianGrid strokeDasharray="3 3" stroke="#3a4552" />
                      <XAxis dataKey="name" tick={{ fontSize: 11, fill: '#95a5a6' }} />
                      <YAxis tick={{ fontSize: 12, fill: '#95a5a6' }} />
                      <Tooltip formatter={(v) => v + 'W'} />
                      <Bar dataKey="value" radius={[2, 2, 0, 0]}>
                        <Cell fill="#FF9900" /><Cell fill="#0A7D4C" /><Cell fill="#D13212" /><Cell fill="#0073BB" />
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                ) : (
                  <ResponsiveContainer width="100%" height={220}>
                    <BarChart data={hourlyChartData} margin={{ top: 5, right: 10, left: 0, bottom: 5 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#3a4552" />
                      <XAxis dataKey="label" tick={{ fontSize: 10, fill: '#95a5a6' }} interval={7} />
                      <YAxis tick={{ fontSize: 12, fill: '#95a5a6' }} unit="kW" />
                      <Tooltip
                        formatter={(v) => Number(v).toFixed(2) + ' kW'}
                        labelFormatter={(_, payload) => payload?.[0]?.payload?.bucket || 'Hour'}
                      />
                      <Bar dataKey="kw" fill="#0073BB" radius={[2, 2, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                )}
              </div>
              {isNarrow && <div className="graph-hint">Swipe left or right on the chart to switch graphs.</div>}
            </div>
          </div>

          {selectedCp && (
            <div className="card">
              <div className="card-header">
                <h3>Charger State: {selectedCp.id}</h3>
                <span className={'badge ' + (selectedCp.status === 'Charging' ? 'badge-on' : selectedCp.status === 'Available' ? 'badge-info' : 'badge-warn')}>{selectedCp.status || 'unknown'}</span>
              </div>
              <div className="card-body">
                <div className="table-wrap"><table className="data-table">
                  <thead><tr><th>Connector</th><th>Status</th><th>Power</th><th>Energy</th><th>Last Update</th></tr></thead>
                  <tbody>
                    {Object.entries(selectedCp.physical_status || {}).map(([connId, status]) => {
                      const mv = (selectedCp.meter_values || {})[connId] || {};
                      return (<tr key={connId}><td>Connector {connId}</td><td><span className={'badge ' + (status === 'Charging' ? 'badge-on' : status === 'Available' ? 'badge-info' : 'badge-off')}>{status}</span></td><td>{mv.power != null ? Math.round(mv.power) + 'W' : '—'}</td><td>{mv.energy != null ? (mv.energy / 1000).toFixed(1) + 'kWh' : '—'}</td><td className="date-cell">{mv.timestamp ? new Date(mv.timestamp).toLocaleTimeString() : '—'}</td></tr>);
                    })}
                    {Object.keys(selectedCp.physical_status || {}).length === 0 && <tr><td colSpan={5} style={{textAlign: 'center', color: '#95a5a6'}}>No connectors active</td></tr>}
                  </tbody>
                </table></div>
              </div>
            </div>
          )}

          {chargePoints.length === 0 && (
            <div className="card"><div className="card-header"><h3>Charge Points</h3></div><div className="card-body"><div className="empty-state"><p>No charge points connected yet.</p><p className="hint">Configure your EV charger to connect at:<br/><code>ws://host:9000/{'{charge_point_id}'}</code></p></div></div></div>
          )}

          {/* Charging Rules */}
          <div className="card">
            <div className="card-header">
              <h3>Charging Rules</h3>
              <span className="badge badge-info">{(() => { const cfg = schedule[effectiveCpId] || {}; const mode = cfg.mode || 'charge_now'; return mode === 'stop' ? '🛑 STOP' : mode === 'auto' ? '⏱ AUTO' : '⚡ CHARGE NOW'; })()}</span>
            </div>
            <div className="card-body">
              {!effectiveCpId ? <div className="empty-state"><p>Select a charge point to view rules.</p></div> : (() => {
                const cfg = schedule[effectiveCpId] || {}; const mode = cfg.mode || 'charge_now';
                const periods = cfg.periods || DEFAULT_PERIODS; const sortedPeriods = [...periods].sort((a, b) => a.start_hour - b.start_hour);
                const tz = cfg.timezone || 'Australia/Sydney'; const currentHour = getNow();
                let activeIdx = sortedPeriods.length - 1;
                for (let i = 0; i < sortedPeriods.length; i++) { if (sortedPeriods[i].start_hour <= currentHour) activeIdx = i; }
                return (<>
                  {mode === 'stop' && <div className="decision-reason override-notice"><strong>🛑 STOP:</strong> All charging blocked. New sessions rejected.</div>}
                  {mode === 'charge_now' && <div className="decision-reason"><strong>⚡ CHARGE NOW:</strong> Full power — no schedule restrictions.</div>}
                  {mode === 'auto' && <div className="decision-reason"><strong>⏱ AUTO:</strong> Time-of-day schedule ({tz})<div className="text-secondary" style={{fontSize: 12, marginTop: 4}}>Current hour: {currentHour}:00 — active period limits charging to {sortedPeriods[activeIdx]?.limit_watts || '?'}W</div></div>}
                  <ul className="rules-list" style={{marginTop: 12}}>
                    {sortedPeriods.map((p, i) => { const endHour = i < sortedPeriods.length - 1 ? sortedPeriods[i + 1].start_hour : 24; const isActive = mode === 'auto' && i === activeIdx; return (<li key={i} className={isActive ? 'active' : ''}><strong>{String(p.start_hour).padStart(2, '0')}:00–{String(endHour).padStart(2, '0')}:00:</strong> {p.limit_watts > 0 ? p.limit_watts + 'W' : 'BLOCKED'}{isActive ? ' ← active' : ''}</li>); })}
                  </ul>
                  {cfg.solar_smart && <div className="hint" style={{fontSize: 12, color: '#95a5a6', marginTop: 8}}>☀️ <strong>Solar Smart:</strong> Active — dynamically throttles in peak hours based on grid import/export.{cfg.off_peak_start_hour != null ? ' Off-peak: ' + cfg.off_peak_start_hour + ':00–' + cfg.off_peak_end_hour + ':00.' : ''}</div>}
                </>);
              })()}
            </div>
          </div>

          {/* Recent Events */}
          <div className="card">
            <div className="card-header"><h3>Recent Events</h3><span className="text-secondary" style={{fontSize: 12}}>{effectiveCpId ? 'Filtered: ' + effectiveCpId : 'All charge points'}</span></div>
            <div className="card-body" style={{ maxHeight: 280, overflowY: 'auto' }}>
              {(!data.recent_events || data.recent_events.length === 0) ? <div className="empty-state"><p>No events yet. Waiting for charge point activity...</p></div>
               : (<div className="table-wrap"><table className="data-table"><thead><tr><th>Time</th><th>Charge Point</th><th>Event</th><th>Details</th></tr></thead><tbody>
                {data.recent_events.filter(ev => !effectiveCpId || ev.charge_point_id === effectiveCpId).map((ev, i) => (
                  <tr key={i}><td className="date-cell">{new Date(ev.time).toLocaleTimeString()}</td><td className="mono-cell">{ev.charge_point_id}</td><td><span className={'badge ' + getEventBadge(ev.type)}>{ev.type}</span></td><td className="mono-cell" style={{maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis'}}>{ev.summary || '-'}</td></tr>
                ))}
              </tbody></table></div>)}
            </div>
          </div>

          {/* Bridge Info */}
          <div className="card"><div className="card-header"><h3>Bridge Info</h3></div><div className="card-body"><div className="info-grid">
            <div className="info-item"><span className="info-label">CSMS Endpoint</span><span className="info-value mono-cell">ws://0.0.0.0:9000/{'{charge_point_id}'}</span></div>
            <div className="info-item"><span className="info-label">MQTT Broker</span><span className="info-value mono-cell">{data.mqtt_broker || 'docker-iot_server'}</span></div>
            <div className="info-item"><span className="info-label">Uptime</span><span className="info-value">{Math.floor((data.uptime_seconds || 0) / 60)}m</span></div>
            <div className="info-item"><span className="info-label">Connected CPs</span><span className={'info-value ' + (connectedCps.length > 0 ? 'text-green' : 'text-red')}>{connectedCps.length}</span></div>
          </div></div></div>

          {/* Period Config Modal */}
          {showConfig && (
          <div className="modal-overlay" style={{ position: 'fixed', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: 'rgba(0,0,0,0.6)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 }}
            onClick={(e) => { if (e.target === e.currentTarget) setShowConfig(false); }}>
            <div className="modal-content card" style={{ width: 520, maxHeight: '80vh', overflow: 'auto', boxShadow: '0 8px 32px rgba(0,0,0,0.4)' }}>
              <div className="card-header"><h3>⚙ Configure Schedule - {effectiveCpId}</h3><button className="btn btn-secondary" style={{padding: '4px 10px'}} onClick={() => setShowConfig(false)}>✕</button></div>
              <div className="card-body">
                <div style={{marginBottom: 16}}>
                  <label style={{fontSize: 12, color: '#95a5a6', display: 'block', marginBottom: 4}}>Timezone</label>
                  <select value={editTimezone} onChange={(e) => setEditTimezone(e.target.value)}
                    style={{width: '100%', padding: '8px', background: '#0d141e', border: '1px solid #3a4552', color: '#d5dbdb', borderRadius: 3}}>
                    {(timezones.length > 0 ? timezones : ['Australia/Sydney', 'UTC']).map(tz => (<option key={tz} value={tz}>{tz}</option>))}
                  </select>
                </div>
                <div style={{marginBottom: 16, padding: '12px', background: '#1a2332', borderRadius: 4, border: '1px solid #3a4552'}}>
                  <label style={{display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', marginBottom: 12}}>
                    <input type="checkbox" checked={editSolarSmart} onChange={(e) => setEditSolarSmart(e.target.checked)} style={{width: 18, height: 18, cursor: 'pointer'}} />
                    <span style={{fontWeight: 600, fontSize: 14}}>☀️ Solar Smart</span>
                  </label>
                  <p style={{fontSize: 12, color: '#95a5a6', marginBottom: 10}}>Dynamically throttle charging based on solar/grid balance.</p>
                  {editSolarSmart && (<div style={{display: 'flex', gap: 16}}>
                    <div style={{flex: 1}}><label style={{fontSize: 11, color: '#95a5a6', display: 'block', marginBottom: 3}}>Off-Peak Start</label><input type="number" min={0} max={23} value={editOffPeakStart} onChange={(e) => setEditOffPeakStart(parseInt(e.target.value) || 0)} style={{width: '100%', padding: '6px 8px', background: '#0d141e', border: '1px solid #3a4552', color: '#d5dbdb', borderRadius: 3}} /></div>
                    <div style={{flex: 1}}><label style={{fontSize: 11, color: '#95a5a6', display: 'block', marginBottom: 3}}>Off-Peak End</label><input type="number" min={0} max={23} value={editOffPeakEnd} onChange={(e) => setEditOffPeakEnd(parseInt(e.target.value) || 0)} style={{width: '100%', padding: '6px 8px', background: '#0d141e', border: '1px solid #3a4552', color: '#d5dbdb', borderRadius: 3}} /></div>
                  </div>)}
                </div>
                <p className="hint" style={{marginBottom: 16, fontSize: 13, color: '#95a5a6'}}>Each period sets a power limit starting at a given hour. Schedule repeats <strong>daily</strong>.</p>
                {editPeriods.map((p, i) => (
                  <div key={i} style={{marginBottom: 8, padding: '8px 12px', display: 'flex', alignItems: 'center', gap: 12, backgroundColor: '#1a2332', borderRadius: 4}}>
                    <div style={{flex: 1}}><label style={{fontSize: 11, color: '#95a5a6', display: 'block'}}>Start Hour</label><input type="number" min={0} max={23} value={p.start_hour} onChange={(e) => { const next = [...editPeriods]; next[i] = {...next[i], start_hour: parseInt(e.target.value) || 0}; setEditPeriods(next); }} style={{width: '100%', padding: '6px 8px', background: '#0d141e', border: '1px solid #3a4552', color: '#d5dbdb', borderRadius: 3}} /></div>
                    <div style={{flex: 2}}><label style={{fontSize: 11, color: '#95a5a6', display: 'block'}}>Limit (Watts)</label><input type="number" min={0} max={50000} step={100} value={p.limit_watts} onChange={(e) => { const next = [...editPeriods]; next[i] = {...next[i], limit_watts: parseInt(e.target.value) || 0}; setEditPeriods(next); }} style={{width: '100%', padding: '6px 8px', background: '#0d141e', border: '1px solid #3a4552', color: '#d5dbdb', borderRadius: 3}} /></div>
                    <button className="btn btn-secondary" style={{padding: '4px 8px', fontSize: 12}} disabled={editPeriods.length <= 1} onClick={() => { if (editPeriods.length > 1) setEditPeriods(editPeriods.filter((_, idx) => idx !== i)); }}>✕</button>
                  </div>
                ))}
                <button className="btn btn-secondary" style={{marginBottom: 16, width: '100%'}} onClick={() => setEditPeriods([...editPeriods, { start_hour: 0, limit_watts: 4800 }])}>+ Add Period</button>
                <div style={{display: 'flex', gap: 8}}>
                  <button className="btn btn-secondary" style={{flex: 1}} onClick={() => setShowConfig(false)}>Cancel</button>
                  <button className="btn btn-primary" style={{flex: 1}} disabled={schedulePending} onClick={async () => { setSchedulePending(true); try { const res = await fetch(BASE + 'schedule', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cp_id: effectiveCpId, timezone: editTimezone, periods: editPeriods.sort((a, b) => a.start_hour - b.start_hour), solar_smart: editSolarSmart, off_peak_start_hour: editOffPeakStart, off_peak_end_hour: editOffPeakEnd }) }); if (res.ok) { setScheduleMsg({ type: 'success', text: 'Schedule updated' }); setShowConfig(false); const schedRes = await fetch(BASE + 'schedule'); if (schedRes.ok) { const schedJson = await schedRes.json(); setSchedule(schedJson.schedule_configs || schedJson.schedule_state || {}); } } else { const err = await res.json().catch(() => ({})); setScheduleMsg({ type: 'error', text: err.error || 'Update failed' }); } } catch (e) { setScheduleMsg({ type: 'error', text: 'Connection issue' }); } finally { setSchedulePending(false); } }}>Save</button>
                </div>
              </div>
            </div>
          </div>
          )}
        </>)}
      </main>
    </div>
  );
}
