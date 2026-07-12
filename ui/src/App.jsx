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

  const fetchData = async () => {
    try {
      const [debugRes, schedRes] = await Promise.all([
        fetch(`${BASE}debug`),
        fetch(`${BASE}schedule`),
      ]);
      if (!debugRes.ok) throw new Error(`HTTP ${debugRes.status}`);
      const json = await debugRes.json();
      setData(json);
      setLastRefresh(new Date());
      setError(null);
      if (schedRes.ok) setSchedule((await schedRes.json()).schedule_state || {});
    } catch (e) {
      setError(e.message);
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
  const connectedCount = chargePoints.filter(cp => cp.connected).length;
  const totalCount = chargePoints.length;
  const connectedCps = chargePoints.filter(cp => cp.connected);

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
        setScheduleMsg({ type: 'success', text: `${cpId}: ${mode === 'scheduled' ? 'Scheduled (20A day / 6A night)' : 'Always On'}` });
        const schedRes = await fetch(`${BASE}schedule`);
        if (schedRes.ok) setSchedule((await schedRes.json()).schedule_state || {});
      } else {
        setScheduleMsg({ type: 'error', text: result.error || 'Failed' });
      }
    } catch (e) {
      setScheduleMsg({ type: 'error', text: e.message });
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
            <h3>Connection Error</h3>
            <p>{error}</p>
            <p className="hint">Make sure the OCPP bridge is running.</p>
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
                <div className={`summary-value ${data.uptime_seconds > 60 ? 'text-green' : 'text-warn'}`}>
                  {Math.floor((data.uptime_seconds || 0) / 60)}m
                </div>
                <div className="summary-label">Uptime</div>
              </div>
            </div>

            {/* Charge Points Table */}
            <div className="card">
              <div className="card-header">
                <h3>Charge Points</h3>
                <span className="text-secondary" style={{fontSize: 12}}>
                  {data.timestamp ? new Date(data.timestamp).toLocaleTimeString() : ''}
                </span>
              </div>
              <div className="card-body">
                {chargePoints.length === 0 ? (
                  <div className="empty-state">
                    <p>No charge points connected yet.</p>
                    <p className="hint">
                      Configure your EV charger to connect to this bridge at:<br/>
                      <code>ws://{'{host}'}:9000/{'{charge_point_id}'}</code>
                    </p>
                  </div>
                ) : (
                  <div className="table-wrap">
                    <table className="data-table">
                      <thead>
                        <tr>
                          <th>Charge Point ID</th>
                          <th>Status</th>
                          <th>Connector</th>
                          <th>Connected</th>
                          <th>Last Event</th>
                        </tr>
                      </thead>
                      <tbody>
                        {chargePoints.map((cp) => (
                          <tr key={cp.id}>
                            <td className="mono-cell">{cp.id}</td>
                            <td>
                              <span className="status-dot" style={{
                                backgroundColor: STATUS_COLORS[cp.status] || '#545B64',
                                display: 'inline-block', width: 10, height: 10,
                                borderRadius: '50%', marginRight: 6
                              }} />
                              {cp.status || 'unknown'}
                            </td>
                            <td>{cp.connector_id != null ? cp.connector_id : '—'}</td>
                            <td>
                              <span className={`badge ${cp.connected ? 'badge-on' : 'badge-off'}`}>
                                {cp.connected ? 'YES' : 'NO'}
                              </span>
                            </td>
                            <td className="date-cell">
                              {cp.last_event ? new Date(cp.last_event).toLocaleTimeString() : '—'}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>

            {/* Recent Events */}
            <div className="card">
              <div className="card-header">
                <h3>Recent Events</h3>
                <span className="text-secondary" style={{fontSize: 12}}>
                  Last {data.recent_events?.length || 0} events
                </span>
              </div>
              <div className="card-body">
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
                        {data.recent_events.map((ev, i) => (
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

            {/* Schedule Control */}
            <div className="card">
              <div className="card-header">
                <h3>⏱ Schedule Control</h3>
                <span className="text-secondary" style={{fontSize: 12}}>
                  Set charging schedule per charge point
                </span>
              </div>
              <div className="card-body">
                {connectedCps.length === 0 ? (
                  <div className="empty-state">
                    <p>No charge points connected. Connect a charger to control its schedule.</p>
                  </div>
                ) : (
                  <>
                    {scheduleMsg && (
                      <div className={`alert ${scheduleMsg.type === 'success' ? 'alert-success' : 'alert-error'}`}
                           style={{ marginBottom: 16, padding: '8px 12px', borderRadius: 4 }}>
                        {scheduleMsg.text}
                      </div>
                    )}
                    {connectedCps.map(cp => {
                      const mode = schedule[cp.id]?.mode || 'always_on';
                      return (
                        <div key={cp.id} className="info-grid" style={{ marginBottom: 16, paddingBottom: 12, borderBottom: '1px solid #3a4552' }}>
                          <div className="info-item">
                            <span className="info-label">Charge Point</span>
                            <span className="info-value mono-cell">{cp.id}</span>
                          </div>
                          <div className="info-item">
                            <span className="info-label">Current Mode</span>
                            <span className="info-value">
                              <span className={`badge ${mode === 'scheduled' ? 'badge-warn' : 'badge-on'}`}>
                                {mode === 'scheduled' ? '⏱ Scheduled' : '🔌 Always On'}
                              </span>
                            </span>
                          </div>
                          <div className="info-item" style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                            <button
                              className="btn btn-primary"
                              disabled={schedulePending || mode === 'scheduled'}
                              onClick={() => setScheduleMode(cp.id, 'scheduled')}
                              style={{ padding: '6px 14px' }}>
                              Enable Schedule
                            </button>
                            <button
                              className="btn btn-secondary"
                              disabled={schedulePending || mode === 'always_on'}
                              onClick={() => setScheduleMode(cp.id, 'always_on')}
                              style={{ padding: '6px 14px' }}>
                              Always On
                            </button>
                          </div>
                        </div>
                      );
                    })}
                    <div className="hint" style={{ fontSize: 12, color: '#95a5a6' }}>
                      <strong>Scheduled:</strong> 20A (full speed) from 12:00 AM – 4:00 PM | 6A (minimum) from 4:00 PM – 12:00 AM
                    </div>
                  </>
                )}
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
