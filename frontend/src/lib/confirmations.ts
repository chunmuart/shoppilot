import type { TradeConfirmation } from "../types";
const record = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown): v is string => typeof v === "string" && !!v.trim();
const minor = (v: unknown): v is number =>
  typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
const currency = (v: unknown): v is string =>
  typeof v === "string" && /^[A-Z]{3}$/.test(v);

/** 只有结构完整的服务端快照才能成为可点击的确认卡。 */
export function readConfirmations(value: unknown): TradeConfirmation[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is TradeConfirmation => {
      if (
        !record(item) ||
        !text(item.confirmation_id) ||
        !text(item.operation_id) ||
        !text(item.buyer_id) ||
        !text(item.session_id) ||
        !text(item.snapshot_hash) ||
        !text(item.expires_at) ||
        !Number.isFinite(Date.parse(item.expires_at)) ||
        !["create", "cancel"].includes(String(item.action)) ||
        !["pending", "approved", "rejected"].includes(String(item.status))
      )
        return false;
      const p = item.payload;
      if (
        !record(p) ||
        p.amount_scope !== "merchandise_only" ||
        p.order_kind !== "purchase_intent" ||
        !minor(p.total_amount_minor) ||
        !currency(p.currency) ||
        !Array.isArray(p.items) ||
        !p.items.length
      )
        return false;
      let total = 0;
      for (const line of p.items) {
        if (
          !record(line) ||
          !text(line.product_id) ||
          !text(line.sku_id) ||
          !text(line.title) ||
          !minor(line.unit_price_minor) ||
          line.currency !== p.currency ||
          !minor(line.quantity) ||
          line.quantity <= 0
        )
          return false;
        total += line.unit_price_minor * line.quantity;
      }
      if (!Number.isSafeInteger(total) || total !== p.total_amount_minor)
        return false;
      const address = p.shipping_address;
      if (
        !record(address) ||
        !["recipient_name", "country", "city", "address_line"].every((key) =>
          text(address[key]),
        ) ||
        !["state", "postal_code", "phone"].every(
          (key) => typeof address[key] === "string",
        )
      )
        return false;
      if (item.action === "cancel" && (!text(p.order_id) || !text(p.reason)))
        return false;
      if (item.status === "approved") {
        const result = item.result;
        if (
          !record(result) ||
          !text(result.order_id) ||
          !["CONFIRMED", "CANCELLED"].includes(String(result.status)) ||
          !minor(result.total_amount_minor) ||
          !currency(result.currency)
        )
          return false;
      } else if (item.result !== null) return false;
      return true;
    })
    .map((item) => ({
      ...item,
      expired:
        item.expired === true || Date.parse(item.expires_at) <= Date.now(),
    }));
}

/** 决议单向推进，迟到的流式 pending 快照不能让确认按钮重新出现。 */
export function mergeConfirmations(
  previous: TradeConfirmation[],
  next: TradeConfirmation[],
): TradeConfirmation[] {
  const entries = new Map(previous.map((item) => [item.confirmation_id, item]));
  for (const item of next) {
    const old = entries.get(item.confirmation_id);
    entries.set(
      item.confirmation_id,
      old && old.status !== "pending" && item.status === "pending" ? old : item,
    );
  }
  const all = [...entries.values()];
  const cancelled = new Set(
    all
      .filter((item) => item.result?.status === "CANCELLED")
      .map((item) => item.result!.order_id),
  );
  return all
    .map((item) =>
      item.result && cancelled.has(item.result.order_id)
        ? { ...item, result: { ...item.result, status: "CANCELLED" as const } }
        : item,
    )
    .slice(-20);
}
