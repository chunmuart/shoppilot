import type { ProductCard } from "../types";
import Modal from "./Modal";
import { money, ProductImage } from "./ProductCards";
export default function ProductComparison({
  products,
  onClose,
}: {
  products: ProductCard[];
  onClose: () => void;
}) {
  return (
    <Modal title="商品比较" onClose={onClose}>
      <h2>放在一起，选择更清楚。</h2>
      <p className="modal-intro">关注你在意的不同，也给决定留一点空间。</p>
      <div className="comparison-scroll">
        <table className="compare-table">
          <thead>
            <tr>
              <th>你的心选</th>
              {products.map((product) => (
                <th key={product.product_id}>
                  <ProductImage product={product} />
                  <strong>{product.title}</strong>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>商品价</td>
              {products.map((p) => (
                <td key={p.product_id}>
                  <span className="compare-price">
                    {money(p.price_major, p.currency)}
                  </span>
                </td>
              ))}
            </tr>
            <tr>
              <td>报价规格</td>
              {products.map((p) => (
                <td key={p.product_id}>
                  {p.skus.find((sku) => sku.sku_id === p.default_sku_id)
                    ?.spec || "目录默认规格"}
                </td>
              ))}
            </tr>
            <tr>
              <td>分类</td>
              {products.map((p) => (
                <td key={p.product_id}>{p.category}</td>
              ))}
            </tr>
            <tr>
              <td>商品特点</td>
              {products.map((p) => (
                <td key={p.product_id}>
                  {p.highlights.slice(0, 3).join("；") || "目录未提供"}
                </td>
              ))}
            </tr>
            <tr>
              <td>可选规格</td>
              {products.map((p) => (
                <td key={p.product_id}>
                  {p.skus.map((s) => s.spec).join(" / ") || "目录未提供"}
                </td>
              ))}
            </tr>
            <tr>
              <td>评分样例</td>
              {products.map((p) => (
                <td key={p.product_id}>
                  {p.rating_summary
                    ? `★ ${p.rating_summary.average}（${p.rating_summary.review_count} 条）`
                    : "未提供"}
                </td>
              ))}
            </tr>
          </tbody>
        </table>
      </div>
      <p className="drawer-note comparison-note">
        基于当前查看的目录快照比较。不同规格、配送地区与币种可能影响最终价格；未提供的信息不做推断。
      </p>
    </Modal>
  );
}
