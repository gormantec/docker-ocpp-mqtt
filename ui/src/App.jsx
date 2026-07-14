import React, { useState, useEffect } from 'react';

const BASE = import.meta.env.BASE_URL;

const STATUS_COLORS = {
  Available: '#0A7D4C',
  Preparing: '#FF9900',
  Charging: '#0073BB',
  SuspendedEVSE: '#545B64',
  SuspendedEV: '#545B64',
  Finishing: '#FF9900',
  Faulted: '#D13212',
  Unavailable: '#D13212',
  Reserved: '#FF9900',
};

const DEFAULT_PERIODS = [
  { start_hour: 0, limit_watts: 4800 },
  { start_hour: 16, limit_watts: 1440 },
];

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

  const fetchData = async () => {
    try {
      const [debugRes, schedRes] = await Promise.all([
        fetch(`${BASE}debug`),
        fetch(`${BASE}schedule`),
      ]);
      if (!debugRes.ok) throw new Error('Server unavailable');
      const json = await debugRes.json();
      setData(json);
      setLastRefresh(new Date());
      setError(null);
      if (schedRes.ok) {
        try {
          const schedJson = await schedRes.json();
          setSchedule(schedJson.schedule_configs || schedJson.schedule_state || {});
          if (schedJson.timezones) setTimezones(schedJson.timezones);
        } catch {}
      }
    } catch (e) {
      setError(e.message === 'Failed to fetch' ? 'Connection lost — retrying…' : 'Server unavailable — retrying…');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 5000);
    return () => clearInterval(interval);
  }, []);

  const chargePoints = data?.charge_points || [];
  const connectedCps = chargePoints.filter(cp => cp.connected);
  const connectedCount = connectedCps.length;

  // Auto-select: first connected CP, else first in list, else null
  const effectiveCpId = selectedCpId || (connectedCps[0]?.id) || (chargePoints[0]?.id) || null;
  const selectedCp = chargePoints.find(cp => cp.id === effectiveCpId) || null;

  const setScheduleMode = async (cpId, mode) => {
    setSchedulePending(true);
    setScheduleMsg(null);
    try {
      const res = await fetch(`${BASE}schedule`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cp_id: cpId, mode }),
      });
      const result = await res.json();
      if (res.ok) {
        setScheduleMsg({ type: 'success', text: `${cpId}: ${mode === 'auto' ? 'AUTO (peak/off-peak schedule)' : mode === 'stop' ? 'STOP — all charging blocked' : 'CHARGE NOW — full power'}` });
        try {
          const schedRes = await fetch(`${BASE}schedule`);
          if (schedRes.ok) {
            const schedJson = await schedRes.json();
            setSchedule(schedJson.schedule_configs || schedJson.schedule_state || {});
          }
        } catch {}
      } else {
        setScheduleMsg({ type: 'error', text: result.error || 'Request failed' });
      }
    } catch (e) {
      setScheduleMsg({ type: 'error', text: 'Connection issue — try again' });
    } finally {
      setSchedulePending(false);
    }
  };

  return (
    <div className="app">
      <header className="aws-navbar">
        <div className="navbar-brand">
          <span className="brand-icon">🔌</span>
          <span>IoT Core</span>
          <span className="brand-divider">|</span>
          <span className="brand-service">OCPP MQTT Bridge</span>
        </div>
        {lastRefresh && (
          <span className="navbar-refresh">Updated: {lastRefresh.toLocaleTimeString()}</span>
        )}
      </header>

      <main className="main-content">
        {loading && !data && <div className="loader">Loading bridge data…</div>}

        {error && (
          <div className="error-card">
            <h3>Connection Issue</h3>
            <p>{error}</p>
            <p className="hint">The bridge may be restarting — data will refresh automatically.</p>
          </div>
        )}

        {data && (
          <>
            {/* Summary Cards */}
            <div className="summary-cards">
              <div className="summary-card">
                <div className={`summary-value ${connectedCount > 0 ? 'text-green' : 'text-red'}`}>
                  {connectedCount}
                </div>
                <div className="summary-label">Connected</div>
              </div>
              <div className="summary-card">
                <div className="summary-value">
                  {chargePoints.filter(cp => cp.status === 'Charging').length}
                </div>
                <div className="summary-label">Charging</div>
              </div>
              <div className="summary-card">
                <div className="summary-value text-green">
                  {(() => {
                    const schedCfg = schedule[effectiveCpId] || {};
                    const periods = schedCfg.periods || [];
                    const mode = schedCfg.mode || 'charge_now';
                    if (mode === 'stop') return 'BLOCKED';
                    if (mode === 'charge_now') return 'FULL';
                    // Show count of periods for auto mode
                    return `${periods.length} windows`;
                  })()}
                </div>
                <div className="summary-label">Schedule</div>
              </div>
              <div className="summary-card">
                <div className={`summary-value ${data.uptime_seconds > 60 ? 'text-green' : 'text-warn'}`}>
                  {Math.floor((data.uptime_seconds || 0) / 60)}m
                </div>
                <div className="summary-label">Uptime</div>
              </div>
            </div>

            {/* Mobile Summary Tile */}
            <div className="summary-mobile">
              <div className="summary-mobile-grid">
                <span className="summary-mobile-label">Connected</span>
                <span className={`summary-mobile-value ${connectedCount > 0 ? 'text-green' : 'text-red'}`}>{connectedCount}</span>
                <span className="summary-mobile-label">Charging</span>
                <span className="summary-mobile-value">{chargePoints.filter(cp => cp.status === 'Charging').length}</span>
                <span className="summary-mobile-label">Schedule</span>
                <span className="summary-mobile-value text-green">{(() => {
                  const schedCfg = schedule[effectiveCpId] || {};
                  const mode = schedCfg.mode || 'charge_now';
                  if (mode === 'stop') return 'BLOCKED';
                  if (mode === 'charge_now') return 'FULL';
                  return `${(schedCfg.periods || []).length} windows`;
                })()}</span>
                <span className="summary-mobile-label">Uptime</span>
                <span className={`summary-mobile-value ${data.uptime_seconds > 60 ? 'text-green' : 'text-warn'}`}>{Math.floor((data.uptime_seconds || 0) / 60)}m</span>
              </div>
            </div>

            {/* Charge Point Selector */}
            {chargePoints.length > 0 && (
              <div className="cp-selector" style={{
                display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 16,
                padding: '8px 0', borderBottom: '1px solid #3a4552'
              }}>
                {chargePoints.map(cp => (
                  <button
                    key={cp.id}
                    className={`btn ${cp.id === effectiveCpId ? 'btn-primary' : 'btn-secondary'}`}
                    style={{ fontSize: 13, padding: '6px 14px' }}
                    onClick={() => setSelectedCpId(cp.id)}
                  >
                    <span className="status-dot" style={{
                      backgroundColor: STATUS_COLORS[cp.status] || '#545B64',
                      display: 'inline-block', width: 8, height: 8,
                      borderRadius: '50%', marginRight: 6
                    }} />
                    {cp.id}
                    {cp.connected ? '' : ' (offline)'}
                  </button>
                ))}
              </div>
            )}

            {/* Selected Charge Point Detail */}
            {selectedCp && (
              <div className="card">
                <div className="card-header">
                  <h3>Charge Point: {selectedCp.id}</h3>
                  <span style={{fontSize: 12}}>
                    {selectedCp.connected ?
                      <span className="badge badge-on">CONNECTED</span> :
                      <span className="badge badge-off">OFFLINE</span>}
                  </span>
                </div>
                <div className="card-body">
                  <div className="info-grid">
                    <div className="info-item">
                      <span className="info-label">Connector 1 (Cable)</span>
                      <span className="info-value">
                        <span className="status-dot" style={{
                          backgroundColor: STATUS_COLORS[(selectedCp.physical_status || {})['1']] || '#545B64',
                          display: 'inline-block', width: 10, height: 10,
                          borderRadius: '50%', marginRight: 6, verticalAlign: 'middle'
                        }} />
                        <strong>{(selectedCp.physical_status || {})['1'] || '—'}</strong>
                      </span>
                    </div>
                    <div className="info-item">
                      <span className="info-label">Connector 0 (Controller)</span>
                      <span className="info-value" style={{color: '#95a5a6'}}>
                        {(selectedCp.connectors || {})['0'] || '—'}
                      </span>
                    </div>
                    <div className="info-item">
                      <span className="info-label">Best Status</span>
                      <span className="info-value">
                        <span className="status-dot" style={{
                          backgroundColor: STATUS_COLORS[selectedCp.status] || '#545B64',
                          display: 'inline-block', width: 10, height: 10,
                          borderRadius: '50%', marginRight: 6, verticalAlign: 'middle'
                        }} />
                        {selectedCp.status || 'unknown'}
                      </span>
                    </div>
                    <div className="info-item">
                      <span className="info-label">Last Event</span>
                      <span className="info-value date-cell">
                        {selectedCp.last_event ? new Date(selectedCp.last_event).toLocaleTimeString() : '—'}
                      </span>
                    </div>
                  </div>
                </div>
              </div>
            )}

            {chargePoints.length === 0 && (
              <div className="card">
                <div className="card-header"><h3>Charge Points</h3></div>
                <div className="card-body">
                  <div className="empty-state">
                    <p>No charge points connected yet.</p>
                    <p className="hint">
                      Configure your EV charger to connect at:<br/>
                      <code>ws://{'host'}:9000/{'{charge_point_id}'}</code>
                    </p>
                  </div>
                </div>
              </div>
            )}

            {/* Schedule Control */}
            <div className="card">
              <div className="card-header">
                <div style={{display: 'flex', alignItems: 'center', gap: 16, flex: 1, minWidth: 0}}>
                  <h3>⏱ Schedule Control</h3>
                  <span className="schedule-hint" style={{fontSize: 12, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis'}}>
                    STOP: block all | AUTO: daily time-of-day schedule | CHARGE NOW: full power
                  </span>
                </div>
                <button className="btn btn-secondary icon-only"
                  style={{padding: '6px 10px', fontSize: 16, lineHeight: 1, flexShrink: 0}}
                  disabled={schedulePending || !selectedCp?.connected}
                  title="Configure schedule periods"
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
                {!effectiveCpId ? (
                  <div className="empty-state"><p>No charge point selected.</p></div>
                ) : !selectedCp?.connected ? (
                  <div className="empty-state"><p>{effectiveCpId} is offline — cannot control schedule.</p></div>
                ) : (
                  <>
                    {scheduleMsg && (
                      <div className={`alert ${scheduleMsg.type === 'success' ? 'alert-success' : 'alert-error'}`}
                           style={{ marginBottom: 16 }}>
                        {scheduleMsg.text}
                      </div>
                    )}
                    {(() => {
                      const schedCfg = schedule[effectiveCpId] || {};
                      const mode = schedCfg.mode || 'charge_now';
                      const periods = schedCfg.periods || DEFAULT_PERIODS;
                      const periodStr = periods.map(p =>
                        `${p.start_hour}:00→${p.limit_watts}W`
                      ).join(', ');
                      return (
                        <>
                          <div className="info-grid" style={{ marginBottom: 12, paddingBottom: 12, borderBottom: '1px solid #3a4552' }}>
                            <div className="info-item">
                              <span className="info-label">Mode</span>
                              <span className="info-value">
                                <span className={`badge ${mode === 'stop' ? 'badge-off' : mode === 'auto' ? 'badge-warn' : 'badge-on'}`}>
                                  {mode === 'stop' ? '🛑 STOP' : mode === 'auto' ? '⏱ AUTO' : '⚡ CHARGE NOW'}
                                </span>
                              </span>
                            </div>
                            <div className="info-item" style={{ display: 'flex', gap: 0, alignItems: 'center' }}>
                              <div className="toggle-group" style={{
                                display: 'inline-flex', borderRadius: 4,
                                overflow: 'hidden', border: '1px solid #3a4552'
                              }}>
                                <button className={`toggle-btn ${mode === 'stop' ? 'toggle-active-danger' : ''}`}
                                  disabled={schedulePending}
                                  onClick={() => setScheduleMode(effectiveCpId, 'stop')}
                                  style={toggleStyle(mode === 'stop', '#D13212')}>
                                  🛑 STOP
                                </button>
                                <button className={`toggle-btn ${mode === 'auto' ? 'toggle-active-primary' : ''}`}
                                  disabled={schedulePending}
                                  onClick={() => setScheduleMode(effectiveCpId, 'auto')}
                                  style={toggleStyle(mode === 'auto', '#0073BB')}>
                                  ⏱ AUTO
                                </button>
                                <button className={`toggle-btn ${mode === 'charge_now' ? 'toggle-active-charge' : ''}`}
                                  disabled={schedulePending}
                                  onClick={() => setScheduleMode(effectiveCpId, 'charge_now')}
                                  style={toggleStyle(mode === 'charge_now', '#0A7D4C')}>
                                  ⚡ CHARGE NOW
                                </button>
                              </div>
                            </div>
                          </div>
                          <div className="hint" style={{ fontSize: 12, color: '#95a5a6' }}>
                            <strong>AUTO:</strong> {periodStr}
                          </div>
                        </>
                      );
                    })()}
                  </>
                )}
              </div>
            </div>

            {/* Recent Events */}
            <div className="card">
              <div className="card-header">
                <h3>Recent Events</h3>
                <span className="text-secondary" style={{fontSize: 12}}>
                  {effectiveCpId ? `Filtered: ${effectiveCpId}` : 'All charge points'}
                </span>
              </div>
              <div className="card-body" style={{ maxHeight: 280, overflowY: 'auto' }}>
                {(!data.recent_events || data.recent_events.length === 0) ? (
                  <div className="empty-state">
                    <p>No events yet. Waiting for charge point activity…</p>
                  </div>
                ) : (
                  <div className="table-wrap">
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th>Time</th>
                          <th>Charge Point</th>
                          <th>Event</th>
                          <th>Details</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.recent_events
                          .filter(ev => !effectiveCpId || ev.charge_point_id === effectiveCpId)
                          .map((ev, i) => (
                          <tr key={i}>
                            <td className="date-cell">{new Date(ev.time).toLocaleTimeString()}</td>
                            <td className="mono-cell">{ev.charge_point_id}</td>
                            <td>
                              <span className={`badge ${getEventBadge(ev.type)}`}>
                                {ev.type}
                              </span>
                            </td>
                            <td className="mono-cell" style={{maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis'}}>
                              {ev.summary || '—'}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>

            {/* Connection Info */}
            <div className="card">
              <div className="card-header">
                <h3>Bridge Info</h3>
              </div>
              <div className="card-body">
                <div className="info-grid">
                  <div className="info-item">
                    <span className="info-label">CSMS Endpoint</span>
                    <span className="info-value mono-cell">ws://0.0.0.0:9000/{'{charge_point_id}'}</span>
                  </div>
                  <div className="info-item">
                    <span className="info-label">MQTT Broker</span>
                    <span className="info-value mono-cell">{data.mqtt_broker || 'docker-iot_server'}</span>
                  </div>
                  <div className="info-item">
                    <span className="info-label">MQTT Thing</span>
                    <span className="info-value mono-cell">{data.mqtt_thing_name || '—'}</span>
                  </div>
                  <div className="info-item">
                    <span className="info-label">Started</span>
                    <span className="info-value">
                      {data.started_at ? new Date(data.started_at).toLocaleString() : '—'}
                    </span>
                  </div>
                </div>
              </div>
            </div>

            {/* Period Configuration Modal */}
            {showConfig && (
            <div className="modal-overlay" style={{
              position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
              backgroundColor: 'rgba(0,0,0,0.6)', display: 'flex',
              alignItems: 'center', justifyContent: 'center', zIndex: 1000
            }} onClick={(e) => { if (e.target === e.currentTarget) setShowConfig(false); }}>
              <div className="modal-content card" style={{
                width: 520, maxHeight: '80vh', overflow: 'auto',
                boxShadow: '0 8px 32px rgba(0,0,0,0.4)'
              }}>
                <div className="card-header">
                  <h3>⚙ Configure Schedule — {effectiveCpId}</h3>
                  <button className="btn btn-secondary" style={{padding: '4px 10px'}}
                    onClick={() => setShowConfig(false)}>✕</button>
                </div>
                <div className="card-body">
                  <div style={{marginBottom: 16}}>
                    <label style={{fontSize: 12, color: '#95a5a6', display: 'block', marginBottom: 4}}>
                      Timezone (with DST)
                    </label>
                    <select value={editTimezone}
                      onChange={(e) => setEditTimezone(e.target.value)}
                      style={{
                        width: '100%', padding: '8px', background: '#0d141e',
                        border: '1px solid #3a4552', color: '#d5dbdb', borderRadius: 3
                      }}>
                      {(timezones.length > 0 ? timezones : ['Australia/Sydney', 'UTC']).map(tz => (
                        <option key={tz} value={tz}>{tz}</option>
                      ))}
                    </select>
                  </div>
                  <div style={{
                    marginBottom: 16, padding: '12px', background: '#1a2332',
                    borderRadius: 4, border: '1px solid #3a4552'
                  }}>
                    <label style={{
                      display: 'flex', alignItems: 'center', gap: 10,
                      cursor: 'pointer', marginBottom: 12
                    }}>
                      <input type="checkbox"
                        checked={editSolarSmart}
                        onChange={(e) => setEditSolarSmart(e.target.checked)}
                        style={{ width: 18, height: 18, cursor: 'pointer' }} />
                      <span style={{ fontWeight: 600, fontSize: 14 }}>
                        ☀️ Solar Smart
                      </span>
                    </label>
                    <p style={{ fontSize: 12, color: '#95a5a6', marginBottom: 10 }}>
                      Dynamically throttle charging based on solar/grid balance.
                      Only active in AUTO mode. Uses power from esy-sunhomes telemetry.
                    </p>
                    {editSolarSmart && (
                      <div style={{ display: 'flex', gap: 16 }}>
                        <div style={{ flex: 1 }}>
                          <label style={{ fontSize: 11, color: '#95a5a6', display: 'block', marginBottom: 3 }}>
                            Off-Peak Start (0-23)
                          </label>
                          <input type="number" min={0} max={23}
                            value={editOffPeakStart}
                            onChange={(e) => setEditOffPeakStart(parseInt(e.target.value) || 0)}
                            style={{ width: '100%', padding: '6px 8px', background: '#0d141e',
                              border: '1px solid #3a4552', color: '#d5dbdb', borderRadius: 3 }} />
                        </div>
                        <div style={{ flex: 1 }}>
                          <label style={{ fontSize: 11, color: '#95a5a6', display: 'block', marginBottom: 3 }}>
                            Off-Peak End (0-23)
                          </label>
                          <input type="number" min={0} max={23}
                            value={editOffPeakEnd}
                            onChange={(e) => setEditOffPeakEnd(parseInt(e.target.value) || 0)}
                            style={{ width: '100%', padding: '6px 8px', background: '#0d141e',
                              border: '1px solid #3a4552', color: '#d5dbdb', borderRadius: 3 }} />
                        </div>
                      </div>
                    )}
                  </div>
                  <p className="hint" style={{marginBottom: 16, fontSize: 13, color: '#95a5a6'}}>
                    Each period sets a power limit starting at a given hour.
                    Schedule repeats <strong>daily</strong> (TxDefaultProfile, Recurring+Daily).
                    Periods are anchored to midnight. The limit applies until the next period begins.
                  </p>
                  {editPeriods.map((p, i) => (
                    <div key={i} className="info-grid" style={{
                      marginBottom: 8, padding: '8px 12px',
                      backgroundColor: '#1a2332', borderRadius: 4,
                      display: 'flex', alignItems: 'center', gap: 12
                    }}>
                      <div style={{flex: 1}}>
                        <label style={{fontSize: 11, color: '#95a5a6', display: 'block'}}>
                          Start Hour (0-23)
                        </label>
                        <input type="number" min={0} max={23}
                          value={p.start_hour}
                          onChange={(e) => {
                            const next = [...editPeriods];
                            next[i] = {...next[i], start_hour: parseInt(e.target.value) || 0};
                            setEditPeriods(next);
                          }}
                          style={{
                            width: '100%', padding: '6px 8px', background: '#0d141e',
                            border: '1px solid #3a4552', color: '#d5dbdb', borderRadius: 3
                          }} />
                      </div>
                      <div style={{flex: 2}}>
                        <label style={{fontSize: 11, color: '#95a5a6', display: 'block'}}>
                          Limit (Watts)
                        </label>
                        <input type="number" min={0} max={50000} step={100}
                          value={p.limit_watts}
                          onChange={(e) => {
                            const next = [...editPeriods];
                            next[i] = {...next[i], limit_watts: parseFloat(e.target.value) || 0};
                            setEditPeriods(next);
                          }}
                          style={{
                            width: '100%', padding: '6px 8px', background: '#0d141e',
                            border: '1px solid #3a4552', color: '#d5dbdb', borderRadius: 3
                          }} />
                      </div>
                      <button className="btn btn-danger" style={{padding: '4px 8px', marginTop: 14}}
                        disabled={editPeriods.length <= 1}
                        onClick={() => setEditPeriods(editPeriods.filter((_, idx) => idx !== i))}>
                        ✕
                      </button>
                    </div>
                  ))}
                  <button className="btn btn-secondary" style={{marginTop: 8}}
                    onClick={() => setEditPeriods([...editPeriods, {start_hour: 0, limit_watts: 1000}])}>
                    + Add Period
                  </button>
                  <div className="hint" style={{fontSize: 11, color: '#95a5a6', marginTop: 8}}>
                    Preview: {editPeriods.sort((a,b) => a.start_hour - b.start_hour).map(p => `${p.start_hour}:00→${p.limit_watts}W`).join(', ')}
                  </div>
                  <div style={{display: 'flex', gap: 8, marginTop: 16, justifyContent: 'flex-end'}}>
                    <button className="btn btn-secondary" onClick={() => setShowConfig(false)}>Cancel</button>
                    <button className="btn btn-primary"
                      disabled={schedulePending}
                      onClick={async () => {
                        setSchedulePending(true);
                        setShowConfig(false);
                        try {
                          const sorted = [...editPeriods].sort((a,b) => a.start_hour - b.start_hour);
                          const res = await fetch(`${BASE}schedule`, {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({
                              cp_id: effectiveCpId, mode: 'auto',
                              periods: sorted, timezone: editTimezone,
                              solar_smart: editSolarSmart,
                              off_peak_start_hour: editOffPeakStart,
                              off_peak_end_hour: editOffPeakEnd,
                            }),
                          });
                          const result = await res.json();
                          if (res.ok) {
                            setScheduleMsg({type: 'success', text: `${effectiveCpId}: schedule updated — ${sorted.map(p => `${p.start_hour}:00→${p.limit_watts}W`).join(', ')}`});
                            const schedRes = await fetch(`${BASE}schedule`);
                            if (schedRes.ok) {
                              const schedJson = await schedRes.json();
                              setSchedule(schedJson.schedule_configs || schedJson.schedule_state || {});
                            }
                          } else {
                            setScheduleMsg({type: 'error', text: result.error || 'Failed to save'});
                          }
                        } catch (e) {
                          setScheduleMsg({type: 'error', text: 'Connection issue'});
                        } finally {
                          setSchedulePending(false);
                        }
                      }}>💾 Save &amp; Apply</button>
                  </div>
                </div>
              </div>
            </div>
          )}
          </>
        )}
      </main>
    </div>
  );
}

function toggleStyle(active, color) {
  return {
    padding: '8px 16px', fontSize: 13, fontWeight: 600,
    border: 'none', cursor: 'pointer',
    backgroundColor: active ? color : '#1a2332',
    color: active ? '#fff' : '#95a5a6',
    transition: 'background 0.15s',
  };
}

function getEventBadge(type) {
  switch (type) {
    case 'boot_notification': return 'badge-on';
    case 'heartbeat': return 'badge-neutral';
    case 'status_notification': return 'badge-warn';
    case 'start_transaction': return 'badge-on';
    case 'stop_transaction': return 'badge-off';
    case 'meter_values': return 'badge-neutral';
    case 'authorize': return 'badge-warn';
    case 'fault': return 'badge-off';
    default: return 'badge-neutral';
  }
}
