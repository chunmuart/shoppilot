import { describe, expect, it } from "vitest";
import { CommerceClient } from "../src/lib/commerceClient";
import {
  mergeConfirmations,
  readConfirmations,
} from "../src/lib/confirmations";
import type { TradeConfirmation } from "../src/types";
const card: TradeConfirmation = {
  confirmation_id: "c1",
  operation_id: "o1",
  action: "create",
  buyer_id: "b1",
  session_id: "s1",
  snapshot_hash: "a".repeat(64),
  expires_at: "2099-01-01T00:00:00+00:00",
  expired: false,
  status: "pending",
  result: null,
  payload: {
    items: [
      {
        product_id: "P1",
        sku_id: "S1",
        title: "背包 黑色",
        quantity: 2,
        unit_price_minor: 12900,
        currency: "CNY",
      },
    ],
    shipping_address: {
      recipient_name: "测试用户",
      country: "CN",
      state: "浙江",
      city: "杭州",
      address_line: "测试地址",
      postal_code: "",
      phone: "",
    },
    total_amount_minor: 25800,
    currency: "CNY",
    amount_scope: "merchandise_only",
    order_kind: "purchase_intent",
  },
};
const approved = (item = card): TradeConfirmation => ({
  ...item,
  status: "approved",
  result: {
    order_id: "order-1",
    status: "CONFIRMED",
    total_amount_minor: 25800,
    total_amount_major: 258,
    currency: "CNY",
    cancel_reason: null,
  },
});
const storage = {
  getItem: (key: string) => (key === "shoppilot.buyer" ? "b1" : null),
  setItem: () => {},
};
const response = (data: unknown) =>
  new Response(JSON.stringify(data), {
    headers: { "Content-Type": "application/json" },
  });
const input = {
  items: [{ product_id: "P1", sku_id: "S1", quantity: 2 }],
  shipping_address: card.payload.shipping_address,
};
describe("权威确认快照与用户动作", () => {
  it("新空会话尚未绑定的404为空，权限403和已有确认的404仍报错", async () => {
    let status = 404;
    const client = new CommerceClient({ buyerId: "b1", url: "/commerce/ag-ui/run", storage,
      fetch: async (_url, init) => {
        if (init?.method === "POST") {
          const body = JSON.parse(String(init.body));
          return response({ confirmation: { ...card, session_id: body.session_id } });
        }
        return new Response(JSON.stringify({ detail: status === 403 ? "无权读取此会话" : "会话不存在" }),
          { status, headers: { "Content-Type": "application/json" } });
      } });
    await client.refreshConfirmations();
    expect(client.getSnapshot().confirmationError).toBeNull();
    status = 403;
    await client.refreshConfirmations();
    expect(client.getSnapshot().confirmationError).toContain("无权");
    await client.prepareOrder(input);
    status = 404;
    await client.refreshConfirmations();
    expect(client.getSnapshot().confirmationError).toContain("不存在");
  });
  it("校验金额、数量、地址、状态和范围，破损数据不可确认", () => {
    expect(readConfirmations([card])).toHaveLength(1);
    for (const patch of [
      { payload: { ...card.payload, total_amount_minor: 1 } },
      { payload: { ...card.payload, currency: "USD" } },
      { payload: { ...card.payload, order_kind: "paid_order" } },
      {
        payload: {
          ...card.payload,
          items: [{ ...card.payload.items[0], quantity: 1.2 }],
        },
      },
      { payload: { ...card.payload, shipping_address: {} } },
      { status: "approved", result: null },
      { expires_at: "invalid" },
    ])
      expect(readConfirmations([{ ...card, ...patch }])).toEqual([]);
  });
  it("迟到pending不能反转批准，取消同步到同一订单历史卡", () => {
    expect(mergeConfirmations([approved()], [card])[0].status).toBe("approved");
    const cancelled = {
      ...approved(),
      confirmation_id: "cancel-1",
      action: "cancel" as const,
      result: { ...approved().result!, status: "CANCELLED" as const },
    };
    expect(
      mergeConfirmations([approved()], [cancelled]).every(
        (item) => item.result?.status === "CANCELLED",
      ),
    ).toBe(true);
  });
  it("准备只发商品地址；批准只发凭证绑定，不接收客户端金额", async () => {
    const requests: Record<string, unknown>[] = [];
    const client = new CommerceClient({ buyerId: "b1",
      url: "/commerce/ag-ui/run",
      storage,
      fetch: async (url, init) => {
        const body = JSON.parse(String(init.body));
        requests.push(body);
        const saved = { ...card, session_id: body.session_id };
        return response({
          confirmation: String(url).endsWith("/resolve")
            ? approved(saved)
            : saved,
        });
      },
    });
    expect(await client.prepareOrder(input)).toBe(true);
    expect(requests[0]).toEqual({
      ...input,
      buyer_id: "b1",
      session_id: client.getSnapshot().sessionId,
    });
    const saved = client.getSnapshot().confirmations[0];
    await client.resolveConfirmation(saved, true);
    expect(requests[1]).toEqual({
      buyer_id: "b1",
      session_id: saved.session_id,
      snapshot_hash: saved.snapshot_hash,
      approved: true,
    });
    expect(client.getSnapshot().confirmations[0].result?.order_id).toBe(
      "order-1",
    );
  });
  it("重复点击只发一次，切会话的迟到响应不污染页面", async () => {
    let finish!: (response: Response) => void,
      body: Record<string, string> = {},
      calls = 0;
    const client = new CommerceClient({ buyerId: "b1",
      url: "/commerce/ag-ui/run",
      storage,
      fetch: async (_, init) => {
        calls++;
        body = JSON.parse(String(init.body));
        return new Promise<Response>((resolve) => {
          finish = resolve;
        });
      },
    });
    const first = client.prepareOrder(input);
    expect(await client.prepareOrder(input)).toBe(false);
    expect(calls).toBe(1);
    client.reset();
    finish(
      response({ confirmation: { ...card, session_id: body.session_id } }),
    );
    expect(await first).toBe(false);
    expect(client.getSnapshot().confirmations).toEqual([]);
  });
  it("确认后的迟到列表刷新不能恢复旧pending卡", async () => {
    let finish!: (response: Response) => void, saved: TradeConfirmation;
    const client = new CommerceClient({ buyerId: "b1",
      url: "/commerce/ag-ui/run",
      storage,
      fetch: async (url, init) => {
        if (String(url).includes("?"))
          return new Promise<Response>((resolve) => {
            finish = resolve;
          });
        const body = JSON.parse(String(init.body));
        saved = { ...card, session_id: body.session_id };
        return response({
          confirmation: String(url).endsWith("/resolve")
            ? approved(saved)
            : saved,
        });
      },
    });
    await client.prepareOrder(input);
    const refreshing = client.refreshConfirmations();
    await client.resolveConfirmation(
      client.getSnapshot().confirmations[0],
      true,
    );
    finish(response({ confirmations: [saved!] }));
    await refreshing;
    expect(client.getSnapshot().confirmations[0].status).toBe("approved");
  });
  it("错误归属不可显示；断网保留原凭证用于幂等重试", async () => {
    const wrong = new CommerceClient({ buyerId: "b1",
      url: "/commerce/ag-ui/run",
      storage,
      fetch: async () => response({ confirmation: card }),
    });
    expect(await wrong.prepareOrder(input)).toBe(false);
    expect(wrong.getSnapshot().confirmations).toEqual([]);
    let fail = false;
    const client = new CommerceClient({ buyerId: "b1",
      url: "/commerce/ag-ui/run",
      storage,
      fetch: async (_, init) => {
        if (fail) throw new Error("连接中断");
        return response({
          confirmation: {
            ...card,
            session_id: JSON.parse(String(init.body)).session_id,
          },
        });
      },
    });
    await client.prepareOrder(input);
    fail = true;
    expect(
      await client.resolveConfirmation(
        client.getSnapshot().confirmations[0],
        true,
      ),
    ).toBe(false);
    expect(client.getSnapshot().confirmations[0].confirmation_id).toBe("c1");
    expect(client.getSnapshot().confirmationError).toContain(
      "重试同一确认不会重复执行",
    );
  });
});

it("刷新恢复当前会话，并从服务端读取确认；收货快照不写本机历史", async () => {
  const values = new Map<string, string>([["shoppilot.buyer", "b1"]]);
  const persistent = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
  };
  let saved: TradeConfirmation;
  const fetch = async (url: string, init: RequestInit) => {
    if (url.includes("?"))
      return response({ confirmations: [approved(saved)] });
    saved = { ...card, session_id: JSON.parse(String(init.body)).session_id };
    return response({ confirmation: saved });
  };
  const first = new CommerceClient({ buyerId: "b1",
    url: "/commerce/ag-ui/run",
    storage: persistent,
    fetch,
  });
  await first.prepareOrder(input);
  const restored = new CommerceClient({ buyerId: "b1",
    url: "/commerce/ag-ui/run",
    storage: persistent,
    fetch,
  });
  expect(restored.getSnapshot().sessionId).toBe(first.getSnapshot().sessionId);
  expect(restored.getSnapshot().confirmations).toEqual([]);
  expect(values.get("shoppilot.agui.sessions.v1")).not.toContain("测试地址");
  await restored.refreshConfirmations();
  expect(restored.getSnapshot().confirmations[0].status).toBe("approved");
  restored.reset();
  const fresh = new CommerceClient({ buyerId: "b1",
    url: "/commerce/ag-ui/run",
    storage: persistent,
    fetch,
  });
  expect(fresh.getSnapshot().sessionId).not.toBe(first.getSnapshot().sessionId);
});
