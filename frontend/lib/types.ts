// Shapes returned by the SignalGuard API. Money fields are strings on the wire
// (never floats — constraint #3), so they stay strings here too.

export interface User {
  id: string;
  email: string;
  created_at: string;
}

export interface RiskProfile {
  version: number;
  max_alert_age_sec: number;
  future_tolerance_sec: number;
  dedupe_window_sec: number;
  allowed_symbols: string[];
  min_stop_distance_pct: string;
  consecutive_loss_threshold: number;
  circuit_breaker_cooldown_minutes: number;
  circuit_breaker_manual_reset: boolean;
  max_daily_dd_pct: string;
  risk_per_trade_pct: string;
  fee_slippage_buffer_bps: number;
  max_notional_per_trade: string;
  max_open_positions: number;
  max_total_notional: string;
  allow_pyramiding: boolean;
  daily_reset_time: string;
  timezone: string;
  updated_at: string;
}

export interface BrokerAccount {
  id: string;
  broker: string;
  label: string;
  is_testnet: boolean;
  is_active: boolean;
  trading_state: "ACTIVE" | "LOCKED";
  locked_at: string | null;
  locked_reason: string | null;
  created_at: string;
}

export interface WebhookEndpointCreated {
  id: string;
  is_active: boolean;
  created_at: string;
  last_used_at: string | null;
  endpoint_token: string;
  hmac_secret: string;
  body_secret: string;
}

export interface Decision {
  id: string;
  alert_id: string;
  broker_account_id: string | null;
  verdict: "APPROVED" | "REJECTED";
  reason_code: string;
  reason_detail: string | null;
  computed_qty: string | null;
  entry_reference_price: string | null;
  stop_price: string | null;
  evaluated_at: string;
  latency_ms: number;
  is_test: boolean;
}

export interface NotificationSettings {
  telegram_chat_id: string | null;
}

export interface Position {
  symbol: string;
  qty: string;
  avg_entry: string;
  mark_price: string | null;
  unrealized_pnl: string | null;
  updated_at: string;
}

export interface EquityPoint {
  equity: string;
  free_balance: string | null;
  taken_at: string;
  is_session_baseline: boolean;
}

// A closed round-trip. Money stays a string all the way to the render — parsing
// it into a JS number would reintroduce exactly the float error the backend
// works to avoid (constraint #3).
export interface Trade {
  id: string;
  symbol: string;
  side: string;
  qty: string;
  entry_price: string;
  exit_price: string;
  realized_pnl: string;
  fees: string;
  opened_at: string | null;
  closed_at: string;
}

// A realtime event as forwarded by /ws.
export interface WsEvent {
  type: "connected" | "heartbeat" | "decision" | "order" | "position" | "equity";
  data?: Record<string, unknown>;
}

export const REASON_CODES = [
  "APPROVED",
  "TRADING_LOCKED",
  "INVALID_PAYLOAD",
  "STALE_ALERT",
  "DUPLICATE_ALERT",
  "SYMBOL_NOT_ALLOWED",
  "NO_STOP_LOSS",
  "CIRCUIT_BREAKER_OPEN",
  "DAILY_DRAWDOWN_HIT",
  "SIZE_BELOW_MINIMUM",
  "EXPOSURE_LIMIT",
  "BROKER_UNAVAILABLE",
  "STATE_UNAVAILABLE",
  "ACCOUNT_NOT_FOUND",
  "INSTRUMENT_UNAVAILABLE",
  "INTERNAL_ERROR",
] as const;
