// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { expect, it } from 'vitest';
import { CatalogMediaProvider, parseMediaManifest, ProductImageCaption } from '../src/components/CatalogMedia';
import { ProductImage } from '../src/components/ProductCards';

const manifest = {kind:'illustration',assets:{bag:{url:'/products/catalog-atlas-a-v1.png',cell:0,columns:4,rows:4}},products:{P10000:'bag'},skus:{'P10000-S1':'P10000'}};
it('拒绝外链、越界裁切和未知商品，不能按相似标题猜图',()=>{
 expect(parseMediaManifest({...manifest,assets:{bag:{url:'https://example.com/test.png'}}}).products).toEqual({});
 expect(parseMediaManifest({...manifest,assets:{bag:{...manifest.assets.bag,cell:16}}}).products).toEqual({});
 expect(parseMediaManifest({})).toEqual({assets:{},products:{},skus:{}});
});
it('旧快照与订单 SKU 从注册表补图，原图优先，失败回退且 URL 更新可重试',async()=>{
 (globalThis as any).IS_REACT_ACT_ENVIRONMENT=true;
 const host=document.createElement('div');document.body.append(host);const root=createRoot(host);
 const product={product_id:'P10000',title:'旅行背包',price_major:100};
 const frozen=JSON.stringify(product);
 const request=async()=>manifest;
 const render=async(p:any)=>act(async()=>root.render(<CatalogMediaProvider request={request}><ProductImage product={p}/><ProductImageCaption product={p}/></CatalogMediaProvider>));
 try {
  await render(product);
  expect(host.querySelector('image')?.getAttribute('href')).toBe('/products/catalog-atlas-a-v1.png');
  expect(host.textContent).toContain('非实物照片'); expect(JSON.stringify(product)).toBe(frozen);
  await render({title:product.title,sku_id:'P10000-S1'});
  expect(host.querySelector('image')).not.toBeNull();
  await render({...product,image_url:'/products/original.png'});
  expect(host.querySelector('img')?.getAttribute('src')).toBe('/products/original.png');
  await act(async()=>host.querySelector('img')!.dispatchEvent(new Event('error')));
  expect(host.textContent).toContain('暂未提供商品图片');
  await render({...product,image_url:'/products/replaced.png'});
  expect(host.querySelector('img')?.getAttribute('src')).toBe('/products/replaced.png');
  await render({...product,product_id:'UNKNOWN'});
  expect(host.querySelector('image')).toBeNull();
 } finally { await act(async()=>root.unmount()); host.remove(); }
});
