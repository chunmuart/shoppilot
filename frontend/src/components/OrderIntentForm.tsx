import { useState, type FormEvent } from "react";
import type { PrepareOrderInput, ProductCard, ShippingAddress } from "../types";
import Modal from "./Modal";
import { money } from "./ProductCards";
import "./confirmations.css";

interface OrderIntentFormProps {
  product: ProductCard;
  skuId: string;
  busy: boolean;
  error: string | null;
  onClose: () => void;
  onPrepare: (input: PrepareOrderInput) => Promise<boolean>;
}

export default function OrderIntentForm({
  product,
  skuId,
  busy,
  error,
  onClose,
  onPrepare,
}: OrderIntentFormProps) {
  const countries = product.ships_to || [];
  const [address, setAddress] = useState<ShippingAddress>({
      recipient_name: "",
      country: countries.includes("CN") ? "CN" : countries[0] || "",
      state: "",
      city: "",
      address_line: "",
      postal_code: "",
      phone: "",
    }),
    [quantity, setQuantity] = useState("1"),
    [localError, setLocalError] = useState<string | null>(null),
    [submitting, setSubmitting] = useState(false);
  const sku = product.skus.find((item) => item.sku_id === skuId),
    locked = busy || submitting,
    quantityValue = Number(quantity),
    quantityValid =
      quantity.trim() !== "" &&
      Number.isSafeInteger(quantityValue) &&
      quantityValue > 0;
  const field = (key: keyof ShippingAddress, value: string) =>
    setAddress((current) => ({ ...current, [key]: value }));

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (locked) return;
    if (!sku || !quantityValid) {
      setLocalError("请选择有效规格，并填写正整数数量。");
      return;
    }
    const normalized = Object.fromEntries(
      Object.entries(address).map(([key, value]) => [key, value.trim()]),
    ) as unknown as ShippingAddress;
    if (
      ![
        normalized.recipient_name,
        normalized.country,
        normalized.city,
        normalized.address_line,
      ].every(Boolean)
    ) {
      setLocalError("请补全收件人、配送国家或地区、城市与详细地址。");
      return;
    }
    if (!countries.includes(normalized.country)) {
      setLocalError("当前目录未确认可配送到该地区，请重新查询。");
      return;
    }
    setLocalError(null);
    setSubmitting(true);
    try {
      const prepared = await onPrepare({
        items: [
          {
            product_id: product.product_id,
            sku_id: sku.sku_id,
            quantity: quantityValue,
          },
        ],
        shipping_address: normalized,
      });
      if (prepared) onClose();
    } catch {
      setLocalError("未能确认提交结果，请刷新确认记录后重试。");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal
      title="准备下单意向"
      drawer
      onClose={() => {
        if (!locked) onClose();
      }}
    >
      <div className="order-intent-intro">
        <span className="confirmation-eyebrow">先核对，再决定</span>
        <h2>准备下单意向</h2>
        <p>
          填写收货信息后，我们会重新核对价格与库存，生成一张由你确认的意向单。
        </p>
      </div>
      <div className="order-intent-product">
        <strong>{product.title}</strong>
        <span>
          {sku?.spec || "规格不可用"} · {skuId}
        </span>
        {sku && (
          <div>
            <strong>{money(sku.price_major, sku.currency)}</strong>
            <small>目录商品单价 · 最终以确认单为准</small>
          </div>
        )}
      </div>
      <form className="order-intent-form" onSubmit={submit}>
        <label>
          购买数量 <span aria-hidden="true">*</span>
          <input
            name="quantity"
            type="number"
            min="1"
            step="1"
            required
            value={quantity}
            onChange={(event) => setQuantity(event.target.value)}
            disabled={locked}
            inputMode="numeric"
          />
        </label>
        <p className="order-intent-help">
          库存以服务端生成确认单和提交时的检查为准。
        </p>
        <fieldset disabled={locked}>
          <legend>收货信息</legend>
          <label>
            收件人 <span aria-hidden="true">*</span>
            <input
              name="recipient_name"
              autoComplete="shipping name"
              value={address.recipient_name}
              onChange={(event) => field("recipient_name", event.target.value)}
              required
              maxLength={100}
              placeholder="收件人的姓名"
            />
          </label>
          <div className="order-intent-field-pair">
            <label>
              国家或地区 <span aria-hidden="true">*</span>
              <select
                name="country"
                autoComplete="shipping country"
                required
                value={address.country}
                onChange={(event) => field("country", event.target.value)}
              >
                {!countries.length && <option value="">配送范围待核验</option>}
                {countries.map((country) => (
                  <option key={country} value={country}>
                    {country === "CN" ? "中国 · CN" : country}
                  </option>
                ))}
              </select>
            </label>
            <label>
              省 / 州
              <input
                name="state"
                autoComplete="shipping address-level1"
                value={address.state}
                onChange={(event) => field("state", event.target.value)}
                maxLength={100}
              />
            </label>
          </div>
          <label>
            城市 <span aria-hidden="true">*</span>
            <input
              name="city"
              autoComplete="shipping address-level2"
              value={address.city}
              onChange={(event) => field("city", event.target.value)}
              required
              maxLength={100}
            />
          </label>
          <label>
            详细地址 <span aria-hidden="true">*</span>
            <textarea
              name="address_line"
              autoComplete="shipping street-address"
              value={address.address_line}
              onChange={(event) => field("address_line", event.target.value)}
              required
              maxLength={500}
              rows={3}
              placeholder="街道、楼栋与门牌号"
            />
          </label>
          <div className="order-intent-field-pair">
            <label>
              邮政编码
              <input
                name="postal_code"
                autoComplete="shipping postal-code"
                value={address.postal_code}
                onChange={(event) => field("postal_code", event.target.value)}
                maxLength={30}
              />
            </label>
            <label>
              联系电话
              <input
                name="phone"
                type="tel"
                autoComplete="shipping tel"
                value={address.phone}
                onChange={(event) => field("phone", event.target.value)}
                maxLength={40}
              />
            </label>
          </div>
        </fieldset>
        {(localError || error) && (
          <div className="confirmation-error" role="alert">
            {localError || error}
          </div>
        )}
        <div className="order-intent-footer">
          <p>
            确认单只包含商品金额，不含运费与关税。生成确认单不会下单、扣库存或付款。
          </p>
          <button
            className="confirmation-primary"
            type="submit"
            disabled={locked || !sku || !countries.length || !quantityValid}
          >
            {locked ? "正在核对并生成…" : "生成确认单"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
