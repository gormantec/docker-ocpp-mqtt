import React, { useState, useEffect, useMemo, useRef } from 'react';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Cell, LineChart, Line,
} from 'recharts';

const BASE = import.meta.env.BASE_URL;

const GRAPH_VIEWS = ['distribution', 'hourly', 'daily60'];
const STALE_READING_SECONDS = 90;
const EVENT_FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'connection', label: 'Connection' },
  { id: 'charging', label: 'Charging' },
  { id: 'controls', label: 'Controls' },
];
const EVENT_GROUPS = {
  connection: new Set(['connected', 'disconnected']),
  charging: new Set(['authorize', 'remote_start', 'start_transaction', 'stop_transaction', 'status_notification']),
  controls: new Set(['schedule', 'solar_throttle', 'cmd_received']),
};

const eventMatchesFilter = (event, filter) =>
  filter === 'all' || EVENT_GROUPS[filter]?.has(event.type);

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

const buildDailyUsageSeries = (daily) => {
  return (daily?.days || []).map((entry) => {
    const date = new Date(entry.date + 'T00:00:00');
    const samples = Number(entry.samples || 0);
    return {
      label: String(date.getDate()).padStart(2, '0') + '/' + String(date.getMonth() + 1).padStart(2, '0'),
      day: date.toLocaleDateString([], { month: 'short', day: 'numeric' }),
      usage_kwh: Number(entry.usage_kwh || 0),
      cost: Number(entry.cost || 0),
      samples,
    };
  });
};

const summarizeChargePower = (chargePoints) => {
  const readings = [];

  for (const cp of chargePoints) {
    const meterValues = cp?.meter_values || {};
    const sessions = cp?.recent_charge_sessions || [];
    const connectorIds = new Set([
      ...Object.keys(meterValues).filter((key) => key !== '0'),
      ...sessions
        .filter((session) => !session.ended_at && ['charging', 'suspended', 'finishing'].includes(session.state))
        .map((session) => String(session.connector_id)),
    ]);

    for (const connectorId of connectorIds) {
      const live = meterValues[connectorId];
      const session = sessions.find((item) =>
        String(item.connector_id) === connectorId && !item.ended_at
        && ['charging', 'suspended', 'finishing'].includes(item.state)
      );
      const sample = session?.last_sample;
      const livePowerAvailable = Number.isFinite(live?.power);
      const power = livePowerAvailable ? live.power : sample?.power_w;
      if (!Number.isFinite(power)) continue;

      readings.push({
        power,
        receivedAt: livePowerAvailable
          ? live.received_at || live.timestamp
          : sample.received_at,
        fromHistory: !livePowerAvailable,
      });
    }
  }

  return {
    watts: readings.length ? readings.reduce((sum, reading) => sum + reading.power, 0) : null,
    lastReportedAt: readings.map((reading) => reading.receivedAt).filter(Boolean).sort()[0] || null,
    fromHistory: readings.some((reading) => reading.fromHistory),
  };
};

const formatReadingAge = (timestamp) => {
  const timestampMs = Date.parse(timestamp || '');
  if (!Number.isFinite(timestampMs)) return 'time unknown';
  const elapsedSeconds = Math.max(0, Math.floor((Date.now() - timestampMs) / 1000));
  if (elapsedSeconds < 60) return `${elapsedSeconds}s ago`;
  return `${Math.floor(elapsedSeconds / 60)}m ago`;
};

const tileAge = (timestamp, thresholdSeconds = 180) => {
  const timestampMs = Date.parse(timestamp || '');
  if (!Number.isFinite(timestampMs)) return '';
  const seconds = Math.max(0, Math.floor((Date.now() - timestampMs) / 1000));
  if (seconds < thresholdSeconds) return '';
  return seconds < 60 ? `${seconds}sec ago` : `${Math.floor(seconds / 60)}min ago`;
};

const readingIsStale = (timestamp) => {
  const timestampMs = Date.parse(timestamp || '');
  return !Number.isFinite(timestampMs)
    || (Date.now() - timestampMs) / 1000 > STALE_READING_SECONDS;
};

const formatPower = (watts) => watts > 500
  ? `${(watts / 1000).toFixed(2)}kW`
  : `${Math.round(watts)}W`;

const formatCurrency = (value, digits = 2) => {
  const amount = Number(value);
  if (!Number.isFinite(amount)) return '—';
  return `${amount < 0 ? '-$' : '$'}${Math.abs(amount).toFixed(digits)}`;
};

const STATUS_COLORS = {
  Available: '#0A7D4C', Preparing: '#FF9900', Charging: '#0073BB',
  SuspendedEVSE: '#545B64', SuspendedEV: '#545B64', Finishing: '#FF9900',
  Faulted: '#D13212', Unavailable: '#D13212', Reserved: '#FF9900',
};


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

function InfoTip({ text }) {
  const [open, setOpen] = useState(false);
  return (
    <span style={{position: 'relative', display: 'inline-block', marginLeft: 6}}>
      <button type="button" aria-label="More info" onClick={(e) => { e.preventDefault(); setOpen(o => !o); }} onBlur={() => setOpen(false)}
        style={{width: 18, height: 18, borderRadius: '50%', border: '1px solid #0073BB', background: open ? '#0073BB' : 'transparent', color: open ? '#fff' : '#0073BB', fontSize: 11, fontWeight: 700, lineHeight: '16px', padding: 0, cursor: 'pointer'}}>i</button>
      {open && <span role="tooltip" style={{position: 'absolute', left: -8, top: 24, zIndex: 50, width: 240, padding: '8px 10px', background: '#232F3E', color: '#fff', borderRadius: 6, fontSize: 12, fontWeight: 400, lineHeight: 1.4, boxShadow: '0 2px 8px rgba(0,0,0,.3)'}}>{text}</span>}
    </span>
  );
}

function ChargerPage({ routeCpId }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [lastRefresh, setLastRefresh] = useState(null);
  const [schedule, setSchedule] = useState({});
  const [schedulePending, setSchedulePending] = useState(false);
  const [scheduleMsg, setScheduleMsg] = useState(null);
  const [showConfig, setShowConfig] = useState(false);
  const [editOffPeakStart, setEditOffPeakStart] = useState(0);
  const [editOffPeakEnd, setEditOffPeakEnd] = useState(6);
  const [editMaxAmps, setEditMaxAmps] = useState(16);
  const [editMinBatterySoc, setEditMinBatterySoc] = useState(50);
  const [editRates, setEditRates] = useState({ off_peak_rate: 0.08, peak_rate_summer: 0.46761, peak_rate_other: 0.36960 });
  const [timezones, setTimezones] = useState([]);
  const [graphView, setGraphView] = useState('distribution');
  const [eventFilter, setEventFilter] = useState('all');
  const [showAllEvents, setShowAllEvents] = useState(false);
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
  const effectiveCpId = routeCpId;
  const selectedCp = chargePoints.find(cp => cp.id === effectiveCpId) || null;

  const powerSummary = summarizeChargePower(chargePoints);
  const totalPower = powerSummary.watts || 0;
  const hourlyChartData = useMemo(
    () => buildHourlySeries(data?.hourly_history?.samples || []),
    [data?.hourly_history?.samples],
  );
  const dailyChartData = useMemo(
    () => buildDailyUsageSeries(data?.daily_usage_60d),
    [data?.daily_usage_60d],
  );
  const todayUsage = data?.daily_usage_60d?.days?.slice(-1)[0] || null;
  const filteredEvents = (data?.recent_events || [])
    .filter((event) => !effectiveCpId || event.charge_point_id === effectiveCpId)
    .filter((event) => eventMatchesFilter(event, eventFilter));
  const visibleEvents = showAllEvents ? filteredEvents : filteredEvents.slice(0, 8);
  const dailyTicksNarrow = useMemo(() => {
    if (!isNarrow || dailyChartData.length === 0) return undefined;

    const ticks = [];
    for (let i = 0; i < dailyChartData.length; i += 10) {
      ticks.push(dailyChartData[i].label);
    }

    const endLabel = dailyChartData[dailyChartData.length - 1].label;
    if (ticks[ticks.length - 1] !== endLabel) ticks.push(endLabel);
    return ticks;
  }, [dailyChartData, isNarrow]);

  const shiftGraph = (step) => {
    const idx = GRAPH_VIEWS.indexOf(graphView);
    const nextIdx = (idx + step + GRAPH_VIEWS.length) % GRAPH_VIEWS.length;
    setGraphView(GRAPH_VIEWS[nextIdx]);
  };

  const fetchWithTimeout = async (url, options = {}, timeoutMs = 10000) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await fetch(url, { ...options, signal: controller.signal });
    } finally {
      clearTimeout(timer);
    }
  };

  const setScheduleMode = async (cpId, mode) => {
    setSchedulePending(true); setScheduleMsg(null);
    try {
      const res = await fetchWithTimeout(BASE + 'schedule', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cp_id: cpId, mode }),
      }, 12000);
      const result = await res.json();
      if (res.ok) {
        const msgs = { auto: 'AUTO (peak/off-peak schedule)', stop: 'STOP - all charging blocked', charge_now: 'CHARGE NOW - full power' };
        const warn = (result.warnings && result.warnings.length > 0) ? ' (' + result.warnings[0] + ')' : '';
        setScheduleMsg({ type: 'success', text: cpId + ': ' + (msgs[mode] || mode) + warn });
        try {
          const schedRes = await fetch(BASE + 'schedule');
          if (schedRes.ok) { const schedJson = await schedRes.json(); setSchedule(schedJson.schedule_configs || schedJson.schedule_state || {}); }
        } catch {}
      } else { setScheduleMsg({ type: 'error', text: result.error || 'Request failed' }); }
    } catch (e) {
      setScheduleMsg({
        type: 'error',
        text: e?.name === 'AbortError' ? 'Schedule request timed out - charger did not respond' : 'Connection issue - try again',
      });
    }
    finally { setSchedulePending(false); }
  };

  const saveScheduleConfig = async () => {
    if (!effectiveCpId) {
      setScheduleMsg({ type: 'error', text: 'No charge point selected' });
      return;
    }

    const currentMode = (schedule[effectiveCpId]?.mode) || 'charge_now';
    const payload = {
      cp_id: effectiveCpId,
      mode: currentMode,
      timezone: schedule[effectiveCpId]?.timezone || editTimezone,
      off_peak_start_hour: editOffPeakStart,
      off_peak_end_hour: editOffPeakEnd,
      max_amps: editMaxAmps,
      min_battery_soc: editMinBatterySoc,
      ...editRates,
    };

    setSchedulePending(true);
    try {
      const res = await fetchWithTimeout(BASE + 'schedule', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }, 12000);

      const result = await res.json().catch(() => ({}));
      if (!res.ok) {
        setScheduleMsg({ type: 'error', text: result.error || 'Update failed' });
        return;
      }

      const warning = (result.warnings && result.warnings.length > 0)
        ? ' (' + result.warnings[0] + ')'
        : '';
      setScheduleMsg({ type: 'success', text: 'Schedule updated' + warning });
      setShowConfig(false);

      const schedRes = await fetch(BASE + 'schedule');
      if (schedRes.ok) {
        const schedJson = await schedRes.json();
        setSchedule(schedJson.schedule_configs || schedJson.schedule_state || {});
      }
    } catch (e) {
      setScheduleMsg({
        type: 'error',
        text: e?.name === 'AbortError' ? 'Schedule update timed out - charger did not respond' : 'Connection issue',
      });
    } finally {
      setSchedulePending(false);
    }
  };

  const solar = data?.solar_metrics || {};

  const getNow = () => {
    const cfg = schedule[effectiveCpId] || {};
    const tz = cfg.timezone || 'Australia/Sydney';
    try { return parseInt(new Intl.DateTimeFormat('en', { hour: 'numeric', hour12: false, timeZone: tz }).format(new Date())); }
    catch { return new Date().getHours(); }
  };

  const scheduleConfig = schedule[effectiveCpId] || {};
  const scheduleMode = scheduleConfig.mode || 'charge_now';
  const solarControl = data?.solar_control || {};
  const solarControlState = solarControl.states?.[effectiveCpId];
  const powerIsStale = powerSummary.watts != null
    && (powerSummary.fromHistory || readingIsStale(powerSummary.lastReportedAt));
  const solarTelemetryIsStale = readingIsStale(solar.last_update);

  let powerControlReason = 'No charger power-control target is currently available.';
  if (scheduleMode === 'stop') {
    powerControlReason = 'STOP mode blocks charging and requests a stop for any active session.';
  } else if (scheduleMode === 'auto') {
    const imp = Number(solar.grid_import);
    powerControlReason = solarControlState?.reason
      ? `AUTO chose ${solarControlState.level_a ?? 0}A: ${solarControlState.reason}.`
      : 'AUTO is waiting for its first level decision.';
    if (!solarTelemetryIsStale && Number.isFinite(imp) && imp > 0 && solarControlState?.reason?.startsWith('grid import')) {
      powerControlReason += ` Grid import is ${formatPower(imp)}.`;
    }
  } else if (scheduleMode === 'charge_now') {
    powerControlReason = 'CHARGE NOW removes the schedule limit; the charger and vehicle can still draw less.';
  }

  const observedPower = powerSummary.watts == null
    ? 'No charger power sample is available.'
    : `Measured output: ${formatPower(totalPower)}${powerSummary.lastReportedAt ? `, ${powerIsStale ? 'stale' : 'updated'} ${formatReadingAge(powerSummary.lastReportedAt)}` : ''}.`;

  return (
    <div className="app">
      <header className="aws-navbar">
        <div className="navbar-brand">
          <a href={BASE} style={{color: 'inherit', textDecoration: 'none', display: 'flex', alignItems: 'center', gap: 8}}><span className="brand-icon">🔌</span><span>IoT Core</span>
          <span className="brand-divider">|</span><span className="brand-service">OCPP</span></a>
          <span className="brand-divider">|</span><span className="brand-service">{routeCpId}</span>
        </div>
        {lastRefresh && <span className="navbar-refresh">Updated: {lastRefresh.toLocaleTimeString()}</span>}
      </header>

      <main className="main-content">
        {loading && !data && <div className="loader">Loading bridge data...</div>}
        {error && <div className="error-card"><h3>Connection Issue</h3><p>{error}</p><p className="hint">The bridge may be restarting - data will refresh automatically.</p></div>}

        {data && (<>
          <div className={'connection-status' + (selectedCp?.connected ? ' is-connected' : ' is-disconnected')}>
            <span className="connection-status-dot" />
            <strong>{effectiveCpId || 'No charge point'}</strong>
            <span>{selectedCp?.connected ? 'Connected' : 'Offline'}</span>
            {selectedCp?.status && <span className="connection-status-state">Connector: {selectedCp.status}</span>}
          </div>
          <div className="summary-cards">
            <div className="summary-card">
              <span className="tile-age">{tileAge(powerSummary.lastReportedAt, 30)}</span>
              <div className={'summary-value' + (powerSummary.watts != null && totalPower > 0 && !powerIsStale ? ' text-green' : '')}>{powerSummary.watts != null ? formatPower(totalPower) : '—'}</div>
              <div className="summary-label">Charging Power</div>
            </div>
            <div className="summary-card">
              <span className="tile-age">{tileAge(solar.last_update)}</span>
              <div className={'summary-value' + (solar.grid_import > 0 ? ' text-red' : ' text-green')}>
                {solar.last_update ? formatPower(solar.grid_import || 0) : '—'}
              </div>
              <div className="summary-label">Grid Import</div>
            </div>
            <div className="summary-card">
              <div className="summary-value">
                {formatCurrency(todayUsage?.cost || 0)}
              </div>
              <div className="summary-label">
                Today Charging Cost
                {Number(todayUsage?.usage_kwh || 0) > 0 && (
                  <span className="summary-subtext">
                    {`${Number(todayUsage.usage_kwh).toFixed(2)} kWh · estimate`}
                  </span>
                )}
              </div>
            </div>
            <div className="summary-card">
              <span className="tile-age">{tileAge(solar.last_update)}</span>
              <div className={'summary-value' + (solar.grid_export > 2000 ? ' text-green' : '')}>
                {solar.last_update ? formatPower(solar.grid_export || 0) : '—'}
              </div>
              <div className="summary-label">Grid Export</div>
            </div>
            <div className="summary-card">
              <span className="tile-age">{tileAge(solar.last_update)}</span>
              <div className={'summary-value' + (solar.battery_soc > 50 ? ' text-green' : solar.battery_soc <= 50 ? ' text-warn' : '')}>
                {solar.battery_soc == null ? '—' : `${solar.battery_soc}%`}
              </div>
              <div className="summary-label">Home Battery SOC</div>
            </div>
            <div className="summary-card">
              <div className="summary-value">
                {scheduleMode === 'stop' ? 'STOP' : scheduleMode === 'charge_now' ? 'FULL' : 'AUTO'}
              </div>
              <div className="summary-label">{effectiveCpId ? 'Charge Mode' : 'Select a charger'}</div>
            </div>
          </div>

          {/* Schedule Control — above Power Distribution, with CP dropdown in header */}
          <div className="card">
            <div className="card-header">
              <div style={{display: 'flex', alignItems: 'center', gap: 16, flex: 1, minWidth: 0}}>
                <h3>⏱ Schedule Control</h3>
              </div>
              <button className="btn btn-secondary icon-only"
                style={{padding: '6px 10px', fontSize: 16, lineHeight: 1, flexShrink: 0}}
                disabled={schedulePending || !selectedCp?.connected} title="Configure Auto settings"
                onClick={() => {
                  const cfg = schedule[effectiveCpId] || {};
                  setEditOffPeakStart(cfg.off_peak_start_hour ?? 0);
                  setEditOffPeakEnd(cfg.off_peak_end_hour ?? 6);
                  setEditMaxAmps(cfg.max_amps ?? 16);
                  setEditMinBatterySoc(cfg.min_battery_soc ?? 50);
                  setEditRates({ off_peak_rate: cfg.off_peak_rate ?? 0.08, peak_rate_summer: cfg.peak_rate_summer ?? 0.46761, peak_rate_other: cfg.peak_rate_other ?? 0.36960 });
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
                  <strong>STOP:</strong> block all | <strong>AUTO:</strong> solar/battery, grid only off-peak | <strong>CHARGE NOW:</strong> full power
                </div>
                <div className="power-context">
                  <strong>Why this power?</strong>
                  <span>{powerControlReason}</span>
                  <span>{observedPower}</span>
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
                  : graphView === 'hourly' ? 'Last 96 hours' : 'Last 60 days'}
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
                <button
                  className={'graph-tab' + (graphView === 'daily60' ? ' active' : '')}
                  onClick={() => setGraphView('daily60')}
                >
                  60d Cost
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
                        { name: powerIsStale ? 'Charger stale' : 'Charging', value: Math.round(totalPower) },
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
                ) : graphView === 'hourly' ? (
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
                ) : (
                  <ResponsiveContainer width="100%" height={240}>
                    <LineChart data={dailyChartData} margin={{ top: 5, right: 10, left: 0, bottom: 5 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#3a4552" />
                      <XAxis
                        dataKey="label"
                        tick={{ fontSize: 10, fill: '#95a5a6' }}
                        interval={isNarrow ? 0 : 4}
                        ticks={dailyTicksNarrow}
                        minTickGap={isNarrow ? 18 : 8}
                      />
                      <YAxis yAxisId="usage" tick={{ fontSize: 12, fill: '#95a5a6' }} unit="kWh" />
                      <YAxis yAxisId="cost" orientation="right" tick={{ fontSize: 11, fill: '#D13212' }} tickFormatter={(v) => formatCurrency(v)} />
                      <Tooltip
                        labelFormatter={(_, payload) => payload?.[0]?.payload?.day || 'Day'}
                        formatter={(value, name) => {
                          if (name === 'Charger energy') return [`${Number(value).toFixed(2)} kWh`, name];
                          return [formatCurrency(value), name];
                        }}
                      />
                      <Line yAxisId="usage" type="monotone" dataKey="usage_kwh" name="Charger energy" stroke="#0073BB" strokeWidth={2} dot={false} />
                      <Line yAxisId="cost" type="linear" dataKey="cost" name="Estimated charging cost" stroke="#D13212" strokeWidth={2} dot={false} connectNulls={false} />
                    </LineChart>
                  </ResponsiveContainer>
                )}
              </div>
              {isNarrow && <div className="graph-hint">Swipe left or right on the chart to switch graphs.</div>}
              {graphView === 'daily60' && (
                <details className="tariff-details">
                  <summary>Cost estimate and tariff assumptions</summary>
                  {data.grid_tariff ? (
                    <div className="tariff-detail-list">
                      <span>Charger energy (kWh) multiplied by the time-of-use rate in effect when it was drawn.</span>
                      <span>Off-peak: {formatCurrency(data.grid_tariff.off_peak_rate, 3)}/kWh, {String(data.grid_tariff.off_peak_start_hour).padStart(2, '0')}:00–{String(data.grid_tariff.off_peak_end_hour).padStart(2, '0')}:00.</span>
                      <span>Peak: {formatCurrency(data.grid_tariff.peak_rate_summer, 3)}/kWh Dec–Feb, {formatCurrency(data.grid_tariff.peak_rate_other, 3)}/kWh otherwise · {data.grid_tariff.timezone}. Edit in the config (⚙).</span>
                    </div>
                  ) : <p>Tariff settings are unavailable; treat these costs as indicative only.</p>}
                </details>
              )}
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
                  <thead><tr><th>Connector</th><th>Status</th><th>Power</th><th title="Cumulative energy imported on the charger's meter register; spans sessions and may reset if the meter is reset.">Cumulative Import</th><th>Vehicle SoC</th><th>Last Update</th></tr></thead>
                  <tbody>
                    {Object.entries(selectedCp.physical_status || {}).map(([connId, status]) => {
                      const mv = (selectedCp.meter_values || {})[connId] || {};
                      return (<tr key={connId}><td data-label="Connector">Connector {connId}</td><td data-label="Status"><span className={'badge ' + (status === 'Charging' ? 'badge-on' : status === 'Available' ? 'badge-info' : 'badge-off')}>{status}</span></td><td data-label="Power">{mv.power != null ? formatPower(mv.power) : '—'}</td><td data-label="Cumulative import">{mv.energy != null ? (mv.energy / 1000).toFixed(1) + ' kWh' : '—'}</td><td data-label="Vehicle SoC">{mv.soc_percent != null ? mv.soc_percent + '%' : 'Not reported by charger'}</td><td data-label="Last update" className="date-cell">{mv.timestamp ? new Date(mv.timestamp).toLocaleTimeString() : '—'}</td></tr>);
                    })}
                    {Object.keys(selectedCp.physical_status || {}).length === 0 && <tr className="empty-row"><td colSpan={6} style={{textAlign: 'center', color: '#95a5a6'}}>No connectors active</td></tr>}
                  </tbody>
                </table></div>
              </div>
            </div>
          )}

          {selectedCp && (
            <div className="card">
              <div className="card-header">
                <h3>Charge History: {selectedCp.id}</h3>
                <span className="text-secondary" style={{fontSize: 12}}>Recent sessions</span>
              </div>
              <div className="card-body">
                <div className="table-wrap"><table className="data-table">
                  <thead><tr><th>Started</th><th>State</th><th>Energy Delivered</th><th>Vehicle SoC</th><th>Health</th><th>Events</th></tr></thead>
                  <tbody>
                    {(selectedCp.recent_charge_sessions || []).map((session) => {
                      const soc = session.soc_end_percent ?? session.soc_start_percent;
                      const healthClass = session.health === 'ok' ? 'badge-on' : session.health === 'faulted' ? 'badge-off' : 'badge-warn';
                      const eventCount = (session.faults || []).length + (session.monitoring_gaps || []).length;
                      return (
                        <tr key={session.session_id}>
                          <td data-label="Started" className="date-cell">{session.plugged_at ? new Date(session.plugged_at).toLocaleString() : '—'}</td>
                          <td data-label="State"><span className={'badge ' + (session.state === 'charging' ? 'badge-on' : 'badge-neutral')}>{session.state || 'unknown'}</span></td>
                          <td data-label="Energy delivered">{session.meter_start_wh != null && session.last_energy_wh != null ? (session.energy_delivered_wh / 1000).toFixed(3) + ' kWh' : '—'}</td>
                          <td data-label="Vehicle SoC">{soc != null ? soc + '%' : 'Not reported by charger'}</td>
                          <td data-label="Session health"><span className={'badge ' + healthClass}>{session.health || 'unknown'}</span></td>
                          <td data-label="Events">{eventCount ? `${(session.faults || []).length} faults, ${(session.monitoring_gaps || []).length} gaps` : 'None'}</td>
                        </tr>
                      );
                    })}
                    {(selectedCp.recent_charge_sessions || []).length === 0 && <tr className="empty-row"><td colSpan={6} style={{textAlign: 'center', color: '#95a5a6'}}>No charge sessions recorded</td></tr>}
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
              <div className="rules-header-meta">
                <span className="badge badge-info">{(() => { const cfg = schedule[effectiveCpId] || {}; const mode = cfg.mode || 'charge_now'; return mode === 'stop' ? '🛑 STOP' : mode === 'auto' ? '⏱ AUTO' : '⚡ CHARGE NOW'; })()}</span>
                <span className="badge badge-neutral">{effectiveCpId ? ('Hour: ' + getNow()) : 'No CP'}</span>
              </div>
            </div>
            <div className="card-body">
              {!effectiveCpId ? <div className="empty-state"><p>Select a charge point to view rules.</p></div> : (() => {
                const cfg = schedule[effectiveCpId] || {}; const mode = cfg.mode || 'charge_now';
                const tz = cfg.timezone || 'Australia/Sydney';
                return (<>
                  {mode === 'stop' && <div className="decision-reason override-notice"><strong>🛑 STOP:</strong> All charging blocked. New sessions rejected.</div>}
                  {mode === 'charge_now' && <div className="decision-reason"><strong>⚡ CHARGE NOW:</strong> Full power mode is active. Schedule limits are bypassed.</div>}
                  {mode === 'auto' && <div className="decision-reason"><strong>⏱ AUTO:</strong> OFF / 6A / 8A / 16A / 32A ({tz})
                    <ul className="rules-list rules-list-ocpp">
                      <li><strong>{String(cfg.off_peak_start_hour ?? 0).padStart(2, '0')}:00–{String(cfg.off_peak_end_hour ?? 6).padStart(2, '0')}:00:</strong> off-peak, charge at {cfg.max_amps ?? 16}A (grid allowed)</li>
                      <li><strong>Otherwise:</strong> follow solar/battery surplus up to {cfg.max_amps ?? 16}A, never import from grid</li>
                      <li><strong>Battery below {cfg.min_battery_soc ?? 50}%:</strong> OFF outside off-peak</li>
                    </ul></div>}
                </>);
              })()}
            </div>
          </div>

          {/* Recent Events */}
          <div className="card">
            <div className="card-header"><h3>Recent Activity</h3><span className="text-secondary" style={{fontSize: 12}}>{effectiveCpId ? `Latest events · ${effectiveCpId}` : 'Latest events · all chargers'}</span></div>
            <div className="card-body">
              <div className="event-filters" role="group" aria-label="Filter recent activity">
                {EVENT_FILTERS.map((filter) => (
                  <button key={filter.id} type="button" className={'event-filter' + (eventFilter === filter.id ? ' active' : '')}
                    aria-pressed={eventFilter === filter.id} onClick={() => { setEventFilter(filter.id); setShowAllEvents(false); }}>
                    {filter.label}
                  </button>
                ))}
              </div>
              {visibleEvents.length === 0 ? <div className="empty-state"><p>No matching events in the recent event buffer.</p></div> : (
                <ol className="event-list">
                  {visibleEvents.map((event, index) => (
                    <li className="event-item" key={`${event.time}-${event.type}-${event.charge_point_id}-${index}`}>
                      <div className="event-meta">
                        <time dateTime={event.time}>{new Date(event.time).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</time>
                        <span className={'badge ' + getEventBadge(event.type)}>{event.type.replaceAll('_', ' ')}</span>
                        {!effectiveCpId && <span className="event-charge-point">{event.charge_point_id}</span>}
                      </div>
                      <p className="event-summary">{event.summary || 'No additional details'}</p>
                    </li>
                  ))}
                </ol>
              )}
              {filteredEvents.length > 8 && (
                <button type="button" className="event-more" aria-expanded={showAllEvents} onClick={() => setShowAllEvents((value) => !value)}>
                  {showAllEvents ? 'Show fewer' : `Show all ${filteredEvents.length} matching events`}
                </button>
              )}
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
          <div className="modal-overlay ocpp-modal-overlay"
            onClick={(e) => { if (e.target === e.currentTarget) setShowConfig(false); }}>
            <div className="modal-content card ocpp-modal-content">
              <div className="card-header"><h3>⚙ Configure Schedule - {effectiveCpId}</h3><button className="btn btn-secondary" style={{padding: '4px 10px'}} onClick={() => setShowConfig(false)}>✕</button></div>
              <div className="card-body">
                <div style={{marginBottom: 16}}>
                  <label className="ocpp-field-label">Off-peak window (grid power allowed)<InfoTip text="Hours when grid power is cheap. Auto charges at the maximum current during this window, even if solar and battery are low. Outside it, Auto never imports from the grid." /></label>
                  <div className="ocpp-period-row">
                    <input type="number" min={0} max={23} value={editOffPeakStart} onChange={(e) => setEditOffPeakStart(parseInt(e.target.value, 10) || 0)} className="ocpp-input ocpp-input-sm" aria-label="Off-peak start hour" />
                    <span style={{alignSelf: 'center'}}>to</span>
                    <input type="number" min={0} max={23} value={editOffPeakEnd} onChange={(e) => setEditOffPeakEnd(parseInt(e.target.value, 10) || 0)} className="ocpp-input ocpp-input-sm" aria-label="Off-peak end hour" />
                  </div>
                </div>
                <div style={{marginBottom: 16}}>
                  <label className="ocpp-field-label">Max current<InfoTip text="Highest current Auto will ever use. Your MG HS tops out at 16A, so 16A is right. Auto steps through OFF, 6A, 8A, 16A up to this limit." /></label>
                  <select value={editMaxAmps} onChange={(e) => setEditMaxAmps(parseInt(e.target.value, 10))} className="ocpp-input ocpp-input-sm">{[6, 8, 16, 32].map(a => <option key={a} value={a}>{a}A</option>)}</select>
                </div>
                <div style={{marginBottom: 16}}>
                  <label className="ocpp-field-label">Keep home battery above (%)<InfoTip text="Outside off-peak, Auto stops charging the car if the home battery falls below this level, so the house keeps its reserve." /></label>
                  <input type="number" min={0} max={100} value={editMinBatterySoc} onChange={(e) => setEditMinBatterySoc(parseInt(e.target.value, 10) || 0)} className="ocpp-input ocpp-input-sm" />
                </div>
                <div style={{marginBottom: 16}}>
                  <label className="ocpp-field-label">Electricity prices ($/kWh, all-in)<InfoTip text="What you pay per kWh including all charges. Used only to estimate charging cost; it doesn't change how charging is controlled. Peak applies outside the off-peak window." /></label>
                  <div className="ocpp-period-row">
                    {[['off_peak_rate', 'Off-peak'], ['peak_rate_summer', 'Peak Dec–Feb'], ['peak_rate_other', 'Peak Mar–Nov']].map(([k, lbl]) => (
                      <div key={k} style={{flex: 1}}>
                        <label className="ocpp-small-label">{lbl}</label>
                        <input type="number" step="0.001" min={0} value={editRates[k]} onChange={(e) => setEditRates({ ...editRates, [k]: parseFloat(e.target.value) || 0 })} className="ocpp-input ocpp-input-sm" />
                      </div>
                    ))}
                  </div>
                </div>
                <div style={{display: 'flex', gap: 8}}>
                  <button className="btn btn-secondary" style={{flex: 1}} onClick={() => setShowConfig(false)}>Cancel</button>
                  <button className="btn btn-primary" style={{flex: 1}} disabled={schedulePending} onClick={saveScheduleConfig}>Save</button>
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

function Home() {
  const [cps, setCps] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    const load = async () => {
      try {
        const res = await fetch(BASE + 'debug');
        if (!res.ok) throw new Error('Server unavailable');
        const json = await res.json();
        const list = json.charge_points || [];
        if (list.length === 1) {
          window.location.replace(BASE + encodeURIComponent(list[0].id));
          return;
        }
        setCps(list);
        setError(null);
      } catch {
        setError('Connection lost - retrying...');
      }
    };
    load();
    const interval = setInterval(load, 5000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="app">
      <header className="aws-navbar">
        <div className="navbar-brand">
          <span className="brand-icon">🔌</span><span>IoT Core</span>
          <span className="brand-divider">|</span><span className="brand-service"><span className="brand-long">OCPP MQTT Bridge</span><span className="brand-short">OCPP</span></span>
        </div>
      </header>
      <main className="main-content">
        {error && <div className="error-card"><p>{error}</p></div>}
        {!cps && !error && <div className="loader">Loading chargers...</div>}
        {cps && cps.length === 0 && <div className="empty-state"><p>No chargers known yet.</p></div>}
        <div style={{display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 16}}>
          {(cps || []).map(cp => {
            const watts = summarizeChargePower([cp]).watts || 0;
            return (
              <a key={cp.id} href={BASE + encodeURIComponent(cp.id)}
                style={{display: 'block', padding: 20, background: '#fff', border: '1px solid #D5DBDB', borderRadius: 6, textDecoration: 'none', color: '#16191F', boxShadow: '0 1px 2px rgba(0,0,0,0.08)'}}>
                <div style={{fontSize: 20, fontWeight: 600}}>{cp.id}</div>
                <div style={{marginTop: 8, fontSize: 13, color: cp.connected ? '#1D8102' : '#879596'}}>
                  ● {cp.connected ? 'Connected' : 'Offline'}{cp.status ? ` · ${cp.status}` : ''}
                </div>
                {cp.connected && watts > 0 && <div style={{marginTop: 4, fontSize: 13}}>{formatPower(watts)}</div>}
              </a>
            );
          })}
        </div>
      </main>
    </div>
  );
}

export default function App() {
  const rel = window.location.pathname.startsWith(BASE) ? window.location.pathname.slice(BASE.length) : window.location.pathname.replace(/^\//, '');
  const cpId = decodeURIComponent(rel.split('/')[0] || '');
  return cpId ? <ChargerPage routeCpId={cpId} /> : <Home />;
}
