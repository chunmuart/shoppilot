import { useEffect, useState } from "react";
import type { TradeConfirmation } from "../types";
import { money } from "./ProductCards";
import "./confirmations.css";

interface ConfirmationCardsProps {
  confirmations: TradeConfirmation[];
  busy: boolean;
  error: string | null;
  onResolve: (confirmation: TradeConfirmation, approved: boolean) => void;
  onCancelOrder: (orderId: string, reason: string) => void;
  onRefresh: () => void;
}

export function isConfirmationExpired(
  confirmation: TradeConfirmation,
  now: number,
) {
  if (confirmation.status !== "pending") return false;
  const expiresAt = Date.parse(confirmation.expires_at);
  return (
    confirmation.expired || !Number.isFinite(expiresAt) || expiresAt <= now
  );
}

function expiryLabel(expiresAt: string) {
  const date = new Date(expiresAt);
  return Number.isFinite(date.getTime())
    ? date.toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      })
    : "有效期需重新核验";
}

function ConfirmationCard({
  confirmation,
  now,
  busy,
  cancelled,
  cancelPending,
  onResolve,
  onCancelOrder,
}: {
  confirmation: TradeConfirmation;
  now: number;
  busy: boolean;
  cancelled: boolean;
  cancelPending: boolean;
  onResolve: ConfirmationCardsProps["onResolve"];
  onCancelOrder: ConfirmationCardsProps["onCancelOrder"];
}) {
  const [showCancel, setShowCancel] = useState(false),
    [reason, setReason] = useState("");
  const { payload, status, action, result } = confirmation;
  const expired = isConfirmationExpired(confirmation, now),
    pending = status === "pending",
    cancellation = action === "cancel",
    actionable = pending && !expired && !cancelled,
    address = payload.shipping_address;
  const label = pending
    ? cancelled && cancellation
      ? "订单已取消"
      : expired
        ? "确认已过期"
        : "等待你确认"
    : status === "rejected"
      ? "已放弃本次操作"
      : cancelled
        ? "意向单已取消"
        : "意向单已创建";
  const orderId = result?.order_id || payload.order_id;
  return (
    <article
      className={`confirmation-card ${pending && !expired ? "is-pending" : "is-resolved"}`}
    >
      <header className="confirmation-card-heading">
        <div>
          <span className="confirmation-eyebrow">
            {cancellation ? "取消确认" : "下单意向"}
          </span>
          <h3>
            {!pending
              ? label
              : cancellation
                ? "确认取消这笔意向单"
                : "核对后，再确认"}
          </h3>
        </div>
        <span
          className={`confirmation-status ${expired ? "is-expired" : ""}`}
          role="status"
        >
          {label}
        </span>
      </header>

      <details
        className={`confirmation-details ${pending ? "is-open" : ""}`}
        open={pending}
      >
        <summary>
          {money(payload.total_amount_minor / 100, payload.currency)} ·{" "}
          {payload.items.reduce((total, item) => total + item.quantity, 0)}{" "}
          件商品
          <span>查看明细与收货信息</span>
        </summary>
        <ul className="confirmation-items">
          {payload.items.map((item) => (
            <li key={`${item.product_id}-${item.sku_id}`}>
              <div>
                <strong>{item.title}</strong>
                <small>
                  规格编号 {item.sku_id} · 数量 {item.quantity}
                </small>
              </div>
              <div className="confirmation-item-price">
                <span>
                  {money(
                    (item.unit_price_minor * item.quantity) / 100,
                    item.currency,
                  )}
                </span>
                <small>
                  单价 {money(item.unit_price_minor / 100, item.currency)}
                </small>
              </div>
            </li>
          ))}
        </ul>

        <div className="confirmation-address">
          <span>收货信息</span>
          <div>
            <strong>
              {address.recipient_name}
              {address.phone ? ` · ${address.phone}` : ""}
            </strong>
            <p>
              {[
                address.country,
                address.state,
                address.city,
                address.address_line,
                address.postal_code,
              ]
                .filter(Boolean)
                .join(" · ")}
            </p>
          </div>
        </div>
        {cancellation && (
          <p className="confirmation-cancel-reason">
            取消原因：{payload.reason}
          </p>
        )}
        <div className="confirmation-total">
          <div>
            <span>商品金额合计</span>
            <small>不含运费与关税</small>
          </div>
          <strong>
            {money(payload.total_amount_minor / 100, payload.currency)}{" "}
            <small>{payload.currency}</small>
          </strong>
        </div>
        <p className="confirmation-scope">
          仅记录下单意向，尚未支付，也未安排发货。
        </p>
        {orderId && (
          <p className="confirmation-order-id">
            意向单号 <span>{orderId}</span>
          </p>
        )}
      </details>

      {pending && (
        <div className="confirmation-decision">
          <p className={`confirmation-expiry ${expired ? "is-expired" : ""}`}>
            {expired
              ? "本次确认已过期，请重新生成确认单。"
              : `有效至 ${expiryLabel(confirmation.expires_at)}`}
          </p>
          <div className="confirmation-actions">
            <button
              type="button"
              className="confirmation-secondary"
              disabled={busy || !actionable}
              onClick={() => onResolve(confirmation, false)}
            >
              {cancellation ? "保留意向单" : "暂不下单"}
            </button>
            <button
              type="button"
              className="confirmation-primary"
              disabled={busy || !actionable}
              onClick={() => onResolve(confirmation, true)}
            >
              {busy
                ? "正在处理…"
                : cancellation
                  ? "确认取消意向单"
                  : "确认创建意向单"}
            </button>
          </div>
        </div>
      )}

      {!pending &&
        status === "approved" &&
        !cancellation &&
        result &&
        !cancelled && (
          <div className="confirmation-cancel-area">
            {cancelPending ? (
              <p className="confirmation-hint">
                已有待确认的取消申请，请在取消确认卡中处理。
              </p>
            ) : showCancel ? (
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  if (!reason.trim() || busy) return;
                  onCancelOrder(result.order_id, reason.trim());
                }}
              >
                <label>
                  取消原因
                  <textarea
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                    required
                    maxLength={500}
                    rows={2}
                    placeholder="请填写本次取消原因"
                    disabled={busy}
                  />
                </label>
                <div className="confirmation-actions">
                  <button
                    type="button"
                    className="confirmation-secondary"
                    disabled={busy}
                    onClick={() => setShowCancel(false)}
                  >
                    返回
                  </button>
                  <button
                    type="submit"
                    className="confirmation-primary"
                    disabled={busy || !reason.trim()}
                  >
                    {busy ? "正在准备…" : "生成取消确认单"}
                  </button>
                </div>
              </form>
            ) : (
              <button
                type="button"
                className="confirmation-text-button"
                disabled={busy}
                onClick={() => setShowCancel(true)}
              >
                申请取消这笔意向单
              </button>
            )}
          </div>
        )}
    </article>
  );
}

export default function ConfirmationCards({
  confirmations,
  busy,
  error,
  onResolve,
  onCancelOrder,
  onRefresh,
}: ConfirmationCardsProps) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (
      !confirmations.some((confirmation) => confirmation.status === "pending")
    )
      return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [confirmations]);
  const cancelledOrders = new Set(
    confirmations.flatMap((confirmation) =>
      confirmation.result?.status === "CANCELLED"
        ? [confirmation.result.order_id]
        : [],
    ),
  );
  const pendingCancellations = new Set(
    confirmations.flatMap((confirmation) =>
      confirmation.action === "cancel" &&
      confirmation.status === "pending" &&
      !isConfirmationExpired(confirmation, now) &&
      confirmation.payload.order_id
        ? [confirmation.payload.order_id]
        : [],
    ),
  );
  if (!confirmations.length && !error && !busy) return null;
  return (
    <section
      className="confirmations-section"
      aria-label="交易确认与意向单"
      aria-busy={busy}
    >
      <div className="confirmations-heading">
        <div>
          <span className="confirmation-eyebrow">由你作决定</span>
          <h2>交易确认与意向单</h2>
        </div>
        <button
          type="button"
          className="confirmation-refresh"
          disabled={busy}
          onClick={onRefresh}
        >
          {busy ? "正在同步…" : "刷新记录"}
        </button>
      </div>
      {error && (
        <div className="confirmation-error" role="alert">
          <strong>请核对操作状态</strong>
          <p>{error}</p>
          <span>请刷新记录核对状态后再试。</span>
        </div>
      )}
      <div className="confirmation-list">
        {confirmations.map((confirmation) => (
          <ConfirmationCard
            key={confirmation.confirmation_id}
            confirmation={confirmation}
            now={now}
            busy={busy}
            cancelled={cancelledOrders.has(
              confirmation.result?.order_id ||
                confirmation.payload.order_id ||
                "",
            )}
            cancelPending={pendingCancellations.has(
              confirmation.result?.order_id || "",
            )}
            onResolve={onResolve}
            onCancelOrder={onCancelOrder}
          />
        ))}
      </div>
    </section>
  );
}
