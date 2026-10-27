import { ProductImageCaption } from "./CatalogMedia";
import type { CommerceSnapshot } from "../types";
import { money, ProductImage } from "./ProductCards";

/** 历史报价只读展示，不能用旧库存、旧金额直接发起交易。 */
export default function ProductHistory({batches = []}: {batches: CommerceSnapshot["productHistory"]}) {
  if (!batches.length) return null;
  return <details className="historical-products">
    <summary>历史商品记录 · {batches.length} 轮</summary>
    <p className="results-footnote">以下为当时保存的商品信息。价格、库存和配送条件可能变化，继续购买前请重新核验。</p>
    {batches.map(batch => <section key={batch.runId} aria-label="历史商品批次">
      <h3>{new Date(batch.updatedAt).toLocaleString("zh-CN")} · {batch.products.length} 件</h3>
      <div className="product-grid">{batch.products.map((product, index) => <article className="product-card" key={product.product_id}>
        <div className="product-visual"><ProductImage product={product}/>
          <span className="visual-caption"><ProductImageCaption product={product} /></span>
        </div>
        <div className="product-body">
          <h4>{index + 1}. {product.title}</h4>
          <p>当时商品价：{money(product.price_major, product.currency)}</p>
          {product.landed_price && <p>当时报价：{product.landed_price.unavailable_reason || money(product.landed_price.landed_total_major, product.landed_price.currency)} · 配送至 {product.landed_price.ship_to}</p>}
          <ul>{product.skus.map(sku => <li key={sku.sku_id}>{sku.spec} · {money(sku.price_major, sku.currency)} · 当时库存 {sku.stock} · {sku.sku_id}</li>)}</ul>
        </div>
      </article>)}</div>
    </section>)}
  </details>;
}
