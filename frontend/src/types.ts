/// <reference types="vite/client" />

export type TradeEventType =
  | "agent.dispatch"
  | "tool.invoke"
  | "tool.result"
  | "token.delta"
  | "plan.update"
  | "context.compressed"
  | "model.fallback"
  | "final.result"
  | "error";

export interface TradeEvent {
  type: TradeEventType;
  payload: Record<string, any>;
  occurred_at: string;
}

export interface LandedPrice {
  ship_to: string;
  subtotal_major: number;
  freight_major: number;
  tariff_major: number;
  tariff_rate: number;
  de_minimis_applied: boolean;
  landed_total_major: number;
  currency: string;
  unavailable_reason?: string;
}

export interface ProductCard {
  product_id: string;
  title: string;
  brand: string;
  category: string;
  origin_country: string;
  price_major: number;
  currency: string;
  highlights: string[];
  skus: {
    sku_id: string;
    spec: string;
    price_major: number;
    currency: string;
    stock: number;
  }[];
  score: number;
  landed_price?: LandedPrice;
  description?: string;
  rating_summary?: { average: number; review_count: number } | null;
  rating_is_live?: boolean;
  ships_to?: string[];
  dimensions_cm?: { length?: number; width?: number; height?: number };
  updated_at?: string;
  default_sku_id?: string;
  image_url?: string | null;
  image_kind?: "illustration" | "placeholder";
  image_alt?: string;
  source_platform?: string;
  source_price_major?: number;
  source_currency?: string;
  canonical_product_id?: string;
  material_tags?: string[];
  weight_kg?: number;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  runId?: string;
}

export interface DiagnosticEvent {
  id: string;
  type: string;
  label: string;
  timestamp: number | null;
  detail?: string;
}

/** 服务端投影的可展示执行过程，原始工具参数不进入界面状态。 */
export interface ExecutionActivity {
  id: string;
  runId: string;
  kind: "tool" | "thinking";
  toolCallId?: string;
  toolName?: string;
  label: string;
  status: "preparing" | "running" | "waiting" | "completed" | "error" | "cancelled" | "interrupted" | "approved";
  sequence: number;
  startedAt: number | null;
  finishedAt: number | null;
  inputSummary?: string;
  resultSummary?: string;
  content?: string;
  truncated?: boolean;
  source?: string;
}

export interface SessionSummary {
  id: string;
  title: string;
  updatedAt: number;
  source?: "server" | "local";
  origin?: "automation";
}

export interface ToolApproval { id: string; tool: string; label: string; arguments: string | Record<string, unknown> }

export interface CommerceSnapshot {
  activities?: ExecutionActivity[];
  productHistory?: {runId: string; updatedAt: number; products: ProductCard[]}[];
  shoppingForm?: unknown;
  toolApprovals?: ToolApproval[];
  skills: PublishedSkill[];
  skillsStatus: "loading" | "ready" | "error";
  skillsError: string | null;
  skillUsages: SkillUsage[];
  sessionId: string;
  messages: ChatMessage[];
  products: ProductCard[];
  events: DiagnosticEvent[];
  status: "idle" | "running" | "stopped" | "error";
  step: string;
  error: string | null;
  searchCompleted: boolean;
  history: SessionSummary[];
  confirmations: TradeConfirmation[];
  confirmationBusy: boolean;
  confirmationError: string | null;
  recoverableRunId: string | null;
  historyError: string | null;
}

export interface ShippingAddress {
  recipient_name: string;
  country: string;
  state: string;
  city: string;
  address_line: string;
  postal_code: string;
  phone: string;
}
export interface PrepareOrderInput {
  items: { product_id: string; sku_id: string; quantity: number }[];
  shipping_address: ShippingAddress;
}
export interface OrderSnapshot {
  order_id: string;
  status: "CONFIRMED" | "CANCELLED";
  total_amount_minor: number;
  total_amount_major: number;
  currency: string;
  cancel_reason: string | null;
}
export interface TradeConfirmation {
  confirmation_id: string;
  operation_id: string;
  action: "create" | "cancel";
  buyer_id: string;
  session_id: string;
  snapshot_hash: string;
  expires_at: string;
  expired: boolean;
  status: "pending" | "approved" | "rejected";
  payload: {
    items: {
      product_id: string;
      sku_id: string;
      title: string;
      unit_price_minor: number;
      currency: string;
      quantity: number;
    }[];
    shipping_address: ShippingAddress;
    total_amount_minor: number;
    currency: string;
    amount_scope: "merchandise_only";
    order_kind: "purchase_intent";
    order_id?: string;
    reason?: string;
    order_status?: string;
  };
  result: OrderSnapshot | null;
}

export interface PublishedSkill {
  id: string;
  version: string;
  title: string;
  description: string;
  scope: string;
  content_hash: string;
  expires_at: string | null;
}
export interface SelectedSkill {
  id: string;
  version: string;
  contentHash: string;
}
export interface SkillUsage {
  toolCallId: string;
  source?: "server_preload";
  id?: string;
  version?: string;
  title?: string;
  contentHash?: string;
  status: "reading" | "used" | "error";
}
