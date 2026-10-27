import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import ConfirmationCards, {
  isConfirmationExpired,
} from "../src/components/ConfirmationCards";
import OrderIntentForm from "../src/components/OrderIntentForm";
import type { ProductCard, TradeConfirmation } from "../src/types";

const confirmation: TradeConfirmation = {
  confirmation_id: "confirm-1",
  operation_id: "operation-1",
  action: "create",
  buyer_id: "buyer-1",
  session_id: "session-1",
  snapshot_hash: "a".repeat(64),
  expires_at: "2026-09-09T12:05:00Z",
  expired: false,
  status: "pending",
  result: null,
  payload: {
    items: [
      {
        product_id: "P1001",
        sku_id: "P1001-S1",
        title: "通勤背包（黑色）",
        unit_price_minor: 18900,
        currency: "CNY",
        quantity: 2,
      },
    ],
    shipping_address: {
      recipient_name: "测试收件人",
      country: "CN",
      state: "浙江",
      city: "杭州",
      address_line: "测试街道 1 号",
      postal_code: "310000",
      phone: "",
    },
    total_amount_minor: 37800,
    currency: "CNY",
    amount_scope: "merchandise_only",
    order_kind: "purchase_intent",
  },
};
const order = {
  order_id: "GBX-test",
  status: "CONFIRMED" as const,
  total_amount_minor: 37800,
  total_amount_major: 378,
  currency: "CNY",
  cancel_reason: null,
};
const product: ProductCard = {
  product_id: "P1001",
  title: "通勤背包",
  brand: "示例品牌",
  category: "背包",
  origin_country: "CN",
  price_major: 189,
  currency: "CNY",
  highlights: [],
  score: 1,
  ships_to: ["CN", "US"],
  skus: [
    {
      sku_id: "P1001-S1",
      spec: "黑色",
      price_major: 189,
      currency: "CNY",
      stock: 10,
    },
  ],
};
function render(
  confirmations = [confirmation],
  busy = false,
  error: string | null = null,
) {
  return renderToStaticMarkup(
    <ConfirmationCards
      confirmations={confirmations}
      busy={busy}
      error={error}
      onResolve={() => {}}
      onCancelOrder={() => {}}
      onRefresh={() => {}}
    />,
  );
}
afterEach(() => vi.useRealTimers());

describe("确认卡的权威状态与执行边界", () => {
  it("呈现 SKU、数量、地址与商品金额，声明未支付并且无内部决议参数", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-09T12:00:00Z"));
    const html = render();
    for (const text of [
      "P1001-S1",
      "数量 2",
      "测试收件人",
      "测试街道 1 号",
      "378.00",
      "不含运费与关税",
      "尚未支付，也未安排发货",
      "确认创建意向单",
    ])
      expect(html).toContain(text);
    expect(html).not.toContain("operation-1");
    expect(html).not.toContain("a".repeat(64));
    expect(html).not.toContain('disabled=""');
  });
  it.each([
    ["本地时钟到期", { ...confirmation }, Date.parse(confirmation.expires_at)],
    [
      "服务端判定到期",
      { ...confirmation, expired: true },
      Date.parse("2026-09-09T12:00:00Z"),
    ],
    [
      "异常有效期",
      { ...confirmation, expires_at: "invalid" },
      Date.parse("2026-09-09T12:00:00Z"),
    ],
  ])("%s 时阻止执行", (_, input, now) => {
    expect(
      isConfirmationExpired(input as TradeConfirmation, now as number),
    ).toBe(true);
    vi.useFakeTimers();
    vi.setSystemTime(now as number);
    const html = render([input as TradeConfirmation]);
    expect(html).toContain("本次确认已过期");
    expect(html).toMatch(/disabled=""[^>]*>确认创建意向单/);
  });
  it("已决议确认即使时间经过也保持真实结果，不重新显示确认按钮", () => {
    const approved = {
      ...confirmation,
      status: "approved" as const,
      result: order,
    };
    expect(isConfirmationExpired(approved, Date.parse("2030-01-01"))).toBe(
      false,
    );
    const html = render([approved]);
    expect(html).toContain("GBX-test");
    expect(html).toContain("意向单已创建");
    expect(html).not.toContain("确认创建意向单");
    expect(html).toContain("申请取消这笔意向单");
  });
  it("拒绝不伪装成功，也不再暴露执行或取消入口", () => {
    const html = render([{ ...confirmation, status: "rejected" }]);
    expect(html).toContain("已放弃本次操作");
    expect(html).not.toContain("意向单已创建");
    expect(html).not.toContain("确认创建意向单");
    expect(html).not.toContain("申请取消这笔意向单");
  });
  it("取消的权威结果覆盖列表中较旧的已创建快照", () => {
    const html = render([
      { ...confirmation, status: "approved", result: order },
      {
        ...confirmation,
        confirmation_id: "cancel-1",
        action: "cancel",
        status: "approved",
        payload: {
          ...confirmation.payload,
          order_id: order.order_id,
          reason: "改变计划",
        },
        result: { ...order, status: "CANCELLED", cancel_reason: "改变计划" },
      },
    ]);
    expect(html).toContain("意向单已取消");
    expect(html).toContain("改变计划");
    expect(html).not.toContain("申请取消这笔意向单");
  });
  it("已有待确认取消时不再允许从旧创建卡重复发起取消", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-09T12:00:00Z"));
    const html = render([
      { ...confirmation, status: "approved", result: order },
      {
        ...confirmation,
        confirmation_id: "cancel-1",
        action: "cancel",
        payload: {
          ...confirmation.payload,
          order_id: order.order_id,
          reason: "改变计划",
        },
      },
    ]);
    expect(html).toContain("已有待确认的取消申请");
    expect(html).toContain("确认取消意向单");
    expect(html).not.toContain("申请取消这笔意向单");
  });
  it("请求进行中禁用重复决议，失败结果可刷新恢复", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-09T12:00:00Z"));
    expect(render([confirmation], true)).toMatch(/disabled=""[^>]*>正在处理/);
    const html = render([], false, "服务端价格已变化");
    expect(html).toContain('role="alert"');
    expect(html).toContain("服务端价格已变化");
    expect(html).toContain("刷新记录");
  });
});

describe("准备表单不把展示价格和假地址变成下单事实", () => {
  it("只提供真实可配送地区，地址必填并留空，标明生成确认不会下单", () => {
    const html = renderToStaticMarkup(
      <OrderIntentForm
        product={product}
        skuId="P1001-S1"
        busy={false}
        error={null}
        onClose={() => {}}
        onPrepare={async () => true}
      />,
    );
    for (const field of ["recipient_name", "city", "address_line", "quantity"])
      expect(html).toMatch(new RegExp(`name="${field}"[^>]*required=""`));
    expect(html).toContain('value="US"');
    expect(html).not.toContain("测试收件人");
    expect(html).toContain("生成确认单不会下单、扣库存或付款");
    expect(html).toContain("最终以确认单为准");
  });
  it("缺少配送范围或有效规格时不能生成确认", () => {
    const html = renderToStaticMarkup(
      <OrderIntentForm
        product={{ ...product, ships_to: [] }}
        skuId="不存在的 SKU"
        busy={false}
        error={null}
        onClose={() => {}}
        onPrepare={async () => true}
      />,
    );
    expect(html).toContain("配送范围待核验");
    expect(html).toContain("规格不可用");
    expect(html).toMatch(/disabled=""[^>]*>生成确认单/);
  });
});
