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

export default function App() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [lastRefresh, setLastRefresh] = useState(null);
  const [schedule, setSchedule] = useState({});
  const [schedulePending, setSchedulePending] = useState(false);
  const [scheduleMsg, setScheduleMsg] = useState(null);
  const [selectedCpId, setSelectedCpId] = useState(null);

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
  const totalCount = chargePoints.length;

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
                  {totalCount}
                </div>
                <div className="summary-label">Total Charge Points</div>
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
                    const now = new Date();
                    const sydHour = (now.getUTCHours() + 10) % 24;
                    const schedCfg = schedule[effectiveCpId] || {};
                    const peakEnd = schedCfg.peak_end_hour ?? 16;
                    if (sydHour < peakEnd) return `${peakEnd - sydHour}h peak left`;
                    const peakStart = schedCfg.peak_start_hour ?? 0;
                    return `${(24 - sydHour) + peakStart}h off-peak`;
                  })()}
                </div>
                <div className="summary-label">Peak Hours Today</div>
              </div>
              <div className="summary-card">
                <div className={`summary-value ${data.uptime_seconds > 60 ? 'text-green' : 'text-warn'}`}>
                  {Math.floor((data.uptime_seconds || 0) / 60)}m
                </div>
                <div className="summary-label">Uptime</div>
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
                <h3>⏱ Schedule Control</h3>
                <span className="text-secondary" style={{fontSize: 12}}>
                  STOP: block all | AUTO: peak/off-peak schedule | CHARGE NOW: full power
                </span>
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
                      const peakW = schedCfg.peak_watts || 4800;
                      const offW = schedCfg.off_peak_watts || 1440;
                      const peakStart = schedCfg.peak_start_hour ?? 0;
                      const peakEnd = schedCfg.peak_end_hour ?? 16;
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
                            <div className="info-item" style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                              <button className={`btn ${mode === 'stop' ? 'btn-danger' : 'btn-secondary'}`}
                                disabled={schedulePending || mode === 'stop'}
                                onClick={() => setScheduleMode(effectiveCpId, 'stop')}>🛑 STOP</button>
                              <button className={`btn ${mode === 'auto' ? 'btn-primary' : 'btn-secondary'}`}
                                disabled={schedulePending || mode === 'auto'}
                                onClick={() => setScheduleMode(effectiveCpId, 'auto')}>⏱ AUTO</button>
                              <button className={`btn ${mode === 'charge_now' ? 'btn-charge' : 'btn-secondary'}`}
                                disabled={schedulePending || mode === 'charge_now'}
                                onClick={() => setScheduleMode(effectiveCpId, 'charge_now')}>⚡ CHARGE NOW</button>
                            </div>
                          </div>
                          <div className="hint" style={{ fontSize: 12, color: '#95a5a6' }}>
                            <strong>AUTO:</strong> Peak {peakStart}:00–{peakEnd}:00 ({peakW}W) | Off-peak ({offW}W) Sydney time
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

          </>
        )}
      </main>
    </div>
  );
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
