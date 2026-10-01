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
      cost: samples > 0 ? Number(entry.cost || 0) : null,
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
  const [editEveningStartHour, setEditEveningStartHour] = useState(17);
  const [editBatteryPrioritySoc, setEditBatteryPrioritySoc] = useState(50);
  const [editGridDeadbandW, setEditGridDeadbandW] = useState(150);
  const [editMinimumSpareW, setEditMinimumSpareW] = useState(500);
  const [editBufferW, setEditBufferW] = useState(500);
  const [editPvPowerThresholdW, setEditPvPowerThresholdW] = useState(1000);
  const [editBatteryOnlyMaxAmps, setEditBatteryOnlyMaxAmps] = useState(24);
  const [editOvernightCurrentAmps, setEditOvernightCurrentAmps] = useState(32);
  const [editOverrideLowSocThreshold, setEditOverrideLowSocThreshold] = useState(15);
  const [editOverrideLowCurrent, setEditOverrideLowCurrent] = useState(8);
  const [editOverrideHighCurrent, setEditOverrideHighCurrent] = useState(16);
  const [editOverrideBoostCurrent, setEditOverrideBoostCurrent] = useState(32);
  const [editOverrideSolarBoostThresholdW, setEditOverrideSolarBoostThresholdW] = useState(2000);
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
  const effectiveCpId = selectedCpId || (connectedCps[0]?.id) || (chargePoints[0]?.id) || null;
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
      timezone: editTimezone,
      periods: [...editPeriods].sort((a, b) => a.start_hour - b.start_hour),
      solar_smart: editSolarSmart,
      off_peak_start_hour: editOffPeakStart,
      off_peak_end_hour: editOffPeakEnd,
      evening_start_hour: editEveningStartHour,
      battery_priority_soc: editBatteryPrioritySoc,
      grid_deadband_w: editGridDeadbandW,
      minimum_spare_power_w: editMinimumSpareW,
      buffer_power_w: editBufferW,
      pv_power_threshold_w: editPvPowerThresholdW,
      battery_only_max_amps: editBatteryOnlyMaxAmps,
      overnight_current_amps: editOvernightCurrentAmps,
      override_low_soc_threshold: editOverrideLowSocThreshold,
      override_low_current: editOverrideLowCurrent,
      override_high_current: editOverrideHighCurrent,
      override_boost_current: editOverrideBoostCurrent,
      override_solar_boost_threshold_w: editOverrideSolarBoostThresholdW,
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
  const schedulePeriods = [...(scheduleConfig.periods || DEFAULT_PERIODS)]
    .sort((a, b) => a.start_hour - b.start_hour);
  const currentHour = effectiveCpId ? getNow() : 0;
  let activePeriod = null;
  for (const period of schedulePeriods) {
    if (period.start_hour <= currentHour) activePeriod = period;
  }
  const solarControl = data?.solar_control || {};
  const solarControlState = solarControl.states?.[effectiveCpId];
  const requestedSolarLimit = solarControlState?.target_watts ?? data?.solar_throttle?.[effectiveCpId];
  const hasSolarLimit = requestedSolarLimit != null && Number.isFinite(Number(requestedSolarLimit));
  const powerIsStale = powerSummary.watts != null
    && (powerSummary.fromHistory || readingIsStale(powerSummary.lastReportedAt));
  const solarTelemetryIsStale = readingIsStale(solar.last_update);

  let powerControlReason = 'No charger power-control target is currently available.';
  if (scheduleMode === 'stop') {
    powerControlReason = 'STOP mode blocks charging and requests a stop for any active session.';
  } else if (scheduleMode === 'auto' && activePeriod?.limit_watts <= 0) {
    powerControlReason = 'The active AUTO schedule window blocks charging.';
  } else if (scheduleMode === 'auto' && scheduleConfig.solar_smart && solarControlState?.direction === 'down') {
    const threshold = Number(solarControl.grid_import_threshold_w);
    powerControlReason = !solarTelemetryIsStale && Number.isFinite(threshold) && Number(solar.grid_import) > threshold
      ? `Grid import is ${formatPower(Number(solar.grid_import))}, above ${formatPower(threshold)}; Solar Smart is reducing its requested limit.`
      : 'Solar Smart last reported a downward limit adjustment; current conditions may have changed.';
  } else if (scheduleMode === 'auto' && scheduleConfig.solar_smart && solarControlState?.direction === 'up') {
    powerControlReason = 'Solar Smart is increasing its requested limit as conditions allow.';
  } else if (scheduleMode === 'auto' && scheduleConfig.solar_smart) {
    powerControlReason = 'Solar Smart is enabled; no limit ramp is currently reported.';
  } else if (scheduleMode === 'auto' && activePeriod) {
    powerControlReason = `The active AUTO window allows up to ${formatPower(activePeriod.limit_watts)}.`;
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
          <span className="brand-icon">🔌</span><span>IoT Core</span>
          <span className="brand-divider">|</span><span className="brand-service">OCPP MQTT Bridge</span>
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
              <div className={'summary-value' + (powerSummary.watts != null && totalPower > 0 && !powerIsStale ? ' text-green' : '')}>{powerSummary.watts != null ? formatPower(totalPower) : '—'}</div>
              <div className="summary-label">
                Charging Power
                <span className={'summary-subtext' + (powerIsStale ? ' is-stale' : '')}>
                  {powerSummary.lastReportedAt
                    ? `${powerIsStale ? 'Stale charger reading' : 'Charger report'} · ${formatReadingAge(powerSummary.lastReportedAt)}`
                    : 'No charger meter reading'}
                </span>
              </div>
            </div>
            <div className="summary-card">
              <div className={'summary-value' + (solarTelemetryIsStale ? '' : solar.grid_import > 0 ? ' text-red' : ' text-green')}>
                {solarTelemetryIsStale ? '—' : formatPower(solar.grid_import || 0)}
              </div>
              <div className="summary-label">
                Grid Import
                <span className={'summary-subtext' + (solarTelemetryIsStale ? ' is-stale' : '')}>
                  {solar.last_update ? `Site meter · ${formatReadingAge(solar.last_update)}` : 'No site meter update'}
                </span>
              </div>
            </div>
            <div className="summary-card">
              <div className="summary-value">
                {Number(todayUsage?.samples || 0) > 0 ? formatCurrency(todayUsage.cost) : '—'}
              </div>
              <div className="summary-label">
                Today Net Grid Spend
                <span className="summary-subtext">
                  {Number(todayUsage?.samples || 0) > 0
                    ? `${Number(todayUsage.usage_kwh || 0).toFixed(2)} kWh imported · estimate`
                    : 'Waiting for site meter samples'}
                </span>
              </div>
            </div>
            <div className="summary-card">
              <div className={'summary-value' + (!solarTelemetryIsStale && solar.grid_export > 2000 ? ' text-green' : '')}>
                {solarTelemetryIsStale ? '—' : formatPower(solar.grid_export || 0)}
              </div>
              <div className="summary-label">Grid Export</div>
            </div>
            <div className="summary-card">
              <div className={'summary-value' + (!solarTelemetryIsStale && solar.battery_soc > 50 ? ' text-green' : !solarTelemetryIsStale && solar.battery_soc <= 50 ? ' text-warn' : '')}>
                {solarTelemetryIsStale || solar.battery_soc == null ? '—' : `${solar.battery_soc}%`}
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
                {chargePoints.length > 0 && (
                  <select value={effectiveCpId || ''} onChange={(e) => setSelectedCpId(e.target.value || null)}
                    style={{ padding: '4px 8px', background: '#fff', border: '1px solid #D5DBDB', color: '#16191F', borderRadius: 3, fontSize: 13, maxWidth: 200 }}>
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
                  setEditEveningStartHour(cfg.evening_start_hour ?? 17);
                  setEditBatteryPrioritySoc(cfg.battery_priority_soc ?? 50);
                  setEditGridDeadbandW(cfg.grid_deadband_w ?? 150);
                  setEditMinimumSpareW(cfg.minimum_spare_power_w ?? 500);
                  setEditBufferW(cfg.buffer_power_w ?? 500);
                  setEditPvPowerThresholdW(cfg.pv_power_threshold_w ?? 1000);
                  setEditBatteryOnlyMaxAmps(cfg.battery_only_max_amps ?? 24);
                  setEditOvernightCurrentAmps(cfg.overnight_current_amps ?? 32);
                  setEditOverrideLowSocThreshold(cfg.override_low_soc_threshold ?? 15);
                  setEditOverrideLowCurrent(cfg.override_low_current ?? 8);
                  setEditOverrideHighCurrent(cfg.override_high_current ?? 16);
                  setEditOverrideBoostCurrent(cfg.override_boost_current ?? 32);
                  setEditOverrideSolarBoostThresholdW(cfg.override_solar_boost_threshold_w ?? 2000);
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
                <div className="power-context">
                  <strong>Why this power?</strong>
                  <span>{powerControlReason}</span>
                  {scheduleConfig.solar_smart && hasSolarLimit && (
                    <span>Latest Solar Smart limit request: {formatPower(Number(requestedSolarLimit))}. This is a bridge target, not confirmation the charger accepted it.</span>
                  )}
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
                          if (name === 'Grid import') return [`${Number(value).toFixed(2)} kWh`, name];
                          return [formatCurrency(value), name];
                        }}
                      />
                      <Line yAxisId="usage" type="monotone" dataKey="usage_kwh" name="Grid import" stroke="#0073BB" strokeWidth={2} dot={false} />
                      <Line yAxisId="cost" type="linear" dataKey="cost" name="Estimated net site cost" stroke="#D13212" strokeWidth={2} dot={false} connectNulls={false} />
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
                      <span>Whole-site grid imports, less feed-in credits; not EV-only charging cost.</span>
                      <span>Off-peak: {formatCurrency(data.grid_tariff.off_peak_rate, 4)}/kWh, {String(data.grid_tariff.off_peak_start_hour).padStart(2, '0')}:00–{String(data.grid_tariff.off_peak_end_hour).padStart(2, '0')}:00.</span>
                      <span>General: {formatCurrency(data.grid_tariff.general_rate, 4)}/kWh plus seasonal demand adder: summer {formatCurrency(data.grid_tariff.summer_demand_rate, 4)}, other months {formatCurrency(data.grid_tariff.non_summer_demand_rate, 4)}.</span>
                      <span>Feed-in credit: {formatCurrency(data.grid_tariff.feed_in_tariff, 4)}/kWh · {data.grid_tariff.timezone}.</span>
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
                const periods = cfg.periods || DEFAULT_PERIODS; const sortedPeriods = [...periods].sort((a, b) => a.start_hour - b.start_hour);
                const tz = cfg.timezone || 'Australia/Sydney'; const currentHour = getNow();
                let activeIdx = sortedPeriods.length - 1;
                for (let i = 0; i < sortedPeriods.length; i++) { if (sortedPeriods[i].start_hour <= currentHour) activeIdx = i; }
                return (<>
                  {mode === 'stop' && <div className="decision-reason override-notice"><strong>🛑 STOP:</strong> All charging blocked. New sessions rejected.</div>}
                  {mode === 'charge_now' && <div className="decision-reason"><strong>⚡ CHARGE NOW:</strong> Full power mode is active. Schedule limits are bypassed.</div>}
                  {mode === 'auto' && <div className="decision-reason"><strong>⏱ AUTO:</strong> Time-of-day schedule ({tz})<div className="rules-detail-text">Current hour: {currentHour}:00 — active period limit is {sortedPeriods[activeIdx]?.limit_watts || '?'}W.</div></div>}
                  <ul className="rules-list rules-list-ocpp">
                    {sortedPeriods.map((p, i) => { const endHour = i < sortedPeriods.length - 1 ? sortedPeriods[i + 1].start_hour : 24; const isActive = mode === 'auto' && i === activeIdx; return (<li key={i} className={isActive ? 'active' : ''}><strong>{String(p.start_hour).padStart(2, '0')}:00–{String(endHour).padStart(2, '0')}:00:</strong> {p.limit_watts > 0 ? p.limit_watts + 'W' : 'BLOCKED'}{isActive ? ' ← active' : ''}</li>); })}
                  </ul>
                  {cfg.solar_smart && <div className="hint rules-hint">☀️ <strong>Solar Smart:</strong> Active — dynamically throttles in peak hours based on grid import/export.{cfg.off_peak_start_hour != null ? ' Off-peak: ' + cfg.off_peak_start_hour + ':00–' + cfg.off_peak_end_hour + ':00.' : ''}</div>}
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
                  <label className="ocpp-field-label">Timezone</label>
                  <select value={editTimezone} onChange={(e) => setEditTimezone(e.target.value)}
                    className="ocpp-input">
                    {(timezones.length > 0 ? timezones : ['Australia/Sydney', 'UTC']).map(tz => (<option key={tz} value={tz}>{tz}</option>))}
                  </select>
                </div>
                <div className="ocpp-modal-panel">
                  <label style={{display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', marginBottom: 12}}>
                    <input type="checkbox" checked={editSolarSmart} onChange={(e) => setEditSolarSmart(e.target.checked)} style={{width: 18, height: 18, cursor: 'pointer'}} />
                    <span style={{fontWeight: 600, fontSize: 14}}>☀️ Solar Smart</span>
                  </label>
                  <p className="ocpp-help-text" style={{marginBottom: 10}}>Dynamically throttle charging based on solar/grid balance.</p>
                  {editSolarSmart && (<div style={{display: 'flex', gap: 16}}>
                    <div style={{flex: 1}}><label className="ocpp-small-label">Off-Peak Start</label><input type="number" min={0} max={23} value={editOffPeakStart} onChange={(e) => setEditOffPeakStart(parseInt(e.target.value) || 0)} className="ocpp-input ocpp-input-sm" /></div>
                    <div style={{flex: 1}}><label className="ocpp-small-label">Off-Peak End</label><input type="number" min={0} max={23} value={editOffPeakEnd} onChange={(e) => setEditOffPeakEnd(parseInt(e.target.value) || 0)} className="ocpp-input ocpp-input-sm" /></div>
                  </div>)}
                </div>

                <div className="ocpp-period-row">
                  <div style={{flex: 1}}><label className="ocpp-small-label">Evening Start Hour</label><input type="number" min={0} max={23} value={editEveningStartHour} onChange={(e) => setEditEveningStartHour(parseInt(e.target.value, 10) || 17)} className="ocpp-input ocpp-input-sm" /></div>
                  <div style={{flex: 1}}><label className="ocpp-small-label">Overnight Current A</label><input type="number" min={8} max={32} value={editOvernightCurrentAmps} onChange={(e) => setEditOvernightCurrentAmps(parseInt(e.target.value, 10) || 32)} className="ocpp-input ocpp-input-sm" /></div>
                </div>
                <div className="ocpp-period-row">
                  <div style={{flex: 1}}><label className="ocpp-small-label">Battery Priority SOC %</label><input type="number" min={0} max={100} value={editBatteryPrioritySoc} onChange={(e) => setEditBatteryPrioritySoc(parseInt(e.target.value, 10) || 50)} className="ocpp-input ocpp-input-sm" /></div>
                  <div style={{flex: 1}}><label className="ocpp-small-label">Grid Deadband W</label><input type="number" min={0} max={5000} value={editGridDeadbandW} onChange={(e) => setEditGridDeadbandW(parseInt(e.target.value, 10) || 150)} className="ocpp-input ocpp-input-sm" /></div>
                </div>
                <div className="ocpp-period-row">
                  <div style={{flex: 1}}><label className="ocpp-small-label">Minimum Spare W</label><input type="number" min={0} max={10000} value={editMinimumSpareW} onChange={(e) => setEditMinimumSpareW(parseInt(e.target.value, 10) || 500)} className="ocpp-input ocpp-input-sm" /></div>
                  <div style={{flex: 1}}><label className="ocpp-small-label">Buffer W</label><input type="number" min={0} max={10000} value={editBufferW} onChange={(e) => setEditBufferW(parseInt(e.target.value, 10) || 500)} className="ocpp-input ocpp-input-sm" /></div>
                </div>
                <div className="ocpp-period-row">
                  <div style={{flex: 1}}><label className="ocpp-small-label">PV Threshold W</label><input type="number" min={0} max={20000} value={editPvPowerThresholdW} onChange={(e) => setEditPvPowerThresholdW(parseInt(e.target.value, 10) || 1000)} className="ocpp-input ocpp-input-sm" /></div>
                  <div style={{flex: 1}}><label className="ocpp-small-label">Battery-Only Max A</label><input type="number" min={8} max={32} value={editBatteryOnlyMaxAmps} onChange={(e) => setEditBatteryOnlyMaxAmps(parseInt(e.target.value, 10) || 24)} className="ocpp-input ocpp-input-sm" /></div>
                </div>
                <div className="ocpp-period-row">
                  <div style={{flex: 1}}><label className="ocpp-small-label">Override Low SOC %</label><input type="number" min={0} max={100} value={editOverrideLowSocThreshold} onChange={(e) => setEditOverrideLowSocThreshold(parseInt(e.target.value, 10) || 15)} className="ocpp-input ocpp-input-sm" /></div>
                  <div style={{flex: 1}}><label className="ocpp-small-label">Override Solar Boost W</label><input type="number" min={0} max={20000} value={editOverrideSolarBoostThresholdW} onChange={(e) => setEditOverrideSolarBoostThresholdW(parseInt(e.target.value, 10) || 2000)} className="ocpp-input ocpp-input-sm" /></div>
                </div>
                <div className="ocpp-period-row">
                  <div style={{flex: 1}}><label className="ocpp-small-label">Override Low A</label><input type="number" min={8} max={32} value={editOverrideLowCurrent} onChange={(e) => setEditOverrideLowCurrent(parseInt(e.target.value, 10) || 8)} className="ocpp-input ocpp-input-sm" /></div>
                  <div style={{flex: 1}}><label className="ocpp-small-label">Override High A</label><input type="number" min={8} max={32} value={editOverrideHighCurrent} onChange={(e) => setEditOverrideHighCurrent(parseInt(e.target.value, 10) || 16)} className="ocpp-input ocpp-input-sm" /></div>
                </div>
                <div className="ocpp-period-row">
                  <div style={{flex: 1}}><label className="ocpp-small-label">Override Boost A</label><input type="number" min={8} max={32} value={editOverrideBoostCurrent} onChange={(e) => setEditOverrideBoostCurrent(parseInt(e.target.value, 10) || 32)} className="ocpp-input ocpp-input-sm" /></div>
                  <div style={{flex: 1}}></div>
                </div>

                <p className="hint ocpp-help-text" style={{marginBottom: 16, fontSize: 13}}>Each period sets a power limit starting at a given hour. Schedule repeats <strong>daily</strong>.</p>
                {editPeriods.map((p, i) => (
                  <div key={i} className="ocpp-period-row">
                    <div style={{flex: 1}}><label className="ocpp-small-label">Start Hour</label><input type="number" min={0} max={23} value={p.start_hour} onChange={(e) => { const next = [...editPeriods]; next[i] = {...next[i], start_hour: parseInt(e.target.value) || 0}; setEditPeriods(next); }} className="ocpp-input ocpp-input-sm" /></div>
                    <div style={{flex: 2}}><label className="ocpp-small-label">Limit (Watts)</label><input type="number" min={0} max={50000} step={100} value={p.limit_watts} onChange={(e) => { const next = [...editPeriods]; next[i] = {...next[i], limit_watts: parseInt(e.target.value) || 0}; setEditPeriods(next); }} className="ocpp-input ocpp-input-sm" /></div>
                    <button className="btn btn-secondary" style={{padding: '4px 8px', fontSize: 12}} disabled={editPeriods.length <= 1} onClick={() => { if (editPeriods.length > 1) setEditPeriods(editPeriods.filter((_, idx) => idx !== i)); }}>✕</button>
                  </div>
                ))}
                <button className="btn btn-secondary" style={{marginBottom: 16, width: '100%'}} onClick={() => setEditPeriods([...editPeriods, { start_hour: 0, limit_watts: 4800 }])}>+ Add Period</button>
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
