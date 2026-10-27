import { describe, expect, it } from "vitest";
import { readProducts } from "../src/lib/commerceClient";

const card = {
  product_id: "P1003",
  title: "Wanderlite 折叠旅行双肩包 35L",
  brand: "Wanderlite",
  category: "旅行装备",
  origin_country: "KR",
  price_major: 129,
  currency: "CNY",
  score: 0.8,
  highlights: ["收纳：可折叠成手掌大小"],
  skus: [
    {
      sku_id: "P1003-S1",
      spec: "石墨黑",
      price_major: 129,
      currency: "CNY",
      stock: 80,
    },
  ],
  description: "防泼水尼龙 超轻 可折叠收纳",
  rating_summary: { average: 4.2, review_count: 52 },
  rating_is_live: false,
  ships_to: ["CN", "JP", "SG"],
  dimensions_cm: { length: 14, width: 10, height: 6 },
  updated_at: "2026-08-01",
  default_sku_id: "P1003-S1",
  image_url: "/products/wanderlite.png",
  image_kind: "illustration",
  image_alt: "AI 生成示意图，非商品实拍",
  source_platform: "etsy",
  source_price_major: 129,
  source_currency: "CNY",
  canonical_product_id: "CAN-LEGACY-002",
  material_tags: ["合成聚合物"],
  weight_kg: 0.33,
  landed_price: {
    ship_to: "CN",
    subtotal_major: 129,
    freight_major: 25,
    tariff_major: 0,
    tariff_rate: 0.09,
    de_minimis_applied: true,
    landed_total_major: 154,
    currency: "CNY",
  },
};

describe("商品展示契约边界", () => {
  it("目录 DTO 原样保留，评分不升级为实时，清洗不修改来源对象", () => {
    const input = structuredClone(card);
    const [result] = readProducts([input]);
    expect(result).toEqual(card);
    result.skus[0].stock = 0;
    result.highlights.push("新增展示项");
    expect(input).toEqual(card);
  });

  it.each([-1, NaN, Infinity, -Infinity, "129"])(
    "拒绝非法商品金额 %s",
    (price_major) => {
      expect(readProducts([{ ...card, price_major }])).toEqual([]);
    },
  );

  it.each(["", "CNY1", "NOT_A_CURRENCY", "cn", "cny", 123])(
    "拒绝不能安全格式化的币种 %s",
    (currency) => {
      expect(readProducts([{ ...card, currency }])).toEqual([]);
    },
  );

  it("允许零价格和所有当前目录币种，不强制原币 SKU 与展示币种一致", () => {
    for (const currency of ["CNY", "USD", "EUR", "JPY", "SGD"]) {
      const [result] = readProducts([
        {
          ...card,
          price_major: 0,
          currency,
          skus: [
            { ...card.skus[0], currency: "USD", price_major: 0, stock: 0 },
          ],
        },
      ]);
      expect(result.price_major).toBe(0);
      expect(result.skus[0]).toMatchObject({
        currency: "USD",
        price_major: 0,
        stock: 0,
      });
      expect(() =>
        new Intl.NumberFormat("zh-CN", {
          style: "currency",
          currency: result.currency,
        }).format(result.price_major),
      ).not.toThrow();
    }
  });

  it.each([
    { stock: -1 },
    { stock: 1.5 },
    { stock: NaN },
    { stock: Infinity },
    { stock: Number.MAX_SAFE_INTEGER + 1 },
    { price_major: -1 },
    { price_major: Infinity },
    { currency: "USD!" },
  ])("无效 SKU 不参与展示：%j", (invalid) => {
    expect(
      readProducts([{ ...card, skus: [{ ...card.skus[0], ...invalid }] }]),
    ).toEqual([]);
  });

  it.each([
    { average: "4.2", review_count: 52 },
    { average: 5.1, review_count: 52 },
    { average: -1, review_count: 52 },
    { average: NaN, review_count: 52 },
    { average: 4.2, review_count: -1 },
    { average: 4.2, review_count: 2.5 },
  ])("只剔除无效评分，保留其余商品：%j", (rating_summary) => {
    const [result] = readProducts([{ ...card, rating_summary }]);
    expect(result.product_id).toBe(card.product_id);
    expect(result).not.toHaveProperty("rating_summary");
    expect(result.landed_price).toEqual(card.landed_price);
  });

  it.each([
    { landed_total_major: "154" },
    { freight_major: -1 },
    { subtotal_major: NaN },
    { currency: "USD!" },
    { currency: "USD" },
    { de_minimis_applied: "true" },
    { tariff_rate: Infinity },
  ])("只剔除不完整或币种错误的到手价：%j", (invalid) => {
    const [result] = readProducts([
      { ...card, landed_price: { ...card.landed_price, ...invalid } },
    ]);
    expect(result.price_major).toBe(129);
    expect(result).not.toHaveProperty("landed_price");
  });

  it("保留无媒体、无评分及报价失败的真实边界，不补造未知金额", () => {
    const unavailable = {
      ...card,
      image_url: null,
      image_kind: "placeholder",
      rating_summary: null,
      landed_price: { unavailable_reason: "该目的地暂不支持报价" },
    };
    const [result] = readProducts([unavailable]);
    expect(result.image_url).toBeNull();
    expect(result.image_kind).toBe("placeholder");
    expect(result.rating_summary).toBeNull();
    expect(result.landed_price).toEqual({
      unavailable_reason: "该目的地暂不支持报价",
    });
  });

  it("剔除坏的可选字段与不完整原币报价，不污染有效商品", () => {
    const input = {
      ...card,
      description: {},
      image_url: 123,
      image_kind: "photo",
      image_alt: [],
      rating_is_live: "true",
      ships_to: ["CN", 1],
      material_tags: {},
      dimensions_cm: { width: "10" },
      weight_kg: -1,
      source_price_major: -1,
      source_currency: "USD",
      updated_at: 123,
      default_sku_id: "OTHER-SKU",
      source_platform: {},
      canonical_product_id: [],
    };
    const [result] = readProducts([input]);
    expect(result.title).toBe(card.title);
    for (const field of [
      "description",
      "image_url",
      "image_kind",
      "image_alt",
      "rating_is_live",
      "ships_to",
      "material_tags",
      "dimensions_cm",
      "weight_kg",
      "source_price_major",
      "source_currency",
      "updated_at",
      "default_sku_id",
      "source_platform",
      "canonical_product_id",
    ]) {
      expect(result).not.toHaveProperty(field);
    }
    expect(result.skus).toEqual(card.skus);
    expect(input.dimensions_cm.width).toBe("10");
  });
});
